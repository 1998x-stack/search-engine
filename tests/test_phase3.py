"""Offline indexer, version fencing, recovery, rebuild and query contract tests."""
from __future__ import annotations
import json
from pathlib import Path
import sqlite3
from contextlib import closing
import tempfile
import unittest
import uuid

from search_engine.db import Database
from search_engine.frontier import Frontier
from search_engine.store import BlobStore
from search_engine.queue import Queue
from search_engine.processing import DocumentProcessor
from search_engine.indexing import OutboxIndexer,LEASE_MS,IndexLease
from search_engine.opensearch import OpenSearchClient,SearchBackendError,READ_ALIAS,WRITE_ALIAS
from search_engine.rebuild import IndexManager
from search_engine.search import SearchService,search_query

ROOT=Path(__file__).resolve().parents[1]

class FakeOpenSearch:
    """Models external_gte ordering and alias atomicity, not BM25 scoring."""
    def __init__(self):
        self.indices={};self.aliases={};self.fail_status={};self.broken=False
        self.calls=[];self.bulk_batches=[]

    def create_index(self,index,mapping):
        if index in self.indices:
            raise SearchBackendError('already exists',status=400)
        assert mapping['mappings']['dynamic']=='strict'
        self.indices[index]={}

    def ensure_initial_aliases(self,index):
        self.aliases={READ_ALIAS:index,WRITE_ALIAS:index}

    def alias_targets(self,alias):
        target=self.aliases.get(alias)
        return {target} if target else set()

    def swap_aliases(self,new,old):
        if self.alias_targets(READ_ALIAS)!=old or self.alias_targets(WRITE_ALIAS)!=old:
            raise ValueError('stale alias')
        self.aliases[READ_ALIAS]=new;self.aliases[WRITE_ALIAS]=new

    def bulk(self,actions):
        self.bulk_batches.append(len(actions))
        if self.broken:
            raise SearchBackendError('backend unavailable',status=503,transient=True)
        results=[]
        for meta,body in actions:
            request=meta['index'];alias=request['_index'];index=self.aliases.get(alias,alias)
            docid=request['_id'];rev=request['version']
            forced=self.fail_status.get(docid)
            if forced:
                results.append({'_id':docid,'status':forced})
                continue
            previous=self.indices[index].get(docid)
            if previous and previous['_version']>rev:
                results.append({'_id':docid,'status':409})
                continue
            self.indices[index][docid]={'_source':body,'_version':rev}
            results.append({'_id':docid,'status':201 if previous is None else 200})
        return results

    def request(self,method,path,body=None,*,ndjson=False):
        self.calls.append((method,path,body))
        if path.endswith('/_refresh'):
            return {'_shards':{'successful':1}}
        if path.endswith('/_count'):
            return {'count':len(self.indices[path.split('/')[1]])}
        if path.endswith('/_search'):
            index=self.indices[self.aliases[path.split('/')[1]]]
            q=body['query']['bool']
            filters=q['filter'];entries=[]
            for docid,doc in index.items():
                src=doc['_source']
                if any(src.get(list(f['term'].keys())[0])!=list(f['term'].values())[0] for f in filters):
                    continue
                word=q['should'][0]['multi_match']['query'].lower()
                if word in (src.get('title','')+' '+src.get('content','')).lower():
                    entries.append({'_id':docid,'_source':src,'_score':1.0})
            return {'hits':{'total':{'value':len(entries)},'hits':entries[:body['size']]}}
        raise RuntimeError(f'Unexpected {method} {path}')

