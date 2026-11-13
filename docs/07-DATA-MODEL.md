# 07 — Entities, SQLite Schema and State Ownership

**Executable proposal:** schema/sqlite.sql. Database uses SQLite >=3.35, foreign_keys=ON, WAL, busy_timeout, explicit migrations and periodic checkpoint/backup. WAL setup is connection/startup configuration, not a substitute for backups.

## 1. Entity relationships

~~~mermaid
erDiagram
 SOURCES ||--o{ URLS : scopes
 HOSTS ||--o{ URLS : belongs_to
 URLS ||--o{ FETCHES : attempts
 DOCUMENTS ||--o{ DOCUMENT_VERSIONS : evolves
 URLS }o--o| DOCUMENTS : resolves_to
 DOCUMENT_VERSIONS ||--o{ INDEX_OUTBOX : projects
 SOURCES ||--o{ CRAWL_RUNS : runs
 CRAWL_RUNS ||--o{ JOBS : schedules
 DOCUMENTS ||--o{ DUPLICATE_RELATIONS : compares
~~~

Cardinalities are conceptual; duplicate relations store left/right document IDs. Fetches reference requested URL, payload digest and optional derived version; raw bytes reside in a blob store. Documents are scoped by source and have canonical URL / latest revision. URL aliases reference documents but never destroy original provenance.

## 2. Authoritative tables

| Table | Purpose / important constraints |
|---|---|
| sources | scope, policy, allowlist, owner, enabled |
| hosts | authority host, robots digest/status, next_allowed_at, inflight |
| urls | source+normalized URL uniqueness, discovered_from, document_id |
| crawl_runs | operator run, state, start/end/cancel |
| fetches | immutable HTTP attempt record, status/error, ETag/Last-Modified, raw blob pointer |
| documents | document_id, source, canonical_url, latest_revision, deleted_at |
| document_versions | (document_id, revision) uniqueness, text hash, normalized blob ref, parser version, lang |
| duplicate_relations | pairwise candidate/decision with algorithm and evidence |
| jobs | durable claims, lease fencing, attempt budget |
| index_outbox | immutable document revision index op and ACK lifecycle |

## 3. State ownership and critical transactions

**A. Discovery:** upsert normalized URL under (source_id,normalized_url), then INSERT job with deterministic idempotency key (source/url/action/revisit epoch). Ignore duplicate enqueue but record provenance. All in one transaction.

**B. Fetch completion:** commit fetch audit entry, blob digest pointer and next PROCESS task. Raw object durability must be confirmed first. 304 updates last_seen but does not create a new content version.

**C. Document commit:** compare expected latest_revision under write lock; INSERT version; UPDATE documents revision; INSERT outbox operation; ACK the processing job. Handle retries where exact same processed input already became current (idempotent no-op).

**D. Outbox ACK:** record remote item outcome and last error, conditionally ACK current lease. Reconcile records separately; OpenSearch is not a transactional participant.

**E. Delete:** add tombstone version/revision, clear searchable fields, enqueue delete and purge raw/derived blobs according to retention policy; keep minimal no-content audit.

## 4. Representative identity

source_id is a boundary, preventing same URL in disjoint tenants/scopes from accidental collision. url_id is stable across recrawls but not necessarily across intentional canonical-mapping changes. document_id stable across content revisions. version revision INTEGER increments monotonically. Duplicate clusters are an optional presentation layer; never use exact/near-duplicate group ID as the primary key of documents.

All times are UTC epoch milliseconds; language BCP-47 code; hashes lowercase hex sha256; user metadata stored in validated JSON and versioned. Source config JSON must be schema validated by API. Payload size bounds to prevent sqlite bloat.

## 5. Migrations, backup and capacity

- Use PR-reviewed ordered SQL migrations; enforce schema version via PRAGMA user_version or migration table and refuse unsupported future versions.
- SQLite backup API or consistent checkpoint/snapshot; **do not** copy only main db while WAL contains uncheckpointed writes.
- Set busy_timeout and keep write transactions short. Measure lock wait, WAL file size, checkpoint starvation.
- Index and raw blob rehydration must work from SQLite+raw backups; restoring only OpenSearch is insufficient.
- No NFS/shared network mount for WAL. P2 moves authoritative state to PostgreSQL and shares object storage.
- Plan privacy and deletion consistency across backups/TTL/observability.

## 6. API serialization

Use source-scoped document IDs, immutable version envelopes, explicit type-safe enums and JSON schema versions. Avoid exposing raw foreign keys, privileged source settings and private job payloads in public search responses.

## 7. Proposed indexes

Cover jobs(state, scheduled_at_ms,priority), jobs(leased_until_ms), urls(source_id, normalized_url), urls(host_id, next_fetch_at_ms), document_versions(document_id,revision), index_outbox(status,scheduled_at_ms), fetches(url_id,fetched_at_ms). See actual DDL.

## References

- https://www.sqlite.org/wal.html
- https://www.sqlite.org/lang_createtable.html
