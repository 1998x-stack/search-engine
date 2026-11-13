# 06 — Durable Background Jobs and Crawl Coordination

## 1. Why not a background thread or OpenSearch queue?

A crawler must survive API restarts, process crashes, rate limits, noindex deletes and reindex. **Never** use OpenSearch as a task queue. P0 uses SQLite as a durable state+queue with a short-transaction single-writer bottleneck; P2 PostgreSQL coordinator can support distributed leases and optional broker wake-ups.

Queues are **at least once**. Every handler must be idempotent, atomic at state-boundaries, or reconcile after partial success.

## 2. Task contract and states

Task fields: id (UUID), kind, entity_type/id, payload_json, priority, scheduled_at, state (ready/leased/succeeded/retry/dead/cancelled), attempt, max_attempts, lease_owner, lease_epoch, leased_until, idempotency_key unique, created/updated_at, last_error_class and correlation_id. State transitions:

~~~text
READY -> LEASED -> SUCCEEDED
             |         |
             -> RETRY --+
             -> DEAD
             -> CANCELLED
RETRY (due) -> LEASED; expired lease -> RETRY or DEAD
~~~

Task kinds: DISCOVER_SITEMAP, DISCOVER_LINKS, REFRESH_ROBOTS, FETCH_URL, EXTRACT_CONTENT, DEDUP_DOCUMENT, WRITE_DOCUMENT, INDEX_OUTBOX_DRAIN, DELETE_DOCUMENT, RECONCILE_INDEX and GC_BLOBS. Avoid creating tasks for every tiny pure function; one pipeline handler may run pure steps inside a single bounded transaction boundary.

## 3. Claim algorithm (SQLite >=3.35)

One worker at a time executes a **brief** BEGIN IMMEDIATE transaction, selects eligible ready/retry (or expired leased) jobs by priority and due time, checks attempt budget, sets state=leased, increments attempt AND lease_epoch, sets leased_until = now + visibility_timeout and lease_owner, then COMMIT. Worker runs network/CPU outside the transaction. On ACK verify id, owner and lease_epoch; if mismatch, reject stale ACK. Heartbeat renewals also require owner/epoch. For expired lease, increment epoch on next claim (fencing). The lease doesn't stop a previously issued HTTP request: all writes require version checks.

Use timestamps in UTC. In SQLite, epoch_ms INTEGER is preferred for atomic comparisons; do not rely on unordered lexicographic locale timestamps. Add unique idempotency_key and scheduled_at/priority index. A high-priority stream must not starve old low-priority tasks: use aging and source fair quotas.

SQL sketch (for explanation; actual production claim must include host budget admission):

~~~sql
BEGIN IMMEDIATE;
SELECT id FROM jobs
 WHERE (state IN ('ready','retry') AND scheduled_at_ms<=:now)
    OR (state='leased' AND leased_until_ms<=:now)
 ORDER BY priority DESC, scheduled_at_ms ASC LIMIT 1;
UPDATE jobs SET state='leased', attempt=attempt+1,
 lease_epoch=lease_epoch+1, lease_owner=:worker,
 leased_until_ms=:deadline, updated_at_ms=:now
 WHERE id=:id;
COMMIT;
~~~

The transition is atomic only inside that short transaction; SELECT/UPDATE must enforce availability and max_attempts in production. See schema DDL for state and index fields.

## 4. Separation of queues and host politeness

FETCH_URL tasks must additionally obtain a host token using a transaction that checks host.next_allowed_at_ms and inflight ceiling. One claim without a host token cannot fetch. Host token must expire/recover on worker crash; update host state when acquiring and releasing; never allow concurrency overruns through duplicate reservations. Prefer dedicated host dispatcher and workers with one token per eligible host P0.

HTTP retries: bounded exponential backoff with full jitter, Retry-After override, per-host circuit breaker. Permanent robots denial, unauthorized/login and 403 challenges are not transient retries. Index retries: 429/5xx, checkpoint per bulk item, DLQ on mapping/validation errors. Always classify and cap attempts by kind.

## 5. Transactional outbox

Within the same SQLite transaction as a new document_version: INSERT index_outbox(op_id, document_id, revision, op_type, payload_ref, status='pending'). Indexer drains outbox and persists status after confirmed remote response. Crash window before remote response -> retry idempotently; crash after remote success before ACK -> repeat safely. Network success but stale retry -> version fencing/reconciliation.

A document deletion emits INDEX_DELETE as a monotonic revision. A later stale INDEX_UPSERT must not resurrect it. Periodically compare SQLite latest revisions/tombstones to OpenSearch projection. Never remove a durable tombstone until a safe retention/reconciliation policy is proven.

## 6. Shutdown, recovery and cancellation

On SIGTERM: stop taking new jobs; finish bounded in-flight work; heartbeat until completion; release/let expire leases; flush logs. On restart: reclaim expired leases and run reconciliation. User cancellation: mark run cancelled and prevent all new claims for that run; best-effort cancel in-flight work without leaving partial durable state. Backpressure when raw storage, DB write latency or OpenSearch queue lag grows.

## 7. Distributed migration P2

PostgreSQL SKIP LOCKED can claim work across hosts; migrate SQLite tables using stable IDs, event contracts and transactional cutover. Broker (Redis Streams/NATS JetStream) only wakes workers and provides non-authoritative delivery; dedupe on coordinator idempotency keys. Host robots and rate limiter remain a **shared authoritative** policy. SQLite can still serve as per-worker non-authoritative spool but is never cross-host ground truth.

## 8. Fault-injection tests

SIGKILL at claim/fetch/blob-write/version-commit/index-bulk/ack; 2 worker race; clock skew; lease expiration + old worker late success; duplicate event; bulk partial failure; disk full; SQLite busy; stale robots; 429 backpressure; cancellation; reindex and live writes concurrently; multi-host coordinator simulated. For every fault assert no lost document, no unauthorized request, eventual latest projection and bounded retries.
