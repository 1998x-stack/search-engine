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
| G-011 | P0 | Numeric hostname (2130706433) passed initial URL validation | local failing test | reject noncanonical numeric IPv4 host; regression test PASS |
| G-012 | P0 | HTTP 200 HTML robots verification page could appear empty and permit all pages | design inspection | robots content type and HTML challenge detection + persistent status guard; fixture test PASS |
| G-013 | P1 | Robots percent-encoding and multi-agent edge cases | design inspection | conservative P0 matcher, differential tests before production |
| G-014 | P1 | Expired permit/worker with blocking network can cause transient over-concurrency | design inspection | timeout < permit lifetime, fault/load tests pending |
| G-015 | P1 | Incomplete migration/backup/restore on populated data | design review | test via SQLite backup API before production |

When recording new incidents: date, commit, environment, exact repro, root cause, regression test, fix and remaining risk. Distinguish observed failures from possible hazards.
