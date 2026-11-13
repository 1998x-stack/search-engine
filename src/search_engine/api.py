"""Public read-only lexical search; no unauthenticated crawl administration routes."""
from __future__ import annotations
import os
from fastapi import FastAPI, HTTPException, Query
from .opensearch import OpenSearchClient, SearchBackendError
from .search import SearchService

app=FastAPI(title='Search Engine',version='0.3.0',
            description='P0 public-only BM25; hybrid retrieval not implemented.')

@app.get('/health/live')
def live():
    return {'status':'alive','phase':'lexical-indexing'}

@app.get('/v1/search')
def search(q:str=Query(min_length=1,max_length=200),
           limit:int=Query(default=10,ge=1,le=20),
           offset:int=Query(default=0,ge=0,le=200),
           language:str|None=None,source_id:str|None=None):
    try:
        client=OpenSearchClient(os.getenv('OPENSEARCH_URL','http://opensearch:9200'),
                                username=os.getenv('OPENSEARCH_USERNAME'),
                                password=os.getenv('OPENSEARCH_PASSWORD'))
        return SearchService(client).search(q,limit=limit,language=language,
                                            source_id=source_id,offset=offset)
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc)) from exc
    except SearchBackendError as exc:
        raise HTTPException(status_code=503,detail='Search backend unavailable') from exc
