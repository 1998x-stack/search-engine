# Gotchas — engineering notebook

| ID | Severity | Issue or risk | Evidence | Resolution/status |
|---|---|---|---|---|
| G-001 | P0 | SQLite WAL cannot share NFS across hosts | SQLite WAL documentation | single-host only; DESIGN RISK |
| G-002 | P0 | Stale indexing writes can revive a deletion | versioned outbox design | Phase 2; NOT IMPLEMENTED |
| G-003 | P0 | Parallel HTTP workers bypass host interval | design review | atomic host_leases; local race test PASS |
| G-004 | P0 | DNS rebinding/internal metadata access | security design | vetted public IP, pinned socket, scoped port; local policy tests PASS, external fuzz NOT RUN |
| G-005 | P0 | CAPTCHA/403 escalation violates site policy | design review | stop, do not evade; local fixture PASS |
| G-006 | P1 | Chinese analyzer mismatch | architecture review | Phase 3; NOT IMPLEMENTED |
| G-007 | P1 | Similarity chaining joins unrelated docs | architecture review | Phase 2; NOT IMPLEMENTED |
| G-008 | P0 | Docker build needs README/schema | initial design review | COPY README and schema, CI build required |
| G-009 | P0 | Dev OpenSearch security disabled | compose design | localhost-only, NOT production-safe |
| G-010 | P0 | Docs-only CI not proof of crawler | phase boundary | new deterministic local HTTP integration tests |
| G-011 | P0 | Numeric hostname (`2130706433`) passed initial URL validation | local failing test | reject noncanonical numeric IPv4 host; regression test PASS |
| G-012 | P0 | HTTP 200 HTML robots verification page could appear empty and permit all pages | design inspection | robots content type and HTML challenge detection; fixture test PASS |
| G-013 | P1 | Robots percent-encoding and multi-agent edge cases | design inspection | conservative P0 matcher, differential tests before production |
| G-014 | P1 | Expired permit/worker with blocking network can cause transient over-concurrency | design inspection | timeout < permit lifetime, fault/load tests pending |
| G-015 | P1 | Incomplete migration/backup/restore on populated data | design review | test via SQLite backup API before production |

When recording new incidents: date, commit, environment, exact repro, root cause, regression test, fix and remaining risk. Distinguish observed failures from possible hazards.

## Phase 2 additions

| ID | Severity | Finding | Mitigation/status |
|---|---|---|---|
| G-018 | P0 | New schema could mask v2 migration if run before version inspection | `Database.initialize()` reads version first; separate atomic 0003 migration, fixture test PASS |
| G-019 | P0 | Index outbox insert failure could leave a committed new revision | document/version/outbox/job ACK in one SQLite transaction; injected failure/rollback test PASS |
| G-020 | P0 | Old queued fetch overwrites a newer document version | durable fetch row order compared before version writes; stale replay test PASS |
| G-021 | P0 | Duplicate edges remain authoritative after content changes | invalidate old duplicate decisions on update/delete/noindex; regression PASS |
| G-022 | P0 | HTTP 304 should not enqueue extraction or increase revision | crawler-to-outbox fixture test PASS |
| G-023 | P1 | SimHash LSH candidate limit can silently miss duplicates | bounded 200 candidates; report recall loss and tune on labeled corpus; DESIGN RISK |
| G-024 | P1 | HTML boilerplate strip loses meaningful text on unusual pages | preserve raw bytes, version extractor, corpus-level evaluation; DESIGN RISK |
| G-025 | P1 | Orphan raw/text blobs after failed transaction | retention-aware GC after source/version audit; NOT IMPLEMENTED |
| G-026 | P0 | Concurrent worker startup can race on SQLite v2→v3 ALTER TABLE | advisory same-host file lock around schema version check+upgrade; 6 simultaneous initializer regression PASS |

## Phase 3 index design findings

