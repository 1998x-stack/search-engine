# Phase 3 — OpenSearch Index Engine & BM25 Lexical Baseline

**Status:** code implemented with offline tests; live OpenSearch outcome is reported by CI independently. This phase does not implement semantic vectors, reranking or robust production HA.

## 1. Architecture

```text
SQLite document_versions + index_outbox (source of truth)
   ↓ short BEGIN IMMEDIATE claim, expiring lease epoch
OutboxIndexer (bounded 4MB NDJSON Bulk requests)
   ↓ version=revision, version_type=external_gte
OpenSearch (strict index mapping, 1 primary/dev no replica)
   ↓ is_indexable=true AND visibility=public
GET /v1/search -> typed BM25 DSL -> results/snippet
```

Index initialization is explicit and operator-controlled; the production caller must not create indices silently on every startup. Projected documents use stable `_id=document_id`. `source_id`, `revision`, `version_id`, provenance URL and language are materialized from SQLite; `content` comes from immutable SHA256 raw-text blobs.

## 2. Tombstone correctness and retry semantics

A `delete` event is materialized as an **empty, logically non-indexable document** (`is_indexable=false`, `content=''`, `title=''`), with higher external revision. A physical deletion would discard eventual version history after tombstone GC, allowing an old delayed worker to resurrect content. Logical tombstones avoid this within a managed index lifecycle. Every read **must** include `is_indexable=true` and `visibility=public`; privileged/tenant-specific documents are not ingested in this phase.

The Bulk API is processed item-by-item, not just by top-level `errors`. Status 2xx ACKs; 408/429/5xx are retryable; 400 mapping errors are dead-lettered; 409 remains retryable unless authoritative SQLite confirms the item was superseded by a newer revision. Workers use leases with `(owner, epoch)` to fence stale acknowledgements; expired leases can be reclaimed. Manual `index-retry-dead` supports recovery once underlying errors are fixed. **Idempotency:** replaying the same external version is allowed via `external_gte`; old lower versions are rejected.

The receipt table records the latest acknowledged revision per logical write alias. `index-audit` checks SQLite projection lag and dead/pending outbox counts only. It **does not** independently verify the remote index; an OpenSearch corruption/disaster requires rebuilding and a restore drill. Payload cap is 4MB per item/batch; oversized content becomes a visible dead event instead of silently truncating.

**Privacy:** logical tombstones hide content from search and remove it from the current document source, but old Lucene segments, snapshots and raw blob backups require explicit retention/erasure policy. Do not claim this implementation alone fulfills legal deletion obligations.

## 3. Lexical field/index design

`infra/opensearch/documents-v1.json` uses strict mapping with built-in `standard`, `english` and `cjk` analyzers. Title gets the strongest boost; headings/body have lower weights. `title.raw` handles exact navigation. The current processor does not emit structured headings; that field is reserved for later extraction. CJK tokenizer support must be validated against actual target OpenSearch build before deployment (CI live smoke uses `_analyze`). English stemming may be unsuitable for technical identifiers; field/rare-term evaluation comes next.

P0 search API accepts query (1–200 characters), optional `language` and `source_id`, limit 1–20, offset 0–200; it constructs typed DSL rather than accepting user raw queries. All filters are applied by OpenSearch *before* ranking. JSON snippets are plain text, not executable HTML. API sends HTTP 503 if backend cannot be reached; not yet serving degraded cached results.

## 4. Index maintenance and aliases

```bash
# After database initialization and starting OpenSearch (local dev only):
python -m search_engine.cli index-init --index search-documents-v001
python -m search_engine.cli index --max-steps 200
python -m search_engine.cli index-audit
python -m search_engine.cli search '中文检索' --language zh
```

Rebuild requires **operator-verified quiescence**: stop crawler, processor and indexer workers; drain/ACK all outbox operations first; invoke `index-rebuild --new-index search-documents-v002 --offline-confirm`. It creates a new physical index, reprojects all latest source versions (including tombstones) in size-bounded Bulk batches (4MB), refreshes and checks document count, verifies the SQLite document-version snapshot, and uses OpenSearch atomic `_aliases` to move read/write aliases together. It retains the old index for operator rollback. The final alias step holds a short SQLite writer lock, but **does not fence external indexers**; never use this as an uncoordinated online reindex.

## 5. Tests / objective gates

- SQLite v3→v4 migration and full v2→v4 upgrade; schema constraints.
- Single-host queue claim, fenced outbox ACK, retry/backoff, permanent DLQ and manual replay.
- Partial Bulk per-item errors, backend timeout, replay after crash, stale revision suppression, logical tombstone and anti-resurrection.
- Bounded typed query/field boosts, language/source/public-only filters, fake end-to-end search.
- Offline rebuild count/snapshot/alias switch, and refusal if outbox pending.
- Real OpenSearch container smoke (CI): strict mapping, `_analyze` zh/en, Bulk write + BM25, delete/noindex filter, offline alias rebuild.

**Not included:** multi-host PostgreSQL, live concurrent alias switch, real-site search-quality judgments, nDCG evaluation corpus, OpenSearch HA/TLS production deployment, vector ANN and LTR.

## 6. References

- [Stanford IR textbook](https://nlp.stanford.edu/IR-book/) chapters 1–8 and 11: postings, scoring, term vocabulary, evaluation and probabilistic retrieval.
- [OpenSearch 3.9 Bulk API](https://docs.opensearch.org/3.9/api-reference/document-apis/bulk/): external/external_gte version control and per-item Bulk failures.
- [OpenSearch 3.9 Alias API](https://docs.opensearch.org/3.9/api-reference/alias/aliases-api/): atomic alias changes.
- [SQLite WAL](https://www.sqlite.org/wal.html): one-host concurrency limits.
