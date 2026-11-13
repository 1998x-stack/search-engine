# Gotchas — Design Assumptions, Not Observed Runtime Incidents

This file is a living engineering notebook. **None of the entries below is a verified production failure.** During implementation add date, environment, command, observed error, reproduction, root cause, regression test, fix commit and status. Never claim unexecuted tests are green.

| ID | Severity | Design hazard | Mitigation | Status |
|---|---|---|---|---|
| G-001 | P0 | SQLite WAL copied across machines/NFS | single-host only; move control plane to PostgreSQL | DESIGN RISK |
| G-002 | P0 | Stale index UPSERT can resurrect deleted documents | monotonic version and reconciler; test tombstone expiration | DESIGN RISK |
| G-003 | P0 | Independent fetch workers bypass host politeness | atomic central host token reservation | DESIGN RISK |
| G-004 | P0 | Redirect/DNS rebinding to internal metadata service | validate every redirect and actual connect IP | DESIGN RISK |
| G-005 | P0 | Challenge/403 retried indefinitely or evaded | stop, record denial, seek permission/API | DESIGN RISK |
| G-006 | P1 | Chinese analyzers differ between index and query | versioned _analyze fixture tests | DESIGN RISK |
| G-007 | P1 | Duplicate similarity chaining wrongly merges pages | pairwise/representative checks, reversible decisions | DESIGN RISK |
| G-008 | P0 | Docker package installation fails if README absent | Dockerfile explicitly COPY README.md | DESIGN REVIEW FIX |
| G-009 | P0 | Local compose uses disabled OpenSearch security | bind 127.0.0.1; never reuse in production | DESIGN RISK |
| G-010 | P0 | Tests absent despite green documentation | CI static/SQLite tests separated from E2E | DESIGN RISK |

## Incident template

- Date/commit/host:
- Preconditions and fixture:
- Exact repro commands:
- Expected vs actual:
- Root cause and evidence:
- Fix and linked PR:
- Regression test and observed output:
- Remaining risk:
