# 08 — Docker, Compose and Linux Deployment Design

## 1. Scope and environment distinction

Infrastructure files in root are **architecture scaffolding**, not evidence of working indexing/crawling. Dockerfile builds a minimal API scaffold and Compose starts API + single-node OpenSearch for local **development** after images are obtained. Worker/queue/indexer endpoints are not implemented in this documentation phase. Never claim an end-to-end crawl until the P0 code and integration tests exist.

Local layout: /data/sqlite (persistent metadata), /data/raw (immutable fetched bodies), OpenSearch named volume, application non-root UID. Keep secrets out of Git; use .env.example as a template.

## 2. Dockerfile specification

- Python 3.12 slim pinned **major/minor** initially; production lock full digest and dependencies.
- Install only pinned/reproducible runtime requirements, no compiler/build tools in runtime stage.
- Non-root UID; copy application code; invoke uvicorn by exec-form command; do not run migrations implicitly in every replica.
- Define liveness route and healthcheck; readiness requires SQLite+OpenSearch and is future implementation work.
- Python logs to stdout/stderr, termination by SIGTERM, bounded shutdown, external volumes.
- Separate api, scheduler, fetch-worker, processor, index-worker entrypoints in later phases; reuse image but run distinct commands.

## 3. Development Compose

OpenSearch single node with security plugin disabled is a **development-only** pattern documented by OpenSearch. Bind host ports to 127.0.0.1 if exposed at all; don't publicly bind 9200/9600. Use dedicated persistent volume. Set OPENSEARCH_JAVA_OPTS per actual RAM. Linux OpenSearch memory mapping frequently requires vm.max_map_count adjustment: verify running version documentation and host configuration before service start. Single-node 0-replica index is never HA.

Minimum startup order: OpenSearch healthy -> API; scheduler/worker deferred until implemented. Docker Compose depends_on readiness is useful but **does not** guarantee downstream dependency remains healthy after boot; code needs retries/circuit breakers.

## 4. Production hardening before launch

1. Enable TLS and authentication for OpenSearch (never deploy dev no-security Compose remotely), restrict network and RBAC, avoid hardcoded passwords.
2. Separate read/write service credentials; minimize API security permissions; admin endpoints authenticated.
3. Configure image digests, supply-chain scanning, CPU/memory caps, named volumes and encrypted backups.
4. Add persistent blob storage, retention, backups, snapshots, key rotation and restore drills.
5. Limit crawler outbound network to safe destinations and enforce SSRF defense in the fetcher; network segmentation alone is insufficient.
6. Use reverse proxy/TLS, body/rate limits, strict CORS and log redaction; enable graceful shutdown and container restart policy.
7. Monitor raw disk, OpenSearch watermark, SQLite write contention and index outbox lag.

## 5. Disaster recovery

Recover SQLite consistent snapshot plus blob store; instantiate clean OpenSearch generation and replay document versions/outbox. Verify active revision counts, samples, tombstone consistency, analyzers, multilingual relevance fixtures before alias switch. Reindex is documented and tested; OpenSearch snapshots alone do not replace source-of-truth backups.

## 6. Observability

Structured JSON logs keyed by crawl_run_id, url_id, document_id, task_id and trace_id; mask authorization headers, cookies, personal data and private URLs. Emit per-host denied/retry/backoff metrics without leaking sensitive query strings. Operator dashboard includes rate/lag/throughput/error taxonomy, OpenSearch health and search relevance regressions.

## 7. Validation commands (when runtime dependencies become available)

~~~bash
docker compose config
docker compose build
docker compose up -d
docker compose ps
docker compose logs --no-color --tail=100
curl -fsS http://127.0.0.1:8000/health/live
curl -fsS http://127.0.0.1:9200/_cluster/health
docker compose down
~~~

**NOT RUN** in this design review. CI and tests must report NOT RUN rather than implying passing builds.

## References

- https://docs.opensearch.org/latest/install-and-configure/install-opensearch/docker/
- https://www.sqlite.org/wal.html
