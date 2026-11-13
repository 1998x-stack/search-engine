#!/usr/bin/env python3
"""OpenSearch 3.9 container smoke: real index, analyzers, bulk, search, tombstone, rebuild.
Only runs against an explicit disposable local test OpenSearch cluster.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import urlopen
from pathlib import Path
import tempfile
import uuid

from search_engine.db import Database
from search_engine.frontier import Frontier
from search_engine.processing import DocumentProcessor
from search_engine.queue import Queue
from search_engine.store import BlobStore
from search_engine.indexing import OutboxIndexer
from search_engine.rebuild import IndexManager
from search_engine.search import SearchService
from search_engine.semantic import (VectorIndexManager, VectorProjector,
                                    model_fingerprint, DIMENSIONS,
                                    VECTOR_READ_ALIAS, VECTOR_WRITE_ALIAS)
from search_engine.hybrid import HybridSearchService
from search_engine.opensearch import OpenSearchClient,READ_ALIAS,WRITE_ALIAS

ROOT=Path(__file__).resolve().parents[1]
ENDPOINT=os.environ.get('OPENSEARCH_URL','')
if not ENDPOINT or not any(x in ENDPOINT for x in ('127.0.0.1:9200','localhost:9200')):
    raise SystemExit('Live test requires explicit loopback OPENSEARCH_URL on disposable cluster')
client=OpenSearchClient(ENDPOINT)
with tempfile.TemporaryDirectory() as tmp:
    db=Database(Path(tmp)/'index.sqlite',ROOT/'schema/sqlite.sql');db.initialize()
    store=BlobStore(Path(tmp)/'raw')
    front=Frontier(db);front.create_source('test','https://example.org',now_ms=0)
    run=front.create_run('test',now_ms=0)
    proc=DocumentProcessor(db,store)
    def add(path,html,status=200,at=100):
        url_id=front.add_url('test','https://example.org/'+path,run_id=run,now_ms=at)
        raw=html.encode('utf-8')
        digest,blob=store.put(raw) if status==200 else (None,None)
        fetch=str(uuid.uuid4())
        with db.transaction() as con:
            con.execute('''INSERT INTO fetches(fetch_id,url_id,run_id,fetched_at_ms,http_status,
                content_type,raw_sha256,raw_blob_ref) VALUES(?,?,?,?,?,?,?,?)''',
                (fetch,url_id,run,at,status,'text/html',digest,blob))
            Queue.enqueue_tx(con,'DELETE_DOCUMENT' if status in (404,410) else 'EXTRACT_CONTENT',
                             fetch,'process:'+fetch,run_id=run,now_ms=at)
        return proc.run_once(now_ms=at+1)
    name='search-documents-v001'
    manager=IndexManager(db,store,client,ROOT/'infra/opensearch/documents-v1.json')
    manager.initialize(name)
    cn=client.request('POST',f'/{name}/_analyze',{'analyzer':'cjk','text':'中文检索引擎'})
    en=client.request('POST',f'/{name}/_analyze',{'analyzer':'english','text':'Searching documents'})
    assert cn.get('tokens') and en.get('tokens'),(cn,en)
    assert add('zh','<html lang="zh"><title>中文检索指南</title><p>倒排索引和搜索引擎入门</p></html>')=='created'
    assert add('en','<html lang="en"><title>Search Engine Guide</title><p>Inverted index and ranking tutorial</p></html>',at=200)=='created'
    class TestEmbedder:
        # A deterministic test double proves OpenSearch kNN integration, NOT semantic quality.
        dimension = DIMENSIONS
        model_key = model_fingerprint('fixture-multilingual-vectors','v1')
        def encode(self, texts):
            result=[]
            for text in texts:
                vector=[0.0]*DIMENSIONS
                vector[0 if ('中文' in text or '检索' in text) else 1]=1.0
                result.append(vector)
            return result
    vector_manager = VectorIndexManager(client,ROOT/'infra/opensearch/vectors-v1.json')
    vector_manager.initialize()
    projector = VectorProjector(db,store,client,TestEmbedder())
    idx=OutboxIndexer(db,store,client)
    assert idx.run_once(now_ms=500)['acked']==2
    client.request('POST',f'/{name}/_refresh')
    assert SearchService(client).search('检索',language='zh')['total']>=1
    assert SearchService(client).search('ranking',language='en')['total']>=1
    assert projector.sync_once(now_ms=550)['acked']==2
    client.request('POST','/search-vectors-v001/_refresh')
    hybrid_zh = HybridSearchService(client,db,TestEmbedder()).search('检索',mode='hybrid')
    assert hybrid_zh['results'] and hybrid_zh['mode_actual']=='hybrid', hybrid_zh
    assert any('vector' in row['retrieval_sources'] for row in hybrid_zh['results']), hybrid_zh
    hybrid_en = HybridSearchService(client,db,TestEmbedder()).search(
        'Search site:example.org',language='en',mode='hybrid')
    assert hybrid_en['results'],hybrid_en
    assert HybridSearchService(client,db,TestEmbedder()).search(
        'Search site:other.example',language='en',mode='hybrid')['results']==[]
    assert SearchService(client).search('Search site:example.org',language='en')['total']>=1
    assert SearchService(client).search('Search site:other.example',language='en')['total']==0
    assert SearchService(client).search('"Search Engine" guide',language='en')['total']>=1
    assert SearchService(client).search('中文分词',rewrite_mode='expand')['analysis']['detected_language']=='zh'
    assert add('en','',status=410,at=600)=='deleted'
    # Before remote tombstone catches up, authoritative SQLite fencing wins.
    assert HybridSearchService(client,db,TestEmbedder()).search(
        'ranking',language='en',mode='hybrid')['results']==[]
    assert idx.run_once(now_ms=1000)['acked']==1
    assert projector.sync_once(now_ms=1000)['acked']==1
    client.request('POST','/search-vectors-v001/_refresh')
    client.request('POST',f'/{name}/_refresh')
    assert SearchService(client).search('ranking',language='en')['total']==0
    assert HybridSearchService(client,db,TestEmbedder()).search(
        'ranking',language='en',mode='hybrid')['results']==[]
    assert projector.pending_count()==0
    assert idx.audit_local()['lagging']==0
    result=manager.rebuild_offline('search-documents-v002',confirmed_quiescent=True)
    assert result['documents']==2
    assert client.alias_targets(READ_ALIAS)=={'search-documents-v002'}
    assert client.alias_targets(WRITE_ALIAS)=={'search-documents-v002'}
    assert SearchService(client).search('检索',language='zh')['total']>=1
    assert SearchService(client).search('ranking',language='en')['total']==0
    # Exercise the actual FastAPI/uvicorn boundary, not only SearchService.
    api=subprocess.Popen([sys.executable,'-m','uvicorn','search_engine.api:app',
                          '--host','127.0.0.1','--port','18089'],
                          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                          env={**os.environ,'SEARCH_DATABASE_PATH':str(db.path),
                               'SEARCH_SEMANTIC_ENABLED':'0'})
    try:
        payload=None
        for _ in range(40):
            if api.poll() is not None:
                raise RuntimeError('FastAPI process exited before startup')
            try:
                url='http://127.0.0.1:18089/v1/search?'+urlencode({'q':'检索','language':'zh'})
                with urlopen(url,timeout=2) as response:
                    payload=json.loads(response.read())
                break
            except OSError:
                time.sleep(0.25)
        assert payload and payload.get('total',0)>=1,payload
        # API must clearly report a lexical fallback when no ML model is provisioned.
        fallback_url='http://127.0.0.1:18089/v1/search?'+urlencode(
            {'q':'检索','language':'zh','mode':'hybrid'})
        with urlopen(fallback_url,timeout=3) as response:
            fallback=json.loads(response.read())
        assert fallback['mode_requested']=='hybrid' and fallback['mode_actual']=='lexical',fallback
        assert 'embedding_model_not_configured' in fallback['degraded_components'],fallback
        assert fallback['results'],fallback
    finally:
        api.terminate()
        api.wait(timeout=5)
    print(json.dumps({'live_opensearch':'PASS','reindexed_documents':result['documents'],
                      'chinese_tokens':len(cn['tokens']),'english_tokens':len(en['tokens']),'vector_projection':'PASS','knn_dimension':DIMENSIONS}))