class Phase3Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name)
        self.db=Database(root/'meta.sqlite',ROOT/'schema/sqlite.sql')
        self.db.initialize()
        self.store=BlobStore(root/'raw')
        self.frontier=Frontier(self.db)
        self.frontier.create_source('example','https://example.org',now_ms=0)
        self.run=self.frontier.create_run('example',now_ms=0)
        self.processor=DocumentProcessor(self.db,self.store)
        self.remote=FakeOpenSearch()
        self.remote.create_index('search-documents-v001',json.loads((ROOT/'infra/opensearch/documents-v1.json').read_text()))
        self.remote.ensure_initial_aliases('search-documents-v001')
        self.indexer=OutboxIndexer(self.db,self.store,self.remote)

    def tearDown(self):
        self.temp.cleanup()

    def put(self,url,content,*,status=200,now=100,robots=''):
        url_id=self.frontier.add_url('example',url,run_id=self.run,now_ms=now)
        digest,blob=self.store.put(content.encode()) if status==200 else (None,None)
        f_id=str(uuid.uuid4())
        with self.db.transaction() as con:
            con.execute('''INSERT INTO fetches(fetch_id,url_id,run_id,fetched_at_ms,http_status,
                content_type,raw_sha256,raw_blob_ref,x_robots_tag) VALUES(?,?,?,?,?,?,?,?,?)''',
                (f_id,url_id,self.run,now,status,'text/html',digest,blob,robots))
            Queue.enqueue_tx(con,'DELETE_DOCUMENT' if status in (404,410) else 'EXTRACT_CONTENT',
                             f_id,f'proc:{f_id}',run_id=self.run,now_ms=now)
        return self.processor.run_once(now_ms=now+1)

    def test_v4_schema_created(self):
        with self.db.connect() as con:
            self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0],5)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM index_receipts').fetchone()[0],0)

    def test_v3_to_v4_migration_preserves_docs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'old.sqlite'
            with closing(sqlite3.connect(path)) as con:
                con.executescript((ROOT/'migrations/fixture_phase2_schema.sql').read_text())
                self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0],3)
            Database(path,ROOT/'schema/sqlite.sql').initialize()
            with closing(sqlite3.connect(path)) as con:
                self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0],5)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM index_receipts").fetchone()[0],0)

    def test_versioned_upsert_and_search(self):
        self.assertEqual(self.put('https://example.org/a','<title>Search Engine Basics</title><p>Inverted index retrieval in English.</p>'),'created')
        stats=self.indexer.run_once(now_ms=500)
        self.assertEqual((stats['acked'],stats['retry']),(1,0))
        service=SearchService(self.remote)
        results=service.search('search')
        self.assertEqual(results['total'],1)
        self.assertIn('Search',results['results'][0]['title'])
        self.assertEqual(self.indexer.audit_local()['lagging'],0)

    def test_noindex_tombstone_not_searchable(self):
        self.put('https://example.org/a','<title>Unique Thing</title><p>Original searchable content</p>')
        self.indexer.run_once(now_ms=500)
        self.put('https://example.org/a','<title>Unique Thing</title><p>Noindex secret content</p>',now=600,robots='noindex')
        self.assertEqual(self.indexer.run_once(now_ms=1000)['acked'],1)
        self.assertEqual(SearchService(self.remote).search('Unique')['total'],0)
        doc=list(self.remote.indices[self.remote.aliases[READ_ALIAS]].values())[0]
        self.assertFalse(doc['_source']['is_indexable'])
        self.assertEqual(doc['_source']['content'],'')
        self.assertEqual(doc['_version'],2)

    def test_410_tombstone_blocks_delayed_old_upsert(self):
        self.put('https://example.org/a','<title>Will be removed</title><p>secret text</p>')
        self.indexer.run_once(now_ms=500)
        self.put('https://example.org/a','',status=410,now=600)
        self.indexer.run_once(now_ms=1000)
        docid=next(iter(self.remote.indices['search-documents-v001']))
        old_meta={'index':{'_index':WRITE_ALIAS,'_id':docid,'version':1,'version_type':'external_gte'}}
        result=self.remote.bulk([(old_meta,{'content':'resurrect!'})])[0]
        self.assertEqual(result['status'],409)
        self.assertEqual(SearchService(self.remote).search('secret')['total'],0)
        self.assertEqual(self.indexer.audit_local()['lagging'],0)

    def test_old_pending_revision_skipped(self):
        self.put('https://example.org/a','<title>Old</title><p>Old content</p>')
        self.put('https://example.org/a','<title>New</title><p>New content</p>',now=200)
        stats=self.indexer.run_once(now_ms=1000)
        self.assertEqual((stats['acked'],stats['superseded']),(1,1))
        only=next(iter(self.remote.indices['search-documents-v001'].values()))
        self.assertEqual(only['_version'],2)

    def test_partial_bulk_retry_and_eventual_ack(self):
        self.put('https://example.org/a','<title>Good</title><p>content alpha</p>')
        self.put('https://example.org/b','<title>Retry</title><p>content beta</p>',now=200)
        with self.db.connect() as con:
            ids=[r['document_id'] for r in con.execute('SELECT document_id FROM documents ORDER BY canonical_url')]
        self.remote.fail_status[ids[1]]=429
        stats=self.indexer.run_once(now_ms=500)
        self.assertEqual((stats['acked'],stats['retry']),(1,1))
        del self.remote.fail_status[ids[1]]
        self.assertEqual(self.indexer.run_once(now_ms=2500)['acked'],1)
        self.assertEqual(self.indexer.audit_local()['lagging'],0)

    def test_bulk_backend_offline_retries(self):
        self.put('https://example.org/a','<title>Offline</title><p>content</p>')
        self.remote.broken=True
        self.assertEqual(self.indexer.run_once(now_ms=500)['retry'],1)
        self.remote.broken=False
        self.assertEqual(self.indexer.run_once(now_ms=2500)['acked'],1)

    def test_permanent_mapping_error_dead_and_manual_retry(self):
        self.put('https://example.org/a','<title>Bad</title><p>content</p>')
        with self.db.connect() as con:
            docid=con.execute('SELECT document_id FROM documents').fetchone()[0]
        self.remote.fail_status[docid]=400
        self.assertEqual(self.indexer.run_once(now_ms=500)['dead'],1)
        self.assertEqual(self.indexer.audit_local()['dead'],1)
        del self.remote.fail_status[docid]
        self.assertEqual(self.indexer.retry_dead(now_ms=1000),1)
        self.assertEqual(self.indexer.run_once(now_ms=1000)['acked'],1)

    def test_lease_expiry_fencing(self):
        self.put('https://example.org/a','<title>Timeout</title><p>content</p>')
        first=self.indexer.claim_batch(now_ms=1000)[0]
        second=self.indexer.claim_batch(now_ms=1000+LEASE_MS+1)[0]
        self.assertNotEqual(first.lease_epoch,second.lease_epoch)
        with self.assertRaises(RuntimeError):
            self.indexer._finish(first,now=1000+LEASE_MS+2,success=True)
        self.indexer._finish(second,now=1000+LEASE_MS+2,success=True)

    def test_duplicate_replay_external_gte(self):
        self.put('https://example.org/a','<title>Replay</title><p>content</p>')
        first=self.indexer.claim_batch(now_ms=1000)[0]
        meta,body=self.indexer._materialize(first,1000)
        self.assertEqual(self.remote.bulk([(meta,body)])[0]['status'],201)
        # Simulate crash before the SQLite ACK.
        self.assertEqual(self.indexer.run_once(now_ms=1000+LEASE_MS+1)['acked'],1)
        self.assertEqual(self.indexer.audit_local()['lagging'],0)

    def test_rebuild_offline_atomic_alias_swap(self):
        self.put('https://example.org/a','<title>Apple Search</title><p>Search index document</p>')
        self.indexer.run_once(now_ms=500)
        mgr=IndexManager(self.db,self.store,self.remote,ROOT/'infra/opensearch/documents-v1.json')
        with self.assertRaises(ValueError):
            mgr.rebuild_offline('search-documents-v002')
        result=mgr.rebuild_offline('search-documents-v002',confirmed_quiescent=True)
        self.assertEqual(result['documents'],1)
        self.assertEqual(self.remote.alias_targets(READ_ALIAS),{'search-documents-v002'})
        self.assertEqual(SearchService(self.remote).search('Apple')['total'],1)

    def test_rebuild_splits_bulk_by_byte_budget(self):
        from unittest.mock import patch
        self.put('https://example.org/a','<title>Alpha</title><p>'+('a'*650)+'</p>')
        self.put('https://example.org/b','<title>Beta</title><p>'+('b'*650)+'</p>',now=200)
        self.indexer.run_once(now_ms=1000)
        self.remote.bulk_batches.clear()
        manager=IndexManager(self.db,self.store,self.remote,ROOT/'infra/opensearch/documents-v1.json')
        with patch('search_engine.rebuild.REBUILD_BATCH_BYTES',1700):
            result=manager.rebuild_offline('search-documents-v004',confirmed_quiescent=True)
        self.assertEqual(result['documents'],2)
        self.assertEqual(self.remote.bulk_batches,[1,1])

    def test_rebuild_rejects_pending_outbox(self):
        self.put('https://example.org/a','<title>Pending</title><p>content</p>')
        mgr=IndexManager(self.db,self.store,self.remote,ROOT/'infra/opensearch/documents-v1.json')
        with self.assertRaises(ValueError):
            mgr.rebuild_offline('search-documents-v003',confirmed_quiescent=True)
        self.assertEqual(self.remote.alias_targets(READ_ALIAS),{'search-documents-v001'})

    def test_query_validation_hard_filters(self):
        payload=search_query('中文 检索',language='zh',source_id='example')
        filters=payload['query']['bool']['filter']
        self.assertIn({'term':{'is_indexable':True}},filters)
        self.assertIn({'term':{'visibility':'public'}},filters)
        self.assertIn({'term':{'language':'zh'}},filters)
        self.assertIn({'term':{'source_id':'example'}},filters)
        for q in ('','\n','a'*201):
            with self.assertRaises(ValueError): search_query(q)
        with self.assertRaises(ValueError): search_query('safe',language='ANYTHING')
        with self.assertRaises(ValueError): search_query('safe',offset=9999)

    def test_auth_requires_https(self):
        with self.assertRaises(ValueError):
            OpenSearchClient('http://localhost:9200',username='admin',password='secret')

if __name__=='__main__':
    unittest.main()
