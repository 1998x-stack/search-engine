"""Whole Phase1+Phase2 pipeline using loopback fixture, no external network."""
from __future__ import annotations
from pathlib import Path
import tempfile
import unittest

from search_engine.crawler import Crawler
from search_engine.db import Database
from search_engine.fetch import SafeFetcher
from search_engine.frontier import Frontier
from search_engine.processing import DocumentProcessor
from search_engine.queue import Queue
from search_engine.store import BlobStore
from test_phase1 import FixtureServer

SCHEMA = Path(__file__).resolve().parents[1]/'schema/sqlite.sql'

class CrawlThroughOutboxTests(unittest.TestCase):
    def test_http_fetch_to_version_to_outbox_and_304(self):
        with FixtureServer() as fixture, tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp)/'s.db',SCHEMA)
            db.initialize()
            store = BlobStore(Path(tmp)/'raw')
            frontier = Frontier(db,allowed_ports={fixture.server.server_port},min_interval_ms=0)
            frontier.create_source('source',fixture.root,now_ms=0)
            run = frontier.create_run('source',now_ms=0)
            frontier.add_url('source',fixture.root,run_id=run,now_ms=0)
            fetcher = SafeFetcher(user_agent='TestCrawler/1 (+mailto:team@example.org)',
                                  allowed_ports={fixture.server.server_port},allow_private_ips=True)
            crawler = Crawler(db,frontier,fetcher,store,max_depth=0)
            processor = DocumentProcessor(db,store)
            self.assertEqual(crawler.run_once(now_ms=1),'robots')
            self.assertEqual(crawler.run_once(now_ms=2),'fetched')
            with db.connect() as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs WHERE kind='EXTRACT_CONTENT'").fetchone()[0],1)
            self.assertEqual(processor.run_once(now_ms=3),'created')
            self.assertEqual(Queue(db).finalize_runs(now_ms=4),1)
            with db.connect() as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM index_outbox').fetchone()[0],1)
                self.assertEqual(con.execute('SELECT normalized_sha256 FROM document_versions').fetchone()[0].__len__(),64)
            next_run = frontier.create_run('source',now_ms=5)
            frontier.add_url('source',fixture.root,run_id=next_run,now_ms=5)
            self.assertEqual(crawler.run_once(now_ms=6),'not_modified')
            self.assertEqual(processor.run_once(now_ms=7),'idle')
            with db.connect() as con:
                self.assertEqual(con.execute('SELECT COUNT(*) FROM document_versions').fetchone()[0],1)
                self.assertEqual(con.execute('SELECT COUNT(*) FROM index_outbox').fetchone()[0],1)

if __name__ == '__main__':
    unittest.main()
