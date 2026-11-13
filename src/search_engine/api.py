"""Read-only bilingual BM25 search. No unauthenticated crawl admin endpoints."""
from __future__ import annotations
import os
from fastapi import FastAPI, HTTPException, Query
from .opensearch import OpenSearchClient, SearchBackendError
from .query import plan_query
from .search import SearchService

app = FastAPI(title="Search Engine", version="0.4.0",
              description="Public BM25 search, validated multilingual query parsing and suggestions.")


@app.get("/health/live")
def live():
    return {"status": "alive", "phase": "query-understanding"}


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
           rewrite_mode: str = "suggest"):
    try:
        client = OpenSearchClient(os.getenv("OPENSEARCH_URL", "http://opensearch:9200"),
                                  username=os.getenv("OPENSEARCH_USERNAME"),
                                  password=os.getenv("OPENSEARCH_PASSWORD"))
        return SearchService(client).search(
            q, limit=limit, language=language, source_id=source_id,
            offset=offset, rewrite_mode=rewrite_mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SearchBackendError as exc:
        raise HTTPException(status_code=503, detail="Search backend unavailable") from exc
