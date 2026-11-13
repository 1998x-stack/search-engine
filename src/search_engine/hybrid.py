"""Hybrid BM25 + filtered kNN retrieval and RRF fusion.

All vector candidates are reconciled against authoritative SQLite latest
revision before presentation. Structured phrase/negative queries are kept
lexical-only instead of weakening their semantics through ANN.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from time import monotonic
from typing import Protocol

from .db import Database
from .opensearch import OpenSearchClient, SearchBackendError, READ_ALIAS, require_index_name
from .query import plan_query
from .search import SearchService, search_query
from .semantic import (
    DIMENSIONS, Embedder, SemanticUnavailable, VECTOR_READ_ALIAS,
    normalized_vector,
)

CANDIDATE_K = 20
RRF_K = 60


class Reranker(Protocol):
    def score(self, query: str, documents: list[str]) -> list[float]: ...


def rrf_fuse(ranked_lists: list[list[str]], *, k: int = RRF_K) -> list[tuple[str, float]]:
    """Deterministic Reciprocal Rank Fusion with duplicate suppression per list."""
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ValueError("RRF k must be a positive integer")
    scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    for ranked in ranked_lists:
        seen = set()
        for i, document_id in enumerate(ranked, 1):
            if not isinstance(document_id, str) or not document_id or document_id in seen:
                continue
            seen.add(document_id)
            scores[document_id] = scores.get(document_id, 0.0) + 1.0 / (k + i)
            best_rank[document_id] = min(best_rank.get(document_id, i), i)
    return sorted(scores.items(), key=lambda v: (-v[1], best_rank[v[0]], v[0]))


def vector_search_query(vector: list[float], lexical_payload: dict, *, k: int,
                        model_key: str) -> dict:
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= CANDIDATE_K:
        raise ValueError("Vector candidate budget outside supported range")
    if len(model_key) != 64 or any(c not in "0123456789abcdef" for c in model_key):
        raise ValueError("Invalid model_key")
    hard = list(lexical_payload["query"]["bool"]["filter"])
    hard.append({"term": {"model_key": model_key}})
    return {
        "size": k,
        "track_total_hits": False,
        "_source": ["document_id", "source_id", "revision", "title", "description",
                    "canonical_url", "language", "updated_at_ms"],
        "query": {
            "knn": {
                "embedding": {
                    "vector": normalized_vector(vector, dimension=DIMENSIONS),
                    "k": k,
                    "filter": {"bool": {"filter": hard}},
                },
            },
        },
    }


@dataclass
class HybridSearchService:
    client: OpenSearchClient
    db: Database
    embedder: Embedder | None
    reranker: Reranker | None = None
    vector_read_alias: str = VECTOR_READ_ALIAS
    lexical_read_alias: str = READ_ALIAS

    def __post_init__(self):
        self.vector_read_alias = require_index_name(self.vector_read_alias)
        self.lexical_read_alias = require_index_name(self.lexical_read_alias)

    def _authoritative_valid(self, candidates: dict[str, dict], plan) -> dict[str, dict]:
        if not candidates:
            return {}
        with self.db.connect() as con:
            permitted = {}
            for doc_id, record in candidates.items():
                row = con.execute("""SELECT d.latest_revision,d.is_deleted,d.source_id,
                    d.canonical_url,d.updated_at_ms,
                    v.is_indexable,v.language
                    FROM documents d JOIN document_versions v
                      ON v.document_id=d.document_id AND v.revision=d.latest_revision
                    WHERE d.document_id=?""", (doc_id,)).fetchone()
                if not row or not row["is_indexable"] or row["is_deleted"]:
                    continue
                # A vector revision must match authoritative latest; stale
                # lexical docs with unknown revisions are also rejected.
                if record.get("revision") != row["latest_revision"]:
                    continue
                if plan.source_id is not None and row["source_id"] != plan.source_id:
                    continue
                if plan.language is not None and row["language"] != plan.language:
                    continue
                from urllib.parse import urlsplit
                if plan.host is not None and urlsplit(row["canonical_url"]).hostname != plan.host:
                    continue
                if plan.after_ms is not None and row["updated_at_ms"] < plan.after_ms:
                    continue
                if plan.before_ms is not None and row["updated_at_ms"] >= plan.before_ms:
                    continue
                permitted[doc_id] = record
        return permitted

    def search(self, q: str, *, limit: int = 10, language: str | None = None,
               source_id: str | None = None, offset: int = 0,
               rewrite_mode: str = "suggest",
               mode: str = "hybrid", rerank: bool = False) -> dict:
        if mode not in ("lexical", "hybrid"):
            raise ValueError("Search mode must be lexical or hybrid")
        if rerank and mode != "hybrid":
            raise ValueError("Rerank requires hybrid mode")
        if mode == "hybrid" and offset != 0:
            raise ValueError("Hybrid paging requires offset=0; deep paging is not yet supported")
        plan = plan_query(q, language=language, source_id=source_id,
                          rewrite_mode=rewrite_mode)
        lexical_service = SearchService(self.client, self.lexical_read_alias)
        if mode == "lexical":
            response = lexical_service.search(q, limit=limit, offset=offset,
                                               language=language, source_id=source_id,
                                               rewrite_mode=rewrite_mode)
            response.update({"mode_requested": mode, "mode_actual": "lexical",
                             "degraded_components": []})
            return response

        t0 = monotonic()
        lexical = lexical_service.search(q, limit=CANDIDATE_K, offset=0,
                                         language=language, source_id=source_id,
                                         rewrite_mode=rewrite_mode)
        lexical_ms = round((monotonic() - t0) * 1000, 2)
        warnings: list[str] = []
        vector_hits: list[dict] = []
        vector_ms = 0.0
        if plan.phrases or plan.excluded:
            warnings.append("vector_disabled_for_structured_query")
        elif self.embedder is None:
            warnings.append("embedding_model_not_configured")
        else:
            started = monotonic()
            try:
                if self.embedder.dimension != DIMENSIONS:
                    raise SemanticUnavailable("Embedding dimension mismatch")
                batch = self.embedder.encode([plan.retrieval_text])
                if len(batch) != 1:
                    raise ValueError("Query embedding size mismatch")
                payload = vector_search_query(
                    batch[0],
                    search_query(q, limit=CANDIDATE_K, offset=0, language=language,
                                 source_id=source_id, rewrite_mode=rewrite_mode),
                    k=CANDIDATE_K, model_key=self.embedder.model_key,
                )
                found = self.client.request(
                    "POST", f"/{self.vector_read_alias}/_search", payload
                )
                vector_hits = found.get("hits", {}).get("hits", [])
            except (SemanticUnavailable, SearchBackendError, ValueError, OSError):
                warnings.append("vector_retrieval_unavailable")
            vector_ms = round((monotonic() - started) * 1000, 2)

        candidates: dict[str, dict] = {}
        lexical_ids = []
        for row in lexical["results"]:
            doc_id = row.get("document_id")
            if isinstance(doc_id, str) and doc_id:
                lexical_ids.append(doc_id)
                candidates[doc_id] = {**row, "retrieval_sources": ["bm25"]}
        vector_ids = []
        for hit in vector_hits[:CANDIDATE_K]:
            source = hit.get("_source") or {}
            doc_id = source.get("document_id") or hit.get("_id")
            revision = source.get("revision")
            if not isinstance(doc_id, str) or not isinstance(revision, int):
                continue
            vector_ids.append(doc_id)
            if doc_id in candidates:
                candidates[doc_id]["retrieval_sources"].append("vector")
            else:
                candidates[doc_id] = {
                    "document_id": doc_id, "title": source.get("title", ""),
                    "url": source.get("canonical_url", ""),
                    "source_id": source.get("source_id", ""),
                    "language": source.get("language", "und"),
                    "revision": revision, "score": hit.get("_score", 0),
                    "snippet": source.get("description", ""),
                    "retrieval_sources": ["vector"],
                }

        candidates = self._authoritative_valid(candidates, plan)
        lexical_ids = [doc_id for doc_id in lexical_ids if doc_id in candidates]
        vector_ids = [doc_id for doc_id in vector_ids if doc_id in candidates]
        fused = rrf_fuse([lexical_ids, vector_ids])
        ranked = []
        for doc_id, rrf_score in fused:
            row = dict(candidates[doc_id])
            row["rrf_score"] = rrf_score
            row["score"] = rrf_score
            ranked.append(row)

        rerank_ms = 0.0
        if rerank and ranked:
            if self.reranker is None:
                warnings.append("reranker_not_configured")
            else:
                started = monotonic()
                shortlist = ranked[:CANDIDATE_K]
                try:
                    values = self.reranker.score(q, [
                        (row["title"] + "\n" + row["snippet"])[:1500] for row in shortlist
                    ])
                    if len(values) != len(shortlist) or any(not isfinite(float(x)) for x in values):
                        raise ValueError("Reranker produced invalid scores")
                    for row, score in zip(shortlist, values):
                        row["rerank_score"] = float(score)
                    shortlist.sort(key=lambda row: (-row["rerank_score"],
                                                    -row["rrf_score"], row["document_id"]))
                    ranked = shortlist + ranked[CANDIDATE_K:]
                except (SemanticUnavailable, ValueError, OSError):
                    warnings.append("reranker_unavailable")
                rerank_ms = round((monotonic() - started) * 1000, 2)

        mode_actual = "hybrid" if vector_hits else "lexical"
        if warnings and mode_actual == "lexical":
            # Never label lexical fallback as successful hybrid retrieval.
            warnings = list(dict.fromkeys(warnings))
        return {
            "query": q, "offset": 0, "limit": limit,
            "total": len(ranked), "total_relation": "candidate_only",
            "lexical_total": lexical["total"],
            "mode_requested": mode, "mode_actual": mode_actual,
            "analysis": plan.public_summary(),
            "degraded_components": warnings,
            "timings_ms": {"bm25": lexical_ms, "vector": vector_ms,
                           "rerank": rerank_ms},
            "results": ranked[:limit],
        }
