"""Optional multilingual embeddings and an idempotent, rebuildable ANN projection.

The vector index is separate from the authoritative SQLite versions and from
BM25. A missing model never creates fabricated semantic vectors. Public-only P1.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from .db import Database, clock_ms
from .opensearch import OpenSearchClient, SearchBackendError, require_index_name
from .store import BlobStore

VECTOR_READ_ALIAS = "search-vectors-read"
VECTOR_WRITE_ALIAS = "search-vectors-write"
VECTOR_INDEX = "search-vectors-v001"
DIMENSIONS = 384
DEFAULT_MODEL_ID = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MAX_TEXT_CHARS = 4000
MAX_PROJECTION_BATCH = 16


class SemanticUnavailable(RuntimeError):
    """Optional semantic dependency or model is not usable."""


class Embedder(Protocol):
    model_key: str
    dimension: int

    def encode(self, texts: list[str]) -> list[list[float]]: ...


def model_fingerprint(model_id: str, revision: str, *, dimension: int = DIMENSIONS) -> str:
    if not model_id or not revision or dimension != DIMENSIONS:
        raise ValueError("Missing model identity or incompatible dimension")
    return hashlib.sha256(f"{model_id}\0{revision}\0{dimension}\0{MAX_TEXT_CHARS}".encode()).hexdigest()


class SentenceTransformerEmbedder:
    """Real optional embedding provider. Pin a model revision for production.

    Dependency install: pip install -e '.[semantic]'. First use downloads model
    artifacts; operators must review model licenses and pin model revision.
    """

    dimension = DIMENSIONS

    def __init__(self, model_id: str = DEFAULT_MODEL_ID, revision: str = "main"):
        self.model_id = model_id
        self.revision = revision
        self.model_key = model_fingerprint(model_id, revision)
        self._model = None

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            if self._model is None:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self.model_id, revision=self.revision)
            result = self._model.encode(texts, convert_to_numpy=True,
                                        normalize_embeddings=True, show_progress_bar=False)
            return [list(map(float, row)) for row in result]
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            raise SemanticUnavailable(f"Embedding model unavailable: {type(exc).__name__}") from exc


class CrossEncoderReranker:
    """Optional CrossEncoder. Intended for an explicitly provisioned model.

    Default model is English-first; evaluate Chinese/mixed quality before use.
    """

    def __init__(self, model_id: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
                 revision: str = "main"):
        self.model_id = model_id
        self.revision = revision
        self._model = None

    def score(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        try:
            if self._model is None:
                from sentence_transformers import CrossEncoder
                self._model = CrossEncoder(self.model_id, revision=self.revision)
            values = self._model.predict([(query, d) for d in documents],
                                         show_progress_bar=False)
            scores = [float(v) for v in values]
            if len(scores) != len(documents) or any(not math.isfinite(s) for s in scores):
                raise ValueError("Cross-Encoder returned invalid scores")
            return scores
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            raise SemanticUnavailable(f"Reranker unavailable: {type(exc).__name__}") from exc


def normalized_vector(values: list[float], *, dimension: int = DIMENSIONS) -> list[float]:
    if len(values) != dimension:
        raise ValueError(f"Expected {dimension} embedding components")
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or
           not math.isfinite(x) for x in values):
        raise ValueError("Embedding has invalid/nonfinite values")
    norm = math.sqrt(sum(float(x) * float(x) for x in values))
    if norm <= 1e-12:
        raise ValueError("Embedding norm is zero")
    return [float(x) / norm for x in values]


class VectorIndexManager:
    def __init__(self, client: OpenSearchClient, mapping_path: str | Path):
        self.client = client
        self.mapping_path = Path(mapping_path)

    def initialize(self, index: str = VECTOR_INDEX) -> None:
        require_index_name(index)
        if self.client.alias_targets(VECTOR_READ_ALIAS) or self.client.alias_targets(VECTOR_WRITE_ALIAS):
            raise ValueError("Vector aliases already exist; explicit migration required")
        mapping = json.loads(self.mapping_path.read_text(encoding="utf-8"))
        properties = mapping["mappings"]["properties"]
        if mapping["mappings"].get("dynamic") != "strict":
            raise ValueError("Vector index requires strict mapping")
        if properties["embedding"]["dimension"] != DIMENSIONS:
            raise ValueError("Vector index dimension differs from embedder")
        if mapping["settings"]["index"].get("knn") is not True:
            raise ValueError("Approximate kNN must be enabled")
        self.client.create_index(index, mapping)
        self.client.request("POST", "/_aliases", {"actions": [
            {"add": {"index": index, "alias": VECTOR_READ_ALIAS}},
            {"add": {"index": index, "alias": VECTOR_WRITE_ALIAS,
                     "is_write_index": True}},
        ]})


class VectorProjector:
    """Resumable reconciliation scan of latest SQLite document revisions.

    SQLite vector_receipts is a checkpoint, *not* the embedding source of truth.
    Identical retries are safe: OpenSearch external_gte protects against old
    revisions and receipt writes verify the current authoritative revision.
    Concurrent workers may repeat work; P1 explicitly uses one worker.
    """

    def __init__(self, db: Database, store: BlobStore, client: OpenSearchClient,
                 embedder: Embedder, *, alias: str = VECTOR_WRITE_ALIAS):
        if embedder.dimension != DIMENSIONS:
            raise ValueError("Embedder dimensions must match knn_vector mapping")
        if not isinstance(embedder.model_key, str) or len(embedder.model_key) != 64:
            raise ValueError("Embedder must expose a stable sha256 model_key")
        self.db, self.store, self.client, self.embedder = db, store, client, embedder
        self.alias = require_index_name(alias)

    def pending_count(self) -> int:
        with self.db.connect() as con:
            return con.execute("""SELECT COUNT(*) FROM documents d
                LEFT JOIN vector_receipts v
                  ON v.document_id=d.document_id AND v.model_key=?
                WHERE d.latest_revision>0 AND COALESCE(v.revision,0)<d.latest_revision
                """, (self.embedder.model_key,)).fetchone()[0]

    def sync_once(self, *, count: int = 8, now_ms: int | None = None) -> dict:
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_PROJECTION_BATCH:
            raise ValueError("Vector batch size must be between 1 and 16")
        now = clock_ms() if now_ms is None else now_ms
        with self.db.connect() as con:
            rows = con.execute("""SELECT d.document_id, d.source_id,d.canonical_url,
                d.latest_revision AS revision,d.updated_at_ms, d.is_deleted,
                v.title,v.description,v.language,v.text_blob_ref,v.is_indexable
                FROM documents d
                JOIN document_versions v ON v.document_id=d.document_id
                                        AND v.revision=d.latest_revision
                LEFT JOIN vector_receipts r ON r.document_id=d.document_id
                                          AND r.model_key=?
                WHERE d.latest_revision>0 AND COALESCE(r.revision,0)<d.latest_revision
                ORDER BY d.updated_at_ms ASC, d.document_id ASC LIMIT ?""",
                (self.embedder.model_key, count)).fetchall()
            rows = [dict(row) for row in rows]
        stats = {"selected": len(rows), "acked": 0, "failed": 0, "conflicts": 0}
        if not rows:
            return stats

        active = [row for row in rows if row["is_indexable"] and not row["is_deleted"]]
        texts = []
        for row in active:
            if not row["text_blob_ref"]:
                raise ValueError("Current indexable document missing text blob")
            text = self.store.get(row["text_blob_ref"]).decode("utf-8")[:MAX_TEXT_CHARS]
            texts.append((row["title"] or "") + "\n" + text)
        vectors = self.embedder.encode(texts)
        if len(vectors) != len(active):
            raise ValueError("Embedding provider returned wrong batch size")
        by_id = {row["document_id"]: normalized_vector(vector)
                 for row, vector in zip(active, vectors)}

        actions = []
        for row in rows:
            is_active = bool(row["is_indexable"] and not row["is_deleted"])
            body = {
                "document_id": row["document_id"], "source_id": row["source_id"],
                "revision": row["revision"], "canonical_url": row["canonical_url"],
                "host": urlsplit(row["canonical_url"]).hostname or "",
                "title": row["title"] or "" if is_active else "",
                "description": row["description"] or "" if is_active else "",
                "language": row["language"] or "und" if is_active else "und",
                "visibility": "public", "is_indexable": is_active,
                "model_key": self.embedder.model_key, "updated_at_ms": row["updated_at_ms"],
            }
            if is_active:
                body["embedding"] = by_id[row["document_id"]]
            meta = {"index": {"_index": self.alias, "_id": row["document_id"],
                              "version": row["revision"], "version_type": "external_gte"}}
            actions.append((meta, body))

        responses = self.client.bulk(actions)
        for row, result in zip(rows, responses):
            if not isinstance(result, dict) or result.get("_id") != row["document_id"]:
                stats["failed"] += 1
                continue
            status = int(result.get("status", 0))
            if 200 <= status < 300:
                with self.db.transaction() as con:
                    current = con.execute("SELECT latest_revision FROM documents WHERE document_id=?",
                                          (row["document_id"],)).fetchone()
                    if current and current["latest_revision"] == row["revision"]:
                        con.execute("""INSERT INTO vector_receipts
                            (model_key,document_id,revision,indexed_at_ms)
                            VALUES(?,?,?,?)
                            ON CONFLICT(model_key,document_id) DO UPDATE SET
                            revision=excluded.revision,indexed_at_ms=excluded.indexed_at_ms
                            WHERE excluded.revision>=vector_receipts.revision""",
                            (self.embedder.model_key,row["document_id"],row["revision"],now))
                        stats["acked"] += 1
                    else:
                        stats["conflicts"] += 1
            elif status == 409:
                stats["conflicts"] += 1
            else:
                stats["failed"] += 1
        return stats
