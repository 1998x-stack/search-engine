"""Operator CLI for Phase 1 (source/run/seed/worker/status)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

from .crawler import Crawler
from .db import Database
from .fetch import SafeFetcher
from .frontier import Frontier
from .store import BlobStore
from .queue import Queue
from .processing import DocumentProcessor
from .indexing import OutboxIndexer
from .opensearch import OpenSearchClient, DEFAULT_INDEX
from .rebuild import IndexManager
from .search import SearchService
from .hybrid import HybridSearchService
from .semantic import (VectorIndexManager,VectorProjector,SentenceTransformerEmbedder,
                       CrossEncoderReranker,DEFAULT_MODEL_ID,VECTOR_INDEX)
from .query import plan_query


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Search Engine crawl, process and lexical indexing')
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
    processor = subs.add_parser('process')
    processor.add_argument('--once',action='store_true')
    processor.add_argument('--max-steps',type=int,default=100)
    processor.add_argument('--sleep-ms',type=int,default=1000)
    worker = subs.add_parser('worker')
    worker.add_argument('--once',action='store_true')
    worker.add_argument('--max-steps',type=int,default=100)
    worker.add_argument('--sleep-ms',type=int,default=1000)
    indexer = subs.add_parser('index')
    indexer.add_argument('--once',action='store_true')
    indexer.add_argument('--max-steps',type=int,default=100)
    indexer.add_argument('--sleep-ms',type=int,default=1000)
    indexer.add_argument('--batch-size',type=int,default=32)
    init_index = subs.add_parser('index-init')
    init_index.add_argument('--index',default=DEFAULT_INDEX)
    audit = subs.add_parser('index-audit')
    retry_dead = subs.add_parser('index-retry-dead')
    retry_dead.add_argument('--document-id')
    rebuild = subs.add_parser('index-rebuild')
    rebuild.add_argument('--new-index',required=True)
    rebuild.add_argument('--offline-confirm',action='store_true')
    query = subs.add_parser('search')
    query.add_argument('query')
    query.add_argument('--language')
    query.add_argument('--source-id')
    query.add_argument('--rewrite-mode',choices=['off','suggest','expand'],default='suggest')
    query.add_argument('--mode',choices=['lexical','hybrid'],default='lexical')
    query.add_argument('--rerank',action='store_true')
    vector_init = subs.add_parser('vector-init')
    vector_init.add_argument('--index',default=VECTOR_INDEX)
    vector_reset = subs.add_parser('vector-reset-receipts')
    vector_reset.add_argument('--confirm',action='store_true')
    vector_sync = subs.add_parser('vector-sync')
    vector_sync.add_argument('--batch-size',type=int,default=8)
    vector_sync.add_argument('--max-steps',type=int,default=100)
    vector_sync.add_argument('--once',action='store_true')

    analyze = subs.add_parser('analyze')
    analyze.add_argument('query')
    analyze.add_argument('--language')
    analyze.add_argument('--source-id')
    analyze.add_argument('--rewrite-mode',choices=['off','suggest','expand'],default='suggest')
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
        # Must be set to a named, truthful user agent with a contact method.
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
    elif args.command == 'process':
        processor = DocumentProcessor(db,BlobStore(args.raw_dir))
        for _ in range(1 if args.once else args.max_steps):
            result = processor.run_once()
            Queue(db).finalize_runs()
            print(result,flush=True)
            if result == 'idle' and not args.once:
                time.sleep(max(0,args.sleep_ms)/1000)
    elif args.command == 'analyze':
        print(json.dumps(plan_query(args.query,language=args.language,
                                    source_id=args.source_id,
                                    rewrite_mode=args.rewrite_mode).public_summary(),ensure_ascii=False))
    elif args.command in ('index','index-init','index-audit','index-retry-dead','index-rebuild','search','vector-init','vector-sync','vector-reset-receipts'):
        client = OpenSearchClient(os.getenv('OPENSEARCH_URL','http://127.0.0.1:9200'),
                                  username=os.getenv('OPENSEARCH_USERNAME'),
                                  password=os.getenv('OPENSEARCH_PASSWORD'))
        store = BlobStore(args.raw_dir)
        indexer = OutboxIndexer(db,store,client)
        if args.command == 'vector-init':
            VectorIndexManager(client,Path(__file__).resolve().parents[2] /
                              'infra/opensearch/vectors-v1.json').initialize(args.index)
            print('Vector index and aliases initialized:',args.index)
        elif args.command == 'vector-reset-receipts':
            emb = SentenceTransformerEmbedder(
                os.getenv('SEARCH_EMBEDDING_MODEL_ID',DEFAULT_MODEL_ID),
                os.getenv('SEARCH_EMBEDDING_MODEL_REVISION','main'))
            projector = VectorProjector(db,store,client,emb)
            deleted = projector.reset_receipts(confirmed_quiescent=args.confirm)
            print(json.dumps({'removed_receipts':deleted,'model_key':emb.model_key}))
        elif args.command == 'vector-sync':
            emb = SentenceTransformerEmbedder(
                os.getenv('SEARCH_EMBEDDING_MODEL_ID',DEFAULT_MODEL_ID),
                os.getenv('SEARCH_EMBEDDING_MODEL_REVISION','main'))
            projector = VectorProjector(db,store,client,emb)
            for _ in range(1 if args.once else args.max_steps):
                outcome = projector.sync_once(count=args.batch_size)
                print(json.dumps(outcome),flush=True)
                if not outcome['selected'] or outcome['failed'] or outcome['conflicts']:
                    break
        elif args.command == 'index-init':
            IndexManager(db,store,client,Path(__file__).resolve().parents[2] / 'infra/opensearch/documents-v1.json').initialize(args.index)
            print('Index and aliases initialized:',args.index)
        elif args.command == 'index':
            for _ in range(1 if args.once else args.max_steps):
                outcome=indexer.run_once(count=args.batch_size)
                print(json.dumps(outcome),flush=True)
                if not outcome['claimed'] and not args.once:
                    time.sleep(max(0,args.sleep_ms)/1000)
        elif args.command == 'index-audit':
            print(json.dumps(indexer.audit_local()))
        elif args.command == 'index-retry-dead':
            print(json.dumps({'retried':indexer.retry_dead(document_id=args.document_id)}))
        elif args.command == 'index-rebuild':
            manager = IndexManager(db,store,client,Path(__file__).resolve().parents[2] / 'infra/opensearch/documents-v1.json')
            print(json.dumps(manager.rebuild_offline(args.new_index,confirmed_quiescent=args.offline_confirm)))
        else:
            if args.mode == 'lexical' and not args.rerank:
                result = SearchService(client).search(
                    args.query,language=args.language,
                    source_id=args.source_id,rewrite_mode=args.rewrite_mode)
            else:
                emb = SentenceTransformerEmbedder(
                    os.getenv('SEARCH_EMBEDDING_MODEL_ID',DEFAULT_MODEL_ID),
                    os.getenv('SEARCH_EMBEDDING_MODEL_REVISION','main')
                ) if os.getenv('SEARCH_SEMANTIC_ENABLED')=='1' else None
                reranker = CrossEncoderReranker(
                    os.getenv('SEARCH_RERANKER_MODEL_ID','cross-encoder/ms-marco-MiniLM-L6-v2'),
                    os.getenv('SEARCH_RERANKER_MODEL_REVISION','main')
                ) if args.rerank and os.getenv('SEARCH_RERANK_ENABLED')=='1' else None
                result = HybridSearchService(client,db,emb,reranker).search(
                    args.query,language=args.language,source_id=args.source_id,
                    rewrite_mode=args.rewrite_mode,mode=args.mode,rerank=args.rerank)
            print(json.dumps(result,ensure_ascii=False))
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