| ID | Priority | Finding | Action/status |
|---|---|---|---|
| G-018 | P0 | Physical deletes lose tombstone external-version history and can permit stale delayed upserts | Use content-free logical tombstones with `external_gte`; verify 410/noindex and replay tests |
| G-019 | P0 | Bulk HTTP 200 can contain item-level errors | Parse every Bulk item, per-item ACK/retry/dead-letter; regression test |
| G-020 | P0 | Indexer crash after remote success but before SQLite receipt | At-least-once replay with external_gte, lease epoch fencing; regression test |
| G-021 | P0 | Offline reindex can race live processor/indexer | Require operator quiescence; check pending outbox and SQLite version snapshot, short writer fence; full online safety NOT IMPLEMENTED |
| G-022 | P1 | Logical tombstones still leave historical bytes in segments/snapshots | Formal purge/retention/backup erasure policy and restore drills NOT IMPLEMENTED |
| G-023 | P1 | SQLite receipts are not an independent OpenSearch audit | Remote `_mget` revision audit and missing-doc repair deferred |
| G-024 | P1 | CJK analyzer, OpenSearch version and JVM memory must be tested on real image | Dedicated live OpenSearch CI smoke; Linux production sizing not tested |
| G-025 | P1 | Public-only search with no tenant ACL or indexers for restricted data | Explicit `visibility=public` filter, reject restricted ingestion until authenticated ACL exists |

| G-026 | P0 | Reindex previously batched only by document count, risking oversized Bulk NDJSON | Add 4MB byte-capped batches and regression test; PASS locally |

## Phase 4 query and relevance risks

| ID | Priority | Finding | Mitigation |
|---|---|---|---|
| G-027 | P0 | Query expansion may broaden beyond user hard constraints | bool.filter independent from scoring; safety tests cover all rewrite modes |
| G-028 | P0 | Malformed/expensive user-supplied DSL | bounded typed parser, no direct OpenSearch DSL, clause and paging limits |
| G-029 | P1 | Spelling suggestions can alter rare code identifiers | suggestion only, never automatically executed; tiny curated vocabulary |
| G-030 | P1 | Chinese lexicon-based segmentation can be inaccurate | diagnostics only; OpenSearch CJK analyzer remains authoritative |
| G-031 | P1 | Synthetic relevance metrics can be mistaken for quality improvement | mark fixtures synthetic; human judged holdout required |
| G-032 | P1 | Site filter semantics could surprise users | exact hostname, not broad suffix wildcard |

## Phase 5 — Vector index and fusion

| ID | Severity | Hazard | Mitigation/status |
|---|---|---|---|
| G-035 | P0 | Stale ANN vector after 410/noindex leaks a deleted document | authoritative SQLite latest-revision check before presentation, logical tombstone projection; regression + OpenSearch smoke PASS |
| G-036 | P0 | Old vector worker overwrites newer revision | external_gte revision fencing, conditional checkpoint update; unit test PASS |
| G-037 | P0 | Vector search returns unauthorized results after filters | Lucene HNSW inline bool.filter (public/noindex/source/site/lang/date/model); unit and live tests PASS; production ACLs NOT IMPLEMENTED |
| G-038 | P1 | Model revision drift produces incomparable embeddings | model_key fingerprint, query-time model_key filter; pin immutable model revisions in production; runtime model download NOT RUN |
| G-039 | P1 | Embedding inference cold start/memory costs | opt-in separate semantic Docker image, cache volume; benchmark load and resource limits NOT RUN |
| G-040 | P1 | RRF improves synthetic scores but regresses real relevance | synthetic-only evaluation fixture, require human qrels and per-language holdout/ablations |
| G-041 | P1 | Cross-Encoder default MS MARCO model is English-first | opt-in rerank; evaluate zh/mixed separately before enabling; model execution NOT RUN |
| G-042 | P1 | Simple vector receipt scan is not multi-worker durable lease queue | single worker only; full reconciliation; distribute later using leased outbox |
| G-043 | P0 | Recreating vector index while retaining SQLite receipts hides missing vectors | explicit vector-reset-receipts --confirm + vector-sync under writer quiescence, no automatic online alias migration |
| G-044 | P1 | Exact phrase/must-not semantics cannot be guaranteed by ANN candidate fusion | fallback to lexical-only for structured queries, preserving constraints; regression PASS |
| G-045 | P1 | Only 20 candidates per route bounds fusion recall | report candidate-only total, measure Recall@100 once candidate budget is expanded |
