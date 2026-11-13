# Phase 1 implementation: crawl foundation

## Deliverables

| Component | Implemented | Critical bound |
|---|---|---|
| SQLite WAL schema v2 | source, run, URL, fetch, job, host, robots, lease | single-host; short BEGIN IMMEDIATE |
| Durable job queue | claim, heartbeat, fenced ACK, retry/defer/dead, cancel, finalize | at-least-once; leased jobs may be reclaimed |
| URL Frontier | source scope, canonical URL, discovered-link enqueue, max_urls/depth/links | no unapproved cross-host traversal |
| Robots | cached 24h (short denial TTL), per-path matcher, fail-closed on errors/challenges | conservative subset; audit RFC9309 edge cases |
| Host politeness | shared host permits, min interval, max in flight, lease expiry | default 1 concurrency / 1000 ms |
| HTTP | strict scheme/port, DNS global-only, connect to vetted IP, TLS host verification | 8s timeout, 2MB wire/5MB decoded |
| Status handling | 200,304,301–308,401,403,404,410,429,5xx | redirects enqueue separate scoped tasks |
| Immutable blob store | content SHA-256 and atomic rename | filesystem storage, GC deferred |
| CLI | init, source, run, worker, status | operator shell; no unauthenticated admin HTTP |
| Tests | local HTTP fixture, race, data constraints | CI runs, no public-web crawling |

## Worker dataflow

`claim -> reserve host permit -> refresh cached robots (only if stale) -> defer -> validate robots path -> conditional HTTP request -> content-limit check -> blob fsync/rename -> SQL fetch+URL state+discovery+ACK transaction -> release permit`.

Status semantics: denied robots/403 => blocked without evasion; 429/503 => bounded retry and per-host cooling period; 304 => prior content remains; 404/410 => URL deleted; each redirect => a newly enqueued in-scope task (not an implicit extra request). Repeated URLs per run use unique idempotency keys.

## Deterministic testing

`PYTHONPATH=src python -m unittest discover -s tests -v` operates offline with a temporary SQLite DB and in-process HTTP fixture. The fixture uses `allow_private_ips=True` **only inside unit/integration tests**; the production CLI defaults to `False`. CI performs SQL contract checks and Docker build separately.

## Limitations and future hardening

- Robots matching is a conservative P0 subset; require differential RFC9309 test corpus, especially percent-encoded path octets and grouped agents, before unrestricted operation.
- DNS resolution is vetted for public IPs and socket pinned, but outbound network policy and adversarial TLS/HTTP fuzzing remain needed for production.
- In-process fixture tests are not full systemd/Linux Docker E2E. No remote public crawl was performed.
- Per-host permits expire after request budget; test slow/hung transport on target deployment and set lease margins accordingly.
- Revisit run/URL budget counting at scale and add time/byte/crawl-page budgets.
- SQLite WAL cannot be used for cross-host workers. PostgreSQL migration remains Phase 2+ architectural requirement.
- Full job fairness, structured metrics, graceful draining, automated migrations and backups remain P0 follow-up hardening tasks.
- Index/document version/outbox remain schema placeholders, **not active processes**.

## Next phase

Implement parser/extractor normalization, canonical URL hints, exact SHA-256 duplicate detection, near-duplicate verification and document revision+outbox, then OpenSearch index writes and lexical evaluation. Do not claim this functionality exists in Phase 1.
