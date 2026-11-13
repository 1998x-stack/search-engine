# Search Engine

**Status: Phase 3 — SQLite-backed crawling, document processing and OpenSearch BM25 lexical search baseline.** This is a single-host development prototype, **not** a production-ready Web-scale search engine.

## Architecture

`Seed -> Robots/Host Frontier -> HTTP Fetch -> Immutable Raw Blob -> Extract/Normalize/Dedup -> Document Version + Index Outbox -> OpenSearch Bulk -> BM25 /v1/search`

- SQLite WAL v4: authoritative crawl jobs, source/URL metadata, document revisions, durable outbox, projection receipts; cannot be shared between hosts over NFS.
- Raw content: immutable SHA256 file blobs, processor versioning and exact/near duplicate evidence.
- OpenSearch 3.9 local dev: strict full-text index, built-in English/CJK analyzers, external-version fencing and logical tombstones.
- Query: validated typed DSL; hard filter `visibility=public` AND `is_indexable=true`, BM25 field boosts, bounded pagination. Semantic/hybrid search is not yet implemented.

## Local quick start (Python >=3.12 + Docker)

```bash
python -m pip install -e .
cp .env.example .env
# Change CRAWLER_USER_AGENT to your real, contactable crawler identity.
docker compose up -d opensearch
python -m search_engine.cli --database ./data/search.db init
python -m search_engine.cli --database ./data/search.db source demo https://example.org
python -m search_engine.cli --database ./data/search.db run demo https://example.org/
export CRAWLER_USER_AGENT='ExampleResearchCrawler/0.1 (+mailto:operator@example.org)'
python -m search_engine.cli --database ./data/search.db worker --max-steps 100
python -m search_engine.cli --database ./data/search.db process --max-steps 100
export OPENSEARCH_URL=http://127.0.0.1:9200
python -m search_engine.cli --database ./data/search.db index-init
python -m search_engine.cli --database ./data/search.db index --max-steps 100
python -m search_engine.cli --database ./data/search.db index-audit
python -m search_engine.cli --database ./data/search.db search 'example'
```

When running entirely within Compose, initialize DB and index via the API container, start the crawler and processor under `--profile crawl`, and start the indexer under `--profile index`. The `/v1/search` HTTP endpoint is read-only. Search backend credentials require TLS. **Do not expose the development Compose cluster to the public internet**: OpenSearch security is disabled and bound to loopback.

## Testing

```bash
PYTHONPATH=src python -W error::ResourceWarning -m unittest discover -s tests -v
python scripts/validate_design.py
docker compose config --quiet
docker build -t search-engine:phase3 .
# Against a fresh disposable OpenSearch container ONLY:
OPENSEARCH_URL=http://127.0.0.1:9200 python scripts/opensearch_live_smoke.py
```

Tests cover cross-phase crawling and document processing, full SQLite migrations, failed and delayed Bulk operations, lease fencing, tombstones, reindex alias cutover and bilingual queries. GitHub Actions runs a real OpenSearch integration job, separate from mocked tests. No claims of production relevance or throughput until measured.

## Index operations

- `index-init --index search-documents-v001`: explicit new index and read/write aliases; refuses overwriting existing aliases.
- `index --max-steps 100`: bounded outbox claims; 2xx per-item ACK, retryable 408/409/429/5xx and persistent dead letters on permanent mapping errors.
- `index-audit`: counts pending/dead and local receipt revision lag (not independent remote verification).
- `index-retry-dead`: operator action after fixing a root cause; accepts `--document-id`.
- `index-rebuild --new-index search-documents-v002 --offline-confirm`: **offline-only** rebuild after stopping writers and draining outbox; verifies count/snapshot and atomically switches read/write aliases.

## Documentation

- [Phase 1 Crawl Foundation](docs/11-PHASE1-IMPLEMENTATION.md)
- [Phase 2 Document Processing & Deduplication](docs/12-PHASE2-IMPLEMENTATION.md)
- [Phase 3 OpenSearch Indexing](docs/13-PHASE3-IMPLEMENTATION.md)
- [Gotchas](docs/gotchas.md)
- [Stanford Introduction to Information Retrieval](https://nlp.stanford.edu/IR-book/)

Architecture docs `docs/00` to `docs/10` describe future scope and remain distinct from the implementation. PR #1 (architecture) -> #2 (crawler) -> #3 (processing) -> #4 (indexing) must be reviewed in dependency order; **nothing is auto-merged**.

## Explicit limits

No complete Stanford relevance judgment set, semantic vectors, reranker, data-quality dashboard, production authentication/ACLs, online rolling reindex, multi-host worker coordination, PDF/OCR or long-term retention/erasure policy. Any new crawler deployment must respect site permission, robots.txt, rate limits and applicable terms.
