"""Read-only bilingual BM25 search. No unauthenticated crawl admin endpoints."""
from __future__ import annotations
import os
from fastapi import FastAPI, HTTPException, Query
import sqlite3
from .db import Database
from .hybrid import HybridSearchService
from .semantic import SentenceTransformerEmbedder, CrossEncoderReranker
from .opensearch import OpenSearchClient, SearchBackendError
from .query import plan_query
from .search import SearchService

app = FastAPI(title="Search Engine", version="0.5.0",
              description="Public lexical + opt-in hybrid retrieval with version-aware vector results.")


@app.get("/health/live")
def live():
    return {"status": "alive", "phase": "hybrid-retrieval"}


@app.get("/v1/query/analyze")
def analyze(q: str = Query(min_length=1, max_length=200),
            language: str | None = None, source_id: str | None = None,
            rewrite_mode: str = "suggest"):
    try:
        return plan_query(q, language=language, source_id=source_id,
                          rewrite_mode=rewrite_mode).public_summary()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/v1/search")
def search(q: str = Query(min_length=1, max_length=200),
           limit: int = Query(default=10, ge=1, le=20),
           offset: int = Query(default=0, ge=0, le=200),
           language: str | None = None, source_id: str | None = None,
           rewrite_mode: str = "suggest", mode: str = "lexical",
           rerank: bool = False):
    try:
        client = OpenSearchClient(os.getenv("OPENSEARCH_URL", "http://opensearch:9200"),
                                  username=os.getenv("OPENSEARCH_USERNAME"),
                                  password=os.getenv("OPENSEARCH_PASSWORD"))
        if mode == "lexical" and not rerank:
            reply = SearchService(client).search(
                q, limit=limit, language=language, source_id=source_id,
                offset=offset, rewrite_mode=rewrite_mode)
            reply.update({"mode_requested": mode, "mode_actual": "lexical",
                          "degraded_components": []})
            return reply
        embedder = SentenceTransformerEmbedder(
            os.getenv("SEARCH_EMBEDDING_MODEL_ID", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"),
            os.getenv("SEARCH_EMBEDDING_MODEL_REVISION", "main"),
        ) if os.getenv("SEARCH_SEMANTIC_ENABLED") == "1" else None
        reranker = CrossEncoderReranker(
            os.getenv("SEARCH_RERANKER_MODEL_ID", "cross-encoder/ms-marco-MiniLM-L-6-v2"),
            os.getenv("SEARCH_RERANKER_MODEL_REVISION", "main"),
        ) if rerank and os.getenv("SEARCH_RERANK_ENABLED") == "1" else None
        db = Database(os.getenv("SEARCH_DATABASE_PATH", "/data/search.db"))
        return HybridSearchService(client, db, embedder, reranker).search(
            q, limit=limit, language=language, source_id=source_id,
            offset=offset, rewrite_mode=rewrite_mode, mode=mode, rerank=rerank)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(status_code=503, detail="Authoritative document database unavailable") from exc
    except SearchBackendError as exc:
        raise HTTPException(status_code=503, detail="Search backend unavailable") from exc
