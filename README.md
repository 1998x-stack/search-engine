# Search Engine

> Documentation-first, bilingual (Chinese/English) information retrieval system design. **Status: architecture proposal; not a completed crawler/search service.**

## Goal

Build a reproducible domain-focused web search engine covering authorized acquisition, document processing, exact/near deduplication, multilingual indexing, query interpretation, hybrid retrieval, and relevance evaluation. Prioritize correctness, operability, and measured relevance before web-scale complexity.

**P0 architecture:** Python workers + SQLite WAL (single machine, authoritative crawl/task/document metadata) + immutable raw-object storage adapter + OpenSearch (derived, rebuildable search index). **P1:** embeddings and reranking. **P2:** PostgreSQL coordinator + distributed workers.

## Architecture at a glance

~~~text
Seed/Sitemap/RSS/API -> URL Frontier -> robots/politeness -> Fetch -> Raw store
                                                      -> Parse -> Normalize -> Dedup
                                                      -> SQLite documents/versions + Outbox
                                                      -> OpenSearch index aliases
Query -> normalize/identify language -> BM25 (+ optional kNN) -> RRF
      -> rerank -> ACL check / snippets / pagination -> Results
~~~

SQLite and OpenSearch have **different roles**: SQLite holds crawl state, durable leases, canonical metadata and the index outbox; OpenSearch is a disposable search projection, **not** the queue or canonical source of truth. SQLite WAL permits concurrent readers and one writer on one host, not multi-host sharing over NFS.

## Documentation index

| Document | Purpose |
|---|---|
| [00 Architecture](docs/00-ARCHITECTURE.md) | System boundaries, ADRs, invariants, SLIs |
| [01 Crawling](docs/01-CRAWLING.md) | Discovery, robots, rate limits, distributed frontier |
| [02 Processing & Dedup](docs/02-PROCESSING-DEDUP.md) | Parsing, exact/near duplicates, canonicalization |
| [03 Indexing](docs/03-INDEXING.md) | Schema, analyzers, updates, zero-downtime reindex |
| [04 Query Understanding](docs/04-QUERY-UNDERSTANDING.md) | Chinese/English, mixed language, query planning |
| [05 Retrieval & Ranking](docs/05-RETRIEVAL-RANKING.md) | BM25, vector, RRF, reranking |
| [06 Background Jobs](docs/06-BACKGROUND-JOBS.md) | Persistent jobs, leases, retries, outbox |
| [07 Data Model](docs/07-DATA-MODEL.md) | Entity model, SQLite DDL, invariants |
| [08 Deployment](docs/08-DEPLOYMENT.md) | Dockerfile, Compose, Linux and security |
| [09 Roadmap & Validation](docs/09-ROADMAP-EVALUATION.md) | P0-P2 gates, datasets, failure injection |
| [10 Stanford IR Mapping](docs/10-STANFORD-IR-MAPPING.md) | Textbook chapter-to-design traceability |

Companion design artifacts: [SQLite schema](schema/sqlite.sql), [OpenSearch index definition](infra/opensearch/documents-v1.json), [Dockerfile](Dockerfile), [Compose](compose.yaml), [environment sample](.env.example). Treat them as **proposed scaffolding** until validated end to end.

## Decisions

1. **Scope:** start from allowlisted domain/seed sets, not unrestricted public-web crawling.
2. **Ethics:** respect robots.txt and site terms; do not bypass CAPTCHAs, logins, or blocklists.
3. **Durability:** store raw fetches and document versions before acknowledging work.
4. **Consistency:** transactional outbox + idempotent index document IDs; retries are normal.
5. **Ranking:** ship lexical relevance and judgment sets before optional semantic search.
6. **Evolution:** PostgreSQL and shared broker are a migration, not early requirements.

## First engineering milestone (not yet implemented)

Build a local fixture-driven crawl of authorized sample pages; index 1,000 reproducible documents; demonstrate UTF-8 Chinese/English search, recrawl, 304, update, delete, idempotent recovery, and deterministic relevance reports. See [test gates](docs/09-ROADMAP-EVALUATION.md).

## Sources

- Stanford, *Introduction to Information Retrieval*: https://nlp.stanford.edu/IR-book/
- RFC 9309 Robots Exclusion Protocol: https://www.rfc-editor.org/rfc/rfc9309.html
- SQLite WAL: https://www.sqlite.org/wal.html
- OpenSearch documentation: https://docs.opensearch.org/latest/
