"""Phase 5 vector projection, RRF, filters, tombstones, fallback and rerank tests.

Fake backend validates contracts; real OpenSearch kNN integration is separate.
Fake embeddings are NOT a semantic quality evaluation.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import tempfile
import unittest
import uuid

from test_phase3 import FakeOpenSearch
from search_engine.db import Database
from search_engine.frontier import Frontier
from search_engine.queue import Queue
from search_engine.processing import DocumentProcessor
from search_engine.store import BlobStore
from search_engine.indexing import OutboxIndexer
from search_engine.opensearch import READ_ALIAS, WRITE_ALIAS
from search_engine.semantic import (
    DIMENSIONS, VectorIndexManager, VectorProjector,
    VECTOR_READ_ALIAS, VECTOR_WRITE_ALIAS, model_fingerprint, normalized_vector,
)
from search_engine.hybrid import HybridSearchService, rrf_fuse, vector_search_query
from search_engine.search import search_query

ROOT = Path(__file__).resolve().parents[1]


class FakeEmbedder:
    dimension = DIMENSIONS

    def __init__(self, revision: str = "v1"):
        self.model_key = model_fingerprint("fixture-multilingual", revision)

    def encode(self, texts):
        vectors = []
        for text in texts:
            values = [0.0] * DIMENSIONS
            values[0 if any(x in text for x in ("中文", "检索", "搜索")) else 1] = 1.0
            vectors.append(values)
        return vectors


class FakeReranker:
    def score(self, q, docs):
        return [float(-n) for n, _ in enumerate(docs)]


class HybridFake(FakeOpenSearch):
    def __init__(self):
        super().__init__()
        self.last_vector_payload = None

    def request(self, method, path, body=None, *, ndjson=False):
        if method == "POST" and path == "/_aliases":
            for action in body["actions"]:
                name = action["add"]["alias"]
                self.aliases[name] = action["add"]["index"]
            return {"acknowledged": True}
        if path == f"/{VECTOR_READ_ALIAS}/_search":
            self.last_vector_payload = body
            index = self.indices[self.aliases[VECTOR_READ_ALIAS]]
            knn = body["query"]["knn"]["embedding"]
            vec = knn["vector"]
            filters = knn["filter"]["bool"]["filter"]
            hits = []
            for docid, raw in index.items():
                row = raw["_source"]
                if any(row.get(next(iter(f["term"]))) != next(iter(f["term"].values()))
                       if "term" in f else
                       not all(row.get("updated_at_ms", 0) >= bound if op == "gte"
                               else row.get("updated_at_ms", 0) < bound
                               for op, bound in f["range"]["updated_at_ms"].items())
                       for f in filters):
                    continue
                if "embedding" not in row:
                    continue
                similarity = sum(a*b for a,b in zip(row["embedding"],vec))
                hits.append({"_id": docid, "_source": row, "_score": similarity})
            hits.sort(key=lambda h: (-h["_score"], h["_id"]))
            return {"hits": {"hits": hits[:body["size"]]}}
        return super().request(method, path, body, ndjson=ndjson)


class Phase5Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = Database(root / "test.sqlite", ROOT / "schema/sqlite.sql")
        self.db.initialize()
        self.store = BlobStore(root / "raw")
        self.frontier = Frontier(self.db)
        self.frontier.create_source("demo", "https://example.org", now_ms=0)
        self.run = self.frontier.create_run("demo", now_ms=0)
        self.processor = DocumentProcessor(self.db, self.store)
        self.client = HybridFake()
        self.client.create_index("search-documents-v001",json.loads(
            (ROOT/"infra/opensearch/documents-v1.json").read_text()))
        self.client.ensure_initial_aliases("search-documents-v001")
        self.embedder = FakeEmbedder()
        self.vector_mgr = VectorIndexManager(
            self.client, ROOT / "infra/opensearch/vectors-v1.json"
        )
        self.vector_mgr.initialize()
        self.indexer = OutboxIndexer(self.db, self.store, self.client)
        self.projector = VectorProjector(self.db, self.store, self.client, self.embedder)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, path: str, title: str = "Search Guide", text: str = "Search tutorial",
            *, now: int = 100, status: int = 200, noindex: bool = False) -> str:
        url = f"https://example.org/{path}"
        url_id = self.frontier.add_url("demo", url, run_id=self.run, now_ms=now)
        markup = f'<html lang="zh"><title>{title}</title><p>{text}</p></html>'
        digest, raw = self.store.put(markup.encode()) if status == 200 else (None, None)
        fetch_id = str(uuid.uuid4())
        with self.db.transaction() as con:
            con.execute("""INSERT INTO fetches(
                fetch_id,url_id,run_id,fetched_at_ms,http_status,content_type,
                raw_sha256,raw_blob_ref,x_robots_tag
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (fetch_id,url_id,self.run,now,status,"text/html",digest,raw,
             "noindex" if noindex else ""))
            Queue.enqueue_tx(con,
                             "DELETE_DOCUMENT" if status in (404,410) else "EXTRACT_CONTENT",
                             fetch_id,f"proc:{fetch_id}",run_id=self.run,now_ms=now)
        return self.processor.run_once(now_ms=now+1)

    def index_and_project(self, *, now: int = 500):
        self.indexer.run_once(now_ms=now)
        return self.projector.sync_once(now_ms=now)

    def test_schema_v5_initializes_and_migrates_v4(self):
        with self.db.connect() as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 5)
        with self.db.transaction() as con:
            con.execute("DROP TABLE vector_receipts")
            con.execute("PRAGMA user_version=4")
        self.db.initialize()
        with self.db.connect() as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0],5)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM vector_receipts").fetchone()[0],0)

    def test_knn_mapping_and_aliases(self):
        m = json.loads((ROOT/"infra/opensearch/vectors-v1.json").read_text())
        self.assertEqual(m["mappings"]["properties"]["embedding"]["dimension"],384)
        self.assertEqual(m["mappings"]["properties"]["embedding"]["method"]["engine"],"lucene")
        self.assertEqual(self.client.alias_targets(VECTOR_READ_ALIAS),{"search-vectors-v001"})
        with self.assertRaises(ValueError):
            self.vector_mgr.initialize()

    def test_vector_normalization_rejects_nonfinite_and_zero(self):
        with self.assertRaises(ValueError):
            normalized_vector([0.0]*DIMENSIONS)
        with self.assertRaises(ValueError):
            normalized_vector([math.nan]+[0.0]*(DIMENSIONS-1))
        with self.assertRaises(ValueError):
            normalized_vector([1.0,0.0])
        self.assertAlmostEqual(normalized_vector([2.0]+[0.0]*(DIMENSIONS-1))[0],1.0)

    def test_rrf_determinism_and_duplicate_suppression(self):
        fused=rrf_fuse([["a","b","b"],["b","c"]])
        self.assertEqual([x[0] for x in fused],["b","a","c"])
        self.assertAlmostEqual(fused[0][1],1/62+1/61)
        with self.assertRaises(ValueError):
            rrf_fuse([["a"]],k=0)

    def test_projections_ack_latest_and_skip_unchanged(self):
        self.put("en","Search Document","Search tutorial")
        stats=self.index_and_project()
        self.assertEqual(stats["selected"],1)
        self.assertEqual(stats["acked"],1)
        self.assertEqual(self.projector.pending_count(),0)
        self.assertEqual(self.projector.sync_once()["selected"],0)
        doc=next(iter(self.client.indices["search-vectors-v001"].values()))
        self.assertEqual(len(doc["_source"]["embedding"]),DIMENSIONS)
        self.assertEqual(doc["_version"],1)

    def test_language_site_and_source_filters_inside_knn(self):
        self.put("zh","中文检索","搜索引擎与索引")
        self.index_and_project()
        result=HybridSearchService(self.client,self.db,self.embedder).search(
            "检索 lang:zh site:example.org source:demo",mode="hybrid")
        self.assertEqual(result["mode_actual"],"hybrid")
        payload=self.client.last_vector_payload
        filters=payload["query"]["knn"]["embedding"]["filter"]["bool"]["filter"]
        self.assertIn({"term":{"visibility":"public"}},filters)
        self.assertIn({"term":{"is_indexable":True}},filters)
        self.assertIn({"term":{"host":"example.org"}},filters)
        self.assertIn({"term":{"source_id":"demo"}},filters)
        self.assertIn({"term":{"language":"zh"}},filters)
        self.assertIn({"term":{"model_key":self.embedder.model_key}},filters)

    def test_hybrid_merges_two_routes_and_deduplicates(self):
        self.put("zh","中文检索","搜索引擎入门")
        self.put("en","Python Search","Python search tutorial",now=200)
        self.index_and_project()
        result=HybridSearchService(self.client,self.db,self.embedder).search("中文",mode="hybrid")
        ids=[x["document_id"] for x in result["results"]]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertEqual(result["total_relation"],"candidate_only")
        self.assertTrue(any("vector" in x["retrieval_sources"] for x in result["results"]))
        self.assertTrue(result["timings_ms"]["bm25"]>=0)

    def test_noindex_stale_vector_excluded_before_projection(self):
        self.put("zh","中文检索","搜索引擎入门")
        self.index_and_project()
        self.put("zh","中文检索","secret removed",now=600,noindex=True)
        # Both ANN and lexical remote data may be stale; SQLite fence must win.
        result=HybridSearchService(self.client,self.db,self.embedder).search("中文",mode="hybrid")
        self.assertEqual(result["results"],[])
        stats=self.index_and_project(now=1100)
        self.assertEqual(stats["acked"],1)
        v=next(iter(self.client.indices["search-vectors-v001"].values()))["_source"]
        self.assertFalse(v["is_indexable"])
        self.assertNotIn("embedding",v)

    def test_410_tombstone_prevents_stale_vector_upsert(self):
        self.put("zh","中文检索","searchable")
        self.index_and_project()
        self.put("zh","",status=410,now=600)
        self.index_and_project(now=1100)
        obj=next(iter(self.client.indices["search-vectors-v001"].items()))
        docid,record=obj
        self.assertEqual(record["_version"],2)
        old_meta={"index":{"_index":VECTOR_WRITE_ALIAS,"_id":docid,"version":1,"version_type":"external_gte"}}
        self.assertEqual(self.client.bulk([(old_meta,{"embedding":[1.0]*DIMENSIONS})])[0]["status"],409)

    def test_model_fingerprint_and_version_reproject(self):
        self.put("zh","中文检索","入门")
        self.index_and_project()
        next_model=FakeEmbedder("v2")
        self.assertNotEqual(next_model.model_key,self.embedder.model_key)
        sync=VectorProjector(self.db,self.store,self.client,next_model)
        self.assertEqual(sync.pending_count(),1)
        self.assertEqual(sync.sync_once()["acked"],1)
        row=next(iter(self.client.indices["search-vectors-v001"].values()))["_source"]
        self.assertEqual(row["model_key"],next_model.model_key)

    def test_malformed_bulk_response_never_ack(self):
        self.put("zh","中文检索","入门")
        self.client.fail_status={}
        original=self.client.bulk
        self.client.bulk=lambda actions: [{"_id":"wrong","status":200} for _ in actions]
        self.assertEqual(self.projector.sync_once()["failed"],1)
        self.assertEqual(self.projector.pending_count(),1)
        self.client.bulk=original
        self.assertEqual(self.projector.sync_once()["acked"],1)

    def test_explicit_receipt_reset_enables_full_reconciliation(self):
        self.put("en","Python Search","Search tutorial")
        self.index_and_project()
        self.assertEqual(self.projector.pending_count(),0)
        with self.assertRaises(ValueError):
            self.projector.reset_receipts()
        self.assertEqual(self.projector.reset_receipts(confirmed_quiescent=True),1)
        self.assertEqual(self.projector.pending_count(),1)
        self.assertEqual(self.projector.sync_once()["acked"],1)

    def test_missing_embedder_falls_back_clearly(self):
        self.put("en","Python Search","Search tutorial")
        self.index_and_project()
        reply=HybridSearchService(self.client,self.db,None).search("Search",mode="hybrid")
        self.assertEqual(reply["mode_actual"],"lexical")
        self.assertIn("embedding_model_not_configured",reply["degraded_components"])

    def test_structured_phrases_disable_ann(self):
        self.put("en","Search Guide","Search tutorial")
        self.index_and_project()
        self.client.last_vector_payload=None
        reply=HybridSearchService(self.client,self.db,self.embedder).search('"Search Guide" tutorial',mode="hybrid")
        self.assertEqual(reply["mode_actual"],"lexical")
        self.assertIn("vector_disabled_for_structured_query",reply["degraded_components"])
        self.assertIsNone(self.client.last_vector_payload)

    def test_reranker_guard_and_opt_in(self):
        self.put("en","Python Search","Python search tutorial")
        self.index_and_project()
        reply=HybridSearchService(self.client,self.db,self.embedder,FakeReranker()).search(
            "Search",mode="hybrid",rerank=True)
        self.assertTrue(reply["results"])
        self.assertIn("rerank_score",reply["results"][0])
        degraded=HybridSearchService(self.client,self.db,self.embedder).search(
            "Search",mode="hybrid",rerank=True)
        self.assertIn("reranker_not_configured",degraded["degraded_components"])

    def test_filtered_knn_does_not_accept_unbounded_candidate(self):
        with self.assertRaises(ValueError):
            vector_search_query([1.0]*DIMENSIONS,search_query("search"),k=21,
                                model_key=self.embedder.model_key)
        with self.assertRaises(ValueError):
            HybridSearchService(self.client,self.db,self.embedder).search("search",mode="hybrid",offset=3)


if __name__=="__main__":
    unittest.main()
