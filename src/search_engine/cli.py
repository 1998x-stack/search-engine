"""Operator CLI for Phase 1 (source/run/seed/worker/status)."""
from __future__ import annotations
import argparse
import json
import os
import time
from .crawler import Crawler
from .db import Database
from .fetch import SafeFetcher
from .frontier import Frontier
from .store import BlobStore
from .queue import Queue

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Search Engine Phase 1 crawl foundation')
    parser.add_argument('--database',default=os.getenv('SEARCH_DATABASE_PATH','./data/search.db'))
    parser.add_argument('--schema',default=os.getenv('SEARCH_SCHEMA_PATH','./schema/sqlite.sql'))
    parser.add_argument('--raw-dir',default=os.getenv('SEARCH_RAW_DIR','./data/raw'))
    subs = parser.add_subparsers(dest='command',required=True)
    subs.add_parser('init')
    source = subs.add_parser('source')
    source.add_argument('source_id')
    source.add_argument('root_url')
    source.add_argument('--host',action='append',default=[])
    run = subs.add_parser('run')
    run.add_argument('source_id')
    run.add_argument('seed_url')
    worker = subs.add_parser('worker')
    worker.add_argument('--once',action='store_true')
    worker.add_argument('--max-steps',type=int,default=100)
    worker.add_argument('--sleep-ms',type=int,default=1000)
    status = subs.add_parser('status')
    status.add_argument('--run-id')
    return parser

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = Database(args.database,args.schema)
    db.initialize()
    frontier = Frontier(db)
    if args.command == 'init':
        print('SQLite initialized:',args.database)
    elif args.command == 'source':
        frontier.create_source(args.source_id,args.root_url,
                               allowed_hosts=args.host or None)
        print('Source registered:',args.source_id)
    elif args.command == 'run':
        run_id = frontier.create_run(args.source_id)
        url_id = frontier.add_url(args.source_id,args.seed_url,run_id=run_id)
        print(json.dumps({'run_id':run_id,'seed_url_id':url_id}))
    elif args.command == 'worker':
        ua = os.getenv('CRAWLER_USER_AGENT','')
        if not ua or '@' not in ua and 'http' not in ua:
            raise SystemExit('Set CRAWLER_USER_AGENT to an identifiable crawler with contact')
        fetcher = SafeFetcher(user_agent=ua)
        crawler = Crawler(db,frontier,fetcher,BlobStore(args.raw_dir))
        for _ in range(1 if args.once else args.max_steps):
            result = crawler.run_once()
            Queue(db).finalize_runs()
            print(result,flush=True)
            if result in ('idle','deferred') and not args.once:
                time.sleep(max(0,args.sleep_ms)/1000)
    elif args.command == 'status':
        with db.connect() as con:
            if args.run_id:
                rows = con.execute('SELECT state,COUNT(*) AS cnt FROM jobs WHERE run_id=? GROUP BY state',
                                   (args.run_id,)).fetchall()
            else:
                rows = con.execute('SELECT state,COUNT(*) AS cnt FROM jobs GROUP BY state').fetchall()
            print(json.dumps({row['state']:row['cnt'] for row in rows}))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
