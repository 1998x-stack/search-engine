"""Phase 2 processor + durable outbox tests (offline, no OpenSearch network)."""
from __future__ import annotations
from pathlib import Path
import sqlite3
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
import uuid

from search_engine.db import Database
from search_engine.dedup import shingles, fingerprint, near_match
from search_engine.extract import extract
from search_engine.frontier import Frontier
from search_engine.processing import DocumentProcessor
from search_engine.queue import Queue
from search_engine.store import BlobStore

SCHEMA = Path(__file__).resolve().parents[1] / 'schema/sqlite.sql'
PHASE1_SCHEMA = Path(__file__).resolve().parents[1] / 'migrations' / 'fixture_phase1_schema.sql'


class ProcessorTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.dir.name)/'crawler.sqlite',SCHEMA)
        self.db.initialize()
        self.store = BlobStore(Path(self.dir.name)/'raw')
        self.frontier = Frontier(self.db)
        self.frontier.create_source('s','https://example.org',now_ms=0)
        self.run = self.frontier.create_run('s',now_ms=0)
        self.processor = DocumentProcessor(self.db,self.store)

    def tearDown(self):
        self.dir.cleanup()

    def add_fetch(self,url: str,content: str | bytes = '',status: int = 200,
                  *, now: int = 100,kind: str = 'text/html', x_robots_tag: str = '',
                  run_id: str | None = None) -> str:
        run_id = run_id or self.run
        url_id = self.frontier.add_url('s',url,run_id=run_id,now_ms=now)
        raw = content.encode('utf-8') if isinstance(content,str) else content
        digest,blob_ref = self.store.put(raw) if status == 200 else (None,None)
        fetch_id = str(uuid.uuid4())
        with self.db.transaction() as con:
            con.execute('''INSERT INTO fetches
                (fetch_id,url_id,run_id,fetched_at_ms,http_status,content_type,
                 raw_sha256,raw_blob_ref,x_robots_tag)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                (fetch_id,url_id,run_id,now,status,kind,digest,blob_ref,x_robots_tag))
            task = 'DELETE_DOCUMENT' if status in (404,410) else 'EXTRACT_CONTENT'
            Queue.enqueue_tx(con,task,fetch_id,f'proc:{fetch_id}',run_id=run_id,now_ms=now)
        return fetch_id

    def row_counts(self) -> tuple[int,int,int]:
        with self.db.connect() as con:
            return tuple(con.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]
                         for name in ('documents','document_versions','index_outbox'))

    def test_extract_html_boilerplate_title_and_canonical(self):
        html = b'''<html lang="zh-CN"><head><title>  OpenSearch \xe4\xb8\xad\xe6\x96\x87 </title>
            <meta name="description" content="Example"><link rel="canonical" href="/guide"></head>
            <body><nav>menu spam</nav><header>header spam</header>
            <h1>\xe6\xa3\x80\xe7\xb4\xa2 \xe5\xbc\x95\xe6\x93\x8e</h1><p>Python 3.12</p>
            <footer>footer spam</footer><script>secret code</script></body></html>'''
        doc = extract(html,'text/html','https://example.org/article')
        self.assertEqual(doc.title,'OpenSearch 中文')
        self.assertNotIn('spam',doc.body)
        self.assertNotIn('secret',doc.body)
        self.assertIn('检索 引擎',doc.body)
        self.assertEqual(doc.canonical_hint,'https://example.org/guide')
        self.assertEqual(doc.language,'zh')
        self.assertTrue(doc.indexable)

    def test_extract_gbk_and_strict_noindex(self):
        html = '<meta charset="gbk"><meta name="robots" content="nofollow, noindex"><p>机器学习</p>'
        parsed = extract(html.encode('gbk'),'text/html','https://example.org/gbk')
        self.assertIn('机器学习',parsed.body)
        self.assertFalse(parsed.indexable)
        self.assertTrue(extract(b'allowed to index','text/plain','https://example.org/ok').indexable)
        self.assertFalse(extract(b'allowed to index','text/plain','https://example.org/ok',x_robots_tag='noindex, nofollow').indexable)
        self.assertTrue(extract(b'allowed to index','text/plain','https://example.org/ok',x_robots_tag='noindexing-is-not-noindex').indexable)

    def test_new_version_and_atomic_outbox(self):
        url = 'https://example.org/post'
        self.add_fetch(url,'<title>One</title><p>First meaningful body</p>')
        self.assertEqual(self.processor.run_once(now_ms=101),'created')
        self.assertEqual(self.row_counts(),(1,1,1))
        with self.db.connect() as con:
            event = con.execute('SELECT op_type,state,payload_ref,revision FROM index_outbox').fetchone()
            self.assertEqual((event['op_type'],event['state'],event['revision']),('upsert','pending',1))
            self.assertTrue(self.store.get(event['payload_ref']))
        self.add_fetch(url,'<title>One</title><p>First meaningful body</p>',now=200)
        self.assertEqual(self.processor.run_once(now_ms=201),'unchanged')
        self.assertEqual(self.row_counts(),(1,1,1))
        self.add_fetch(url,'<title>Two</title><p>Changed meaningful body</p>',now=300)
        self.assertEqual(self.processor.run_once(now_ms=301),'updated')
        self.assertEqual(self.row_counts(),(1,2,2))
        with self.db.connect() as con:
            self.assertEqual([r[0] for r in con.execute('SELECT revision FROM document_versions ORDER BY revision')],[1,2])

    def test_exact_dedup_preserves_both_documents(self):
        content = 'Same exact original article body with meaningful words.'
        self.add_fetch('https://example.org/a','<title>A</title><p>'+content+'</p>',now=100)
        self.assertEqual(self.processor.run_once(now_ms=101),'created')
        self.add_fetch('https://example.org/b','<title>B</title><p>'+content+'</p>',now=200)
        self.assertEqual(self.processor.run_once(now_ms=201),'created')
        self.assertEqual(self.row_counts(),(2,2,2))
        with self.db.connect() as con:
            rel = con.execute('SELECT method,score,decision FROM duplicate_relations').fetchone()
            self.assertEqual((rel['method'],rel['score'],rel['decision']),('exact',1.0,'duplicate'))

    def test_near_dedup_and_non_transitive_pairwise(self):
        content = ' '.join(f'unique{n}' for n in range(150))
        edited = content.replace('unique90','otherword90')
        self.assertIsNotNone(near_match(content,edited,fingerprint(shingles(content)),fingerprint(shingles(edited))))
        self.add_fetch('https://example.org/a',content,kind='text/plain',now=100)
        self.processor.run_once(now_ms=101)
        self.add_fetch('https://example.org/b',edited,kind='text/plain',now=200)
        self.processor.run_once(now_ms=201)
        with self.db.connect() as con:
            rel=con.execute("SELECT method,score FROM duplicate_relations WHERE method='near'").fetchone()
            self.assertIsNotNone(rel)
            self.assertGreaterEqual(rel['score'],.85)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM document_signatures').fetchone()[0],2)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM signature_bands').fetchone()[0],16)

    def test_noindex_delete_and_recovery(self):
        url='https://example.org/post'
        self.add_fetch(url,'<title>One</title><p>Visible body</p>',now=100)
        self.processor.run_once(now_ms=101)
        self.add_fetch(url,'<meta name="robots" content="noindex"><p>Visible body</p>',now=200)
        self.assertEqual(self.processor.run_once(now_ms=201),'noindex')
        self.add_fetch(url,'<p>Visible body again</p>',now=300)
        self.assertEqual(self.processor.run_once(now_ms=301),'updated')
        self.add_fetch(url,status=410,now=400)
        self.assertEqual(self.processor.run_once(now_ms=401),'deleted')
        self.add_fetch(url,'<p>Back online</p>',now=500)
        self.assertEqual(self.processor.run_once(now_ms=501),'updated')
        with self.db.connect() as con:
            events = [row[0] for row in con.execute('SELECT op_type FROM index_outbox ORDER BY revision')]
            self.assertEqual(events,['upsert','delete','upsert','delete','upsert'])
            self.assertEqual(tuple(con.execute('SELECT is_deleted,latest_revision FROM documents').fetchone()),(0,5))

    def test_old_fetch_cannot_overwrite_new_revision(self):
        url='https://example.org/one'
        self.add_fetch(url,'Old content',kind='text/plain',now=100)
        self.add_fetch(url,'New content',kind='text/plain',now=200)
        self.assertEqual(self.processor.run_once(now_ms=201),'created')  # oldest queued first
        self.assertEqual(self.processor.run_once(now_ms=202),'updated')
        # Reprocess older fetch in a separate queued task after the newer revision.
        with self.db.connect() as con:
            fetch_id=con.execute('SELECT fetch_id FROM fetches ORDER BY rowid LIMIT 1').fetchone()[0]
        self.processor.queue.enqueue('EXTRACT_CONTENT',fetch_id,'replay-stale',run_id=self.run,now_ms=202)
        self.assertEqual(self.processor.run_once(now_ms=203),'stale')
        self.assertEqual(self.row_counts(),(1,2,2))

    def test_outbox_failure_rolls_back_version_and_job_ack(self):
        self.add_fetch('https://example.org/fault','content',kind='text/plain')
        with self.db.transaction() as con:
            con.execute("CREATE TRIGGER stop_outbox BEFORE INSERT ON index_outbox BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.processor.run_once(now_ms=101)
        self.assertEqual(self.row_counts(),(0,0,0))
        with self.db.transaction() as con:
            con.execute('DROP TRIGGER stop_outbox')
        self.assertEqual(self.processor.run_once(now_ms=60102),'created')
        self.assertEqual(self.row_counts(),(1,1,1))

    def test_corrupted_raw_content_is_dead_lettered(self):
        fetch_id=self.add_fetch('https://example.org/err','raw-body',kind='text/plain')
        with self.db.transaction() as con:
            con.execute("UPDATE fetches SET raw_sha256='bad' WHERE fetch_id=?",(fetch_id,))
        self.assertEqual(self.processor.run_once(now_ms=101),'dead')
        self.assertEqual(self.row_counts(),(0,0,0))

    def test_cancelled_run_blocks_versions(self):
        self.add_fetch('https://example.org/cancel','raw-body',kind='text/plain')
        Queue(self.db).cancel_run(self.run,now_ms=101)
        self.assertEqual(self.processor.run_once(now_ms=102),'idle')
        self.assertEqual(self.row_counts(),(0,0,0))

    def test_duplicate_relation_invalidated_on_document_change(self):
        body='The same same document content with useful detail'
        self.add_fetch('https://example.org/a',body,kind='text/plain',now=100)
        self.processor.run_once(now_ms=101)
        self.add_fetch('https://example.org/b',body,kind='text/plain',now=200)
        self.processor.run_once(now_ms=201)
        self.add_fetch('https://example.org/b','Completely different article',kind='text/plain',now=300)
        self.assertEqual(self.processor.run_once(now_ms=301),'updated')
        with self.db.connect() as con:
            self.assertEqual(con.execute("SELECT decision FROM duplicate_relations WHERE method='exact'").fetchone()[0],'distinct')

    def test_noindex_header_produces_delete_event(self):
        self.add_fetch('https://example.org/header',b'<p>Secret document body</p>',
                       kind='text/html',x_robots_tag='googlebot: noindex')
        self.assertEqual(self.processor.run_once(now_ms=101),'noindex')
        with self.db.connect() as con:
            self.assertEqual(con.execute('SELECT op_type FROM index_outbox').fetchone()[0],'delete')
            self.assertEqual(con.execute('SELECT is_indexable FROM document_versions').fetchone()[0],0)

    def test_html_void_inside_boilerplate_and_cross_site_canonical(self):
        html=b'<nav><img src="x"><div>spam</div></nav><p>Keep this</p><link rel="canonical" href="https://another.example.org/">'
        parsed=extract(html,'text/html','https://example.org/original')
        self.assertEqual(parsed.body,'Keep this')
        self.assertIsNone(parsed.canonical_hint)

    def test_unsupported_media_does_not_make_version(self):
        self.add_fetch('https://example.org/xml',b'<xml>not supported</xml>',kind='text/xml')
        self.assertEqual(self.processor.run_once(now_ms=101),'dead')
        self.assertEqual(self.row_counts(),(0,0,0))

    def test_simultaneous_initializer_migration_is_serialized(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'concurrent.sqlite'
            with closing(sqlite3.connect(path)) as con:
                con.executescript(PHASE1_SCHEMA.read_text())
            def boot(_):
                local=Database(path,SCHEMA)
                local.initialize()
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(boot,range(6)))
            with closing(sqlite3.connect(path)) as con:
                self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0],5)
                self.assertEqual(con.execute('SELECT COUNT(*) FROM sqlite_master WHERE name=\'document_signatures\'').fetchone()[0],1)

    def test_v2_to_v3_migration_preserves_data(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'existing.sqlite'
            with closing(sqlite3.connect(path)) as con:
                con.executescript(PHASE1_SCHEMA.read_text())
                con.execute('''INSERT INTO sources
                  (source_id,name,root_url,allowed_hosts_json,policy_json,created_at_ms,updated_at_ms)
                  VALUES ('old','Old','https://example.org','["example.org"]','{}',0,0)''')
                con.commit()
            old=Database(path,SCHEMA)
            old.initialize()
            old.initialize()
            with old.connect() as con:
                self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0],5)
                self.assertEqual(con.execute('SELECT COUNT(*) FROM sources').fetchone()[0],1)
                self.assertIn('x_robots_tag',[r[1] for r in con.execute('PRAGMA table_info(fetches)')])

if __name__ == '__main__':
    unittest.main()
