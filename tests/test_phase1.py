"""Offline integration suite against an approved loopback HTTP fixture."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import gzip
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

from search_engine.db import Database
from search_engine.queue import Queue
from search_engine.urls import normalize_url, URLRejected, resolve_public_ips
from search_engine.robots import RobotsPolicy
from search_engine.fetch import SafeFetcher, FetchError
from search_engine.frontier import Frontier
from search_engine.crawler import Crawler
from search_engine.store import BlobStore

SCHEMA = Path(__file__).resolve().parents[1] / 'schema/sqlite.sql'

class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    hits: dict[str, int] = {}
    robots_body = b'User-agent: TestCrawler\nDisallow: /private\nAllow: /private/ok\n'
    robots_content_type = 'text/plain'
    robots_status = 200

    def do_GET(self):
        path = urlsplit(self.path).path
        self.hits[path] = self.hits.get(path, 0) + 1
        if path == '/robots.txt':
            self.send_bytes(self.robots_status,self.robots_body,self.robots_content_type)
        elif path == '/':
            if self.headers.get('If-None-Match') == '"seed"':
                self.send_bytes(304,b'','text/html',{'ETag':'"seed"'})
            else:
                self.send_bytes(200,b'<html><a href="/next">Next</a><a href="/private">private</a><a href="http://169.254.169.254/latest">forbidden</a></html>',
                                'text/html',{'ETag':'"seed"'})
        elif path == '/next':
            self.send_bytes(200,b'Next page','text/plain')
        elif path == '/private':
            self.send_bytes(200,b'NEVER FETCH','text/plain')
        elif path == '/retry':
            self.send_bytes(429,b'no','text/plain',{'Retry-After':'2'})
        elif path == '/redirect':
            self.send_bytes(302,b'','text/plain',{'Location':'/next'})
        elif path == '/redirect-out':
            self.send_bytes(302,b'','text/plain',{'Location':'http://127.0.0.2/escape'})
        elif path == '/gzip':
            self.send_bytes(200,gzip.compress(b'hello world'),'text/plain',{'Content-Encoding':'gzip'})
        elif path == '/gzip-huge':
            self.send_bytes(200,gzip.compress(b'x'*20000),'text/plain',{'Content-Encoding':'gzip'})
        elif path == '/huge':
            self.send_bytes(200,b'x'*20000,'text/plain')
        elif path == '/gone':
            self.send_bytes(410,b'', 'text/plain')
        else:
            self.send_bytes(404,b'', 'text/plain')

    def send_bytes(self,status: int,body: bytes,kind: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(body)))
        for key,value in (extra or {}).items():
            self.send_header(key,value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass

class FixtureServer:
    def __enter__(self):
        self.server = ThreadingHTTPServer(('127.0.0.1',0),FixtureHandler)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        FixtureHandler.hits = {}
        FixtureHandler.robots_body = b'User-agent: TestCrawler\nDisallow: /private\nAllow: /private/ok\n'
        FixtureHandler.robots_content_type = 'text/plain'
        FixtureHandler.robots_status = 200
        self.root = f'http://127.0.0.1:{self.server.server_port}'
        return self

    def __exit__(self,*_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.db = Database(root/'state.db', SCHEMA)
        self.db.initialize()
        self.store = BlobStore(root/'raw')
        self.queue = Queue(self.db)

    def tearDown(self):
        self.dir.cleanup()

    def make_crawler(self, fixture: FixtureServer, *, min_interval_ms: int = 0):
        frontier = Frontier(self.db,allowed_ports={fixture.server.server_port},
                            min_interval_ms=min_interval_ms)
        fetcher = SafeFetcher(user_agent='TestCrawler/1 (+mailto:team@example.org)',
                              allowed_ports={fixture.server.server_port},allow_private_ips=True)
        crawler = Crawler(self.db,frontier,fetcher,self.store,max_depth=2)
        frontier.create_source('s',fixture.root,now_ms=0)
        run = frontier.create_run('s',now_ms=0)
        frontier.add_url('s',fixture.root,run_id=run,now_ms=0)
        return crawler,frontier,run

    def test_sqlite_queue_leases_and_fencing(self):
        self.queue.enqueue('FETCH_URL','url','fetch:url',now_ms=0)
        a = self.queue.claim('A',now_ms=0,lease_ms=10)
        self.assertIsNotNone(a)
        self.assertIsNone(self.queue.claim('B',now_ms=5))
        b = self.queue.claim('B',now_ms=11)
        self.assertEqual(b.job_id,a.job_id)
        self.assertGreater(b.epoch,a.epoch)
        with self.assertRaises(RuntimeError):
            self.queue.complete(a)
        self.queue.complete(b)
        with self.db.connect() as con:
            self.assertEqual(con.execute('SELECT state FROM jobs').fetchone()[0], 'succeeded')

    def test_retry_budget_and_idempotent_enqueue(self):
        a = self.queue.enqueue('FETCH_URL','url','same',now_ms=0)
        self.assertEqual(a,self.queue.enqueue('FETCH_URL','url','same',now_ms=0))
        with self.assertRaises(ValueError):
            self.queue.enqueue('FETCH_URL','different','same',now_ms=0)
        with self.db.transaction() as con:
            con.execute('UPDATE jobs SET max_attempts=2')
        lease = self.queue.claim('w',now_ms=0)
        self.assertEqual(self.queue.fail(lease,'transient',retryable=True,now_ms=0,retry_after_ms=10),'retry')
        lease = self.queue.claim('w',now_ms=10)
        self.assertEqual(self.queue.fail(lease,'transient',retryable=True,now_ms=10),'dead')

    def test_host_permits_are_serialized_and_expire(self):
        with FixtureServer() as fixture:
            frontier = Frontier(self.db,allowed_ports={fixture.server.server_port},min_interval_ms=0)
            frontier.create_source('s',fixture.root,now_ms=0)
            run = frontier.create_run('s',now_ms=0)
            url_id = frontier.add_url('s',fixture.root,run_id=run,now_ms=0)
            with self.db.connect() as con:
                host_id = con.execute('SELECT host_id FROM urls WHERE url_id=?',(url_id,)).fetchone()[0]
                job_id = con.execute('SELECT job_id FROM jobs').fetchone()[0]
            p,due = frontier.reserve_host(host_id,job_id,now_ms=1,lease_ms=10)
            self.assertIsNotNone(p)
            other,due = frontier.reserve_host(host_id,job_id,now_ms=2,lease_ms=10)
            self.assertIsNone(other)
            new,due = frontier.reserve_host(host_id,job_id,now_ms=11,lease_ms=10)
            self.assertIsNotNone(new)
            frontier.release_host(new)

    def test_robot_longest_rule_wins(self):
        policy = RobotsPolicy.parse('''User-agent: *
Disallow: /secret
User-agent: TestCrawler
Disallow: /private
Allow: /private/ok
Disallow: /tmp/*
Allow: /tmp/public$''')
        self.assertFalse(policy.allowed('TestCrawler/1','https://example.org/private/a'))
        self.assertTrue(policy.allowed('TestCrawler/1','https://example.org/private/ok'))
        self.assertTrue(policy.allowed('TestCrawler/1','https://example.org/secret'))
        self.assertFalse(policy.allowed('TestCrawler/1','https://example.org/tmp/else'))
        self.assertTrue(policy.allowed('TestCrawler/1','https://example.org/tmp/public'))

    def test_url_scoping_and_ssrf(self):
        self.assertEqual(normalize_url('HTTPS://EXAMPLE.ORG:443/hello#fragment'),
                         'https://example.org/hello')
        self.assertEqual(normalize_url('https://example.org/?a=2&a=1'),
                         'https://example.org/?a=2&a=1')
        for value in ('file:///etc/passwd','http://user:password@example.org/',
                      'http://example.org:22/', 'http://2130706433/'):
            with self.assertRaises(URLRejected, msg=value):
                normalize_url(value)
        with self.assertRaises(URLRejected):
            resolve_public_ips('127.0.0.1',80)

    def test_fetcher_gzip_and_byte_limit(self):
        with FixtureServer() as fixture:
            fetcher = SafeFetcher(user_agent='TestCrawler/1 (+mailto:team@example.org)',
                                  allowed_ports={fixture.server.server_port},allow_private_ips=True,
                                  max_body_bytes=100)
            self.assertEqual(fetcher.fetch(fixture.root+'/gzip').body,b'hello world')
            with self.assertRaises(FetchError):
                fetcher.fetch(fixture.root+'/huge')
            with self.assertRaises(FetchError):
                fetcher.fetch(fixture.root+'/gzip-huge')

    def test_end_to_end_discovery_robots_and_raw_store(self):
        with FixtureServer() as fixture:
            crawler,frontier,run = self.make_crawler(fixture)
            results = [crawler.run_once(now_ms=n) for n in range(15)]
            self.assertIn('robots',results)
            self.assertIn('fetched',results)
            self.assertIn('blocked',results)
            self.assertEqual(FixtureHandler.hits.get('/private',0),0)
            self.assertEqual(FixtureHandler.hits['/robots.txt'],1)
            with self.db.connect() as con:
                rows = con.execute('SELECT status,normalized_url FROM urls').fetchall()
                self.assertEqual(len(rows),3)
                self.assertEqual(con.execute('SELECT COUNT(*) FROM fetches WHERE http_status=200').fetchone()[0],2)
                ref = con.execute('SELECT raw_blob_ref FROM fetches WHERE http_status=200 LIMIT 1').fetchone()[0]
                self.assertTrue(self.store.get(ref))

    def test_conditional_304_across_runs(self):
        with FixtureServer() as fixture:
            crawler,frontier,run = self.make_crawler(fixture)
            self.assertEqual(crawler.run_once(now_ms=1),'robots')
            self.assertEqual(crawler.run_once(now_ms=2),'fetched')
            second = frontier.create_run('s',now_ms=3)
            frontier.add_url('s',fixture.root,run_id=second,now_ms=3)
            results = [crawler.run_once(now_ms=n) for n in range(4,10)]
            self.assertIn('not_modified',results)
            with self.db.connect() as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM fetches WHERE http_status=304').fetchone()[0],1)

    def test_429_backoff_and_no_challenge_evasion(self):
        with FixtureServer() as fixture:
            crawler,frontier,run = self.make_crawler(fixture)
            frontier.add_url('s',fixture.root+'/retry',run_id=run,now_ms=1)
            for i in range(10):
                crawler.run_once(now_ms=i+1)
            with self.db.connect() as con:
                rows = con.execute("SELECT state,scheduled_at_ms FROM jobs WHERE entity_id=(SELECT url_id FROM urls WHERE normalized_url=?)",
                                   (fixture.root+'/retry',)).fetchall()
                self.assertEqual(len(rows),1)
                self.assertEqual(rows[0]['state'],'retry')
                self.assertGreater(rows[0]['scheduled_at_ms'],1000)

    def test_redirect_does_not_auto_fetch_and_respects_scope(self):
        with FixtureServer() as fixture:
            crawler,frontier,run = self.make_crawler(fixture)
            frontier.add_url('s',fixture.root+'/redirect-out',run_id=run,now_ms=0)
            for i in range(8):
                crawler.run_once(now_ms=i+1)
            with self.db.connect() as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM urls WHERE normalized_url LIKE ?',('%127.0.0.2%',)).fetchone()[0],0)

    def test_robots_200_challenge_page_does_not_grant_access(self):
        with FixtureServer() as fixture:
            FixtureHandler.robots_body = b'<html><title>Human verification</title></html>'
            FixtureHandler.robots_content_type = 'text/html'
            crawler,frontier,run = self.make_crawler(fixture)
            frontier.add_url('s',fixture.root+'/next',run_id=run,now_ms=0)
            self.assertEqual(crawler.run_once(now_ms=1),'blocked')
            self.assertEqual(crawler.run_once(now_ms=2),'blocked')
            self.assertEqual(FixtureHandler.hits.get('/',0),0)
            self.assertEqual(FixtureHandler.hits.get('/next',0),0)
            with self.db.connect() as con:
                self.assertEqual(con.execute('SELECT robots_status FROM hosts').fetchone()[0], 'disallowed')

    def test_robots_503_fails_closed_without_page_fetch(self):
        with FixtureServer() as fixture:
            FixtureHandler.robots_status = 503
            crawler,frontier,run = self.make_crawler(fixture)
            self.assertEqual(crawler.run_once(now_ms=1),'retry')
            self.assertEqual(FixtureHandler.hits.get('/',0),0)
            with self.db.connect() as con:
                self.assertEqual(con.execute('SELECT state FROM jobs').fetchone()[0], 'retry')

    def test_run_max_urls_budget(self):
        with FixtureServer() as fixture:
            frontier = Frontier(self.db,allowed_ports={fixture.server.server_port})
            frontier.create_source('s',fixture.root,now_ms=0)
            run = frontier.create_run('s',config={'max_urls':2},now_ms=0)
            frontier.add_url('s',fixture.root,run_id=run,now_ms=0)
            frontier.add_url('s',fixture.root+'/a',run_id=run,now_ms=0)
            with self.assertRaises(URLRejected):
                frontier.add_url('s',fixture.root+'/b',run_id=run,now_ms=0)

    def test_max_urls_budget_across_existing_urls_from_prior_runs(self):
        with FixtureServer() as fixture:
            frontier = Frontier(self.db,allowed_ports={fixture.server.server_port})
            frontier.create_source('s',fixture.root,now_ms=0)
            first = frontier.create_run('s',now_ms=0)
            frontier.add_url('s',fixture.root,run_id=first,now_ms=0)
            frontier.add_url('s',fixture.root+'/next',run_id=first,now_ms=0)
            second = frontier.create_run('s',config={'max_urls':1},now_ms=1)
            frontier.add_url('s',fixture.root,run_id=second,now_ms=1)
            with self.assertRaises(URLRejected):
                frontier.add_url('s',fixture.root+'/next',run_id=second,now_ms=1)

    def test_finalize_and_cancel_run(self):
        with FixtureServer() as fixture:
            crawler,frontier,run = self.make_crawler(fixture)
            self.queue.cancel_run(run,now_ms=1)
            self.assertIsNone(self.queue.claim('other',now_ms=2))
            with self.db.connect() as con:
                self.assertEqual(con.execute('SELECT state FROM crawl_runs').fetchone()[0],'cancelled')
            other = frontier.create_run('s',now_ms=3)
            frontier.add_url('s',fixture.root+'/gone',run_id=other,now_ms=3)
            for n in range(4,9):
                crawler.run_once(now_ms=n)
            self.assertEqual(self.queue.finalize_runs(now_ms=10),1)
            with self.db.connect() as con:
                self.assertEqual(con.execute('SELECT state FROM crawl_runs WHERE run_id=?',(other,)).fetchone()[0],'completed')

    def test_concurrent_claims_are_exclusive(self):
        self.queue.enqueue('FETCH_URL','url','test:race',now_ms=0)
        def claim(worker):
            return self.queue.claim(worker,now_ms=1,lease_ms=5000)
        with ThreadPoolExecutor(max_workers=8) as pool:
            leases = list(pool.map(claim,[str(n) for n in range(8)]))
        self.assertEqual(sum(x is not None for x in leases),1)
        self.queue.complete(next(x for x in leases if x),now_ms=2)

if __name__ == '__main__':
    unittest.main()
