# 00 — Architecture, Boundaries and ADRs

## 1. Requirements and scope

An operator-approved, initially domain-focused web search engine. Sources include seed URLs, sitemaps, RSS, APIs and uploaded files. P0 is a **single-host** Linux deployment with multiple worker processes; P2 permits multiple worker hosts. This is **not** a claim of Google-scale completeness. Unknown demand, index size and traffic are open assumptions to measure.

Functional requirements: discover -> schedule -> fetch -> parse -> deduplicate -> version -> index -> search -> evaluate -> purge. Nonfunctional requirements: safe URL fetching, policy compliance, idempotence, recovery after crash, provenance, multilingual quality, explainable benchmarks and minimal operator burden.

## 2. Component diagram

~~~mermaid
flowchart LR
 Seeds[Seeds / Sitemaps / Feeds] --> Frontier[Frontier & host scheduler]
 Frontier --> Fetch[Fetch workers]
 Fetch --> Raw[(Raw blob store)]
 Fetch --> Proc[Parse/normalize/dedup]
 Proc --> DB[(SQLite authoritative state)]
 DB --> Outbox[Index outbox]
 Outbox --> OS[(OpenSearch projection)]
 UI[Search UI/API] --> Query[Query understanding]
 Query --> OS
 OS --> Rank[Merge/rank/highlight]
 Rank --> UI
 Metrics[Metrics and judgments] -.-> Frontier
 Metrics -.-> Rank
~~~

**Control plane:** SQLite owns sources, URL frontier, host policy, fetch history, documents, versions, jobs and outbox. **Data plane:** raw immutable blobs are separate from relational rows. **Search plane:** OpenSearch stores a rebuildable view. Search serving must not write crawl metadata as a side effect.

## 3. Stable identity and event contracts

- source_id: stable source scope; url_id: sha256 of source_id + normalized fetch URL; document_id: canonical page identity within source scope; version_id: immutable record for one logical document version.
- fetch_id: each HTTP attempt; blob_digest: sha256 over raw bytes; normalized_digest: sha256 over canonicalized substantive content; index_generation: deployed OpenSearch schema/analyzer generation.
- Every produced event has event_id, event_type, schema_version, producer, entity_id, occurred_at and idempotency_key. Store canonical UUID/hex strings; do not rely on timestamps for order.
- Invariant: an active indexed document references an existing document version; deletion creates an explicit tombstone; outbox op is replayable; a stale worker cannot acknowledge a newer lease.

## 4. Deployment and scaling decisions

| Dimension | P0 | P2 boundary |
|---|---|---|
| Task/store | SQLite WAL + durable job table | PostgreSQL durable coordinator |
| Worker location | Multiple processes, one Linux host | Multi-host worker fleet |
| Search | Single node OpenSearch, no external exposure | Secured OpenSearch cluster |
| Rate limits | Atomic per-host scheduler state | Central lease/rate limiter |
| Raw contents | Local volume via storage interface | Object storage |
| Indexing consistency | Transactional SQLite outbox | PostgreSQL outbox + broker |
| Semantic | Deferred | Separate embedding workers/ANN |
| Shard count | 1 primary, 0 replicas in DEV only | Size/load-tested shards and replicas |

**ADR-001:** SQLite+OpenSearch selected for simple P0. Risk: single writer, finite throughput, backups, cannot share WAL over NFS. Exit criteria: queue contention, operational limits, high availability or distributed workers required. Mitigation: repository layer interfaces, SQL migrations, outbox event contracts.

**ADR-002:** OpenSearch is a materialized index, never the source of truth. Supports full rebuild from persisted documents and immutable blobs.

**ADR-003:** No unauthorized anti-bot circumvention. Prefer supported APIs or permission, adjust crawl cadence, stop on challenges.

**ADR-004:** Document-level ACL/visibility are hard constraints applied **before** lexical/vector search and again before response serialization. P0 can restrict to public-only if ACL cannot be correctly implemented.

## 5. Reliability principles

At-least-once task delivery + idempotent effects, not mythical exactly-once execution. Raw blob write first; transaction commits version + outbox + job transition next; indexer bulk result is reconciled individually. On crash, expired leases are reclaimable. Recover gaps with reconciliation: active SQLite versions vs OpenSearch IDs and generations.

Monitoring: discovered URLs; allowed/blocked/error/304 fetches; per-host request rate; queue age and lease expirations; processing throughput; outbox lag; index document count drift; search p50/p95/p99, 5xx; Recall@K and nDCG@10. Alerts need a measurable baseline.

## 6. Security and ownership

Reject SSRF: only http/https, validated host/port, resolve DNS and reject loopback/private/link-local/metadata ranges, repeat validation per redirect, pin validated destination for connection where supported. Bound redirects, body size, timeout, decompression ratio; sanitize parser outputs. Treat HTML as hostile and never execute embedded scripts by default. Disable untrusted active code rendering. Respect deletion and privacy requests; define retention for raw copies, logs and snapshots. Encrypt secrets in production, no secrets in Git.

## 7. Explicit non-goals for P0

Global crawling, CAPTCHA evasion, arbitrary JS rendering, personalized ranking, opaque LLM-based quality scores, cross-host SQLite usage, all-at-once microservices, unfounded scale/SLO claims.

## 8. P0 service/API contracts (proposed)

- POST /v1/sources: create an approved source scope and seeds.
- POST /v1/crawl-runs: submit crawl configuration with idempotency key.
- GET /v1/crawl-runs/{id}: progress and error groups.
- GET /v1/search?q=&lang=&page_size=&cursor=: validated, length/rate limited query.
- GET /health/live; GET /health/ready: readiness verifies SQLite open and OpenSearch connectivity.
- Admin endpoints require authentication; public search endpoints have abuse protection.

Architecture acceptance gates are defined in 09-ROADMAP-EVALUATION.md. All functionality is design-only until tested.
