# 09 — Phases, Engineering Gates, Evaluation and Risks

## P0: single-host lexical search foundation

**Scope:** fixture-first seeds/sitemaps, RFC 9309 robots, host budgets, HTTP conditional fetching, HTML parsing, exact dedup, normalized document versions, SQLite durable jobs/leases/outbox, OpenSearch explicit index, BM25 zh/en/mixed query, safe search API, Linux Docker, CLI seeding and monitoring.

**P0 exit gates (objective):**
- Authorized fixture crawl of >=1,000 documents with deterministic seed, manifest and hashes; all fixture hosts obey robots, no cross-scope fetch and no SSRF escape.
- Re-run same crawl twice: idempotent canonical docs, no unexpected duplicate versions; 304 verified; changed text produces revision; 404/410/noindex tombstones become absent from result set.
- Kill/restart workers at fetch, version commit, index bulk, ACK: no permanent lost data; latest revision wins; max retries and DLQ enforced.
- Multi-process one-host queue stress: no double ACK, host in-flight never exceeds configured limit, no starvation on measured fixtures.
- Chinese/English/mixed analyzers pass expected-token fixtures and at least 30 graded judgments per language (90 total). Report nDCG@10/Recall@100/MRR@10 baseline; do **not** invent targets from thin air.
- Bench on specified machine/corpus: p50/p95/p99 and index update visibility; initial candidate gate p95 <300ms at 10K docs, refine from actual measurements. 100% scoped ACL/public-policy checks.
- Docker Compose build/up/down + health; SQLite migration, backup/restore; OpenSearch index rebuild; security checks. Every not-run test explicitly marked NOT RUN.

## P1: hybrid relevance

Add embeddings with fixed model/version/dimension, ANN, chunking and document-level fusion, RRF, Cross-Encoder reranker, stronger Chinese analysis and expanded 100+ judgments per language group. Must demonstrate statistically defensible per-intent lift (and disclose regressions), lexical fallback, latency budget and no ACL leaks. Compare BM25-only, hybrid, reranker ablations.

## P2: distributed scale

Migrate to PostgreSQL coordinator with stable IDs/outbox and a tested cutover/rollback, distributed worker fleet, centrally serialized host politeness, optional durable wake-up broker, object store, OS multi-node with secured endpoints, backups and failover. Gate on measured SQLite bottleneck/availability requirement, not speculation.

## Evaluation protocol

Split query judgments into development/holdout datasets to prevent overfitting; graded relevance 0/1/2/3, blind judge guidelines, adjudication for disagreement. Report per-language, query intent, domain, document length and novelty. Version corpus snapshot, analyzers, synonym sets, embedding model and index generation.

Offline metrics: Precision@10, Recall@100, MRR@10, nDCG@10, zero-result, duplicate rate, freshness lag. Online: p95/p99 latency, error rate, index lag, backpressure, SLO burn rate, guardrail violation count and informed user satisfaction (not just CTR).

## Failure-injection suite

- Crawl: robots 500, robots disallow, 429 Retry-After, redirect to 169.254.169.254, DNS rebinding, hidden 10MB decompressed response, forbidden extension, challenge page.
- Storage: SQLite SQLITE_BUSY, WAL checkpoint starvation, disk full, incomplete raw blob, DB backup during writes, interrupted migration.
- Queue: two concurrent claims, duplicate idempotency key, expired lease + old worker, SIGKILL after side effect, cancellation, malformed task, starvation.
- Index: bulk partial errors, alias flip, refresh gap, stale upsert after delete, OpenSearch down, mapping mismatch, vector model incompatibility.
- Ranking: multilingual ambiguity, no-result fallback, exact CVE identifier, query injection, zero-ACL leakage, rerank outage, pagination consistency.

## Risk register

| Risk | Impact | Prevention | Trigger to revisit |
|---|---|---|---|
| Single SQLite writer | contention and queue lag | short writes, bounded concurrency, metrics | lock wait / lag exceeds budget |
| Robots/policy drift | improper access | RFC parser, cache TTL, fail-closed policy | deny/regression in fixtures |
| URL explosion | infinite frontier | scope, query normalization, depth/page budgets | high URL/doc ratio |
| Boilerplate duplicates | poor relevance | extraction & pairwise tests | duplicate top-10 spikes |
| Analyzer mismatch | zh recall failures | _analyze fixtures, versioned mapping | token / recall regressions |
| Outbox lag/stale upsert | outdated results | revision fencing + reconciliation | index drift/reconciliation alert |
| Search cost | OOM/latency | staged candidates, bounded clauses | p95/cost threshold breach |
| Unverified docs | unsafe deployment | staged gates, NOT RUN labels | CI or E2E missing |

## Implementation sequence

1. Add source scope validation, crawl fixtures, SQLite migrations and durable queue with tests.
2. Implement robots/host budgets, safe fetcher, conditional HTTP and raw store; write failure cases first.
3. Implement extraction/canonicalization/hashes, document revisions and outbox; validate idempotence.
4. Create OpenSearch mappings and indexer; lexical QueryPlan and search API.
5. Add Chinese/English evaluation corpus, benchmark and Compose smoke tests.
6. Only then start P1 relevance experiments and P2 distributed work.

## Source of truth for progress

This document defines **planned** acceptance gates. At this publication time, no integration/latency/relevance/load tests have been run. Tests must only be marked PASS after observed runs with preserved command/log evidence.
