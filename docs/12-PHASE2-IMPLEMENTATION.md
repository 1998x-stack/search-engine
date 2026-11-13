# Phase 2 — Document Processing, Deduplication and Transactional Outbox

**Scope:** single-host SQLite pipeline after Phase 1 crawl. This phase **does not** deliver OpenSearch index writes, query APIs, ANN or LLM summaries. The next phase consumes the durable outbox.

## Executed pipeline

```text
FETCH_URL HTTP 200 -> raw content SHA-256 + immutable blob
   -> same SQLite transaction stores fetch and EXTRACT_CONTENT job
   -> processor leases job (at least once)
   -> validates blob SHA-256 + media type
   -> bounded HTML/plain extraction, normalization and language detection
   -> content digest + SimHash 8 x 8-bit LSH candidate lookup
   -> pairwise Jaccard verification (no transitive clustering)
   -> BEGIN IMMEDIATE: document version + duplicate edges + index_outbox + task ACK
HTTP 404/410 (known document) -> DELETE_DOCUMENT -> tombstone + delete outbox
HTTP 304 -> fetch metadata only, no content job/version/outbox
```

## Extraction and indexing policy

- Use Python stdlib HTMLParser for deterministic P0 extraction; discard script/style/nav/footer/aside/form/header. Preserve title, plain body text, metadata description, `lang`, and same-host canonical **hint**. Do not trust a cross-host canonical hint as a redirect or identity proof.
- Character support: UTF-8/UTF-16 BOM, GBK/GB18030, Big5, Windows-1252 with allowlisted HTML charset detection. Unknown encoding falls back to UTF-8 replacement. Preserve display title; normalize body for matching via NFKC + casefold + whitespace folding.
- `noindex` is recognized from HTML meta robots and HTTP X-Robots-Tag. Noindex content remains provenance but emits an index **delete** operation; empty body is unindexable. Parser never executes JS, follows embedded external links or renders unsafe HTML.
- P0 supports HTML/XHTML/text only; PDF/XML structured parsing, OCR, advanced boilerplate classifiers and multilingual segmenters are later phases.

## Identity, exact duplicates and near duplicates

- `document_id = SHA256(source_id + NUL + normalized_url)`. Identity is stable across versions, unique per source/URL. Canonical hints do **not** merge document identity.
- `normalized_sha256 = SHA256(normalized substantive body)` is a content digest independent of title. A change in title, body, indexability, language, description or canonical hint creates a new document version. Exact content digests across different URLs create a **pairwise** duplicate relation, not a destructive merge.
- Near-duplicate candidate generation: word/CJK-character 3-shingles -> stable 64-bit SimHash -> 8 disjoint 8-bit index bands. Hamming distance <=7 guarantees one band collision; this only proposes candidates. Verify min 20 tokens, min length ratio 0.75, Hamming <=7 and **exact Jaccard >=0.85**. Max 200 candidates per document and compare only current same-source versions. Recall is bounded by candidate budget; tune using annotated data.
- Do not use transitive clustering. A~B and B~C do not imply A~C. Similarity relations are marked `distinct` when documents update/delete/noindex; evidence rows are retained for audit.

## Versioning, deletion, outbox

- Fetch rows are immutable; processor validates blob bytes before parsing. Raw and extracted normalized text are content-addressed and fsynced before reference is committed.
- Writer transaction stores new version, updates `documents.latest_revision`/delete state, updates URL document pointer, inserts exactly one outbox upsert/delete event, records pairwise duplicate relations, then ACKs the processing lease. Fault injection proves that a failing outbox insert rolls back all preceding DB changes and does not ACK the task.
- 404/410 on a **known** document enqueues DELETE_DOCUMENT and creates an explicit tombstone version plus delete outbox event. 304 creates no new version. Noindex emits a delete outbox without claiming the source URL is gone.
- Older fetch rows cannot overwrite newer versions even if processing tasks arrive out of order; fetch `rowid` is monotonic within the local SQLite database. Stale leases are fenced by the Phase 1 epoch/owner token. Do not treat this as multi-host ordering; P2 distributed migration needs a cross-node ingest sequence.
- OpenSearch is **not** updated in this phase. `index_outbox.state='pending'` is an expected result. Indexer must implement per-item ACK, idempotent `_id=document_id`, monotonic external revisions and reconciliation before declaring search freshness.

## SQLite v3 and migration

Fresh installs use `schema/sqlite.sql` v3. Existing v2 databases run `migrations/0003_document_processing.sql` atomically via `Database.initialize()`, under a Linux single-host advisory file lock to prevent crawler/processor startup races. Added: `fetches.x_robots_tag`, `document_versions.canonical_hint`, `document_versions.description`, `document_signatures`, and `signature_bands` with indexes. A copy of the version-2 DDL in `migrations/fixture_phase1_schema.sql` is retained **only as a migration test fixture**.

> Backup real SQLite database + WAL consistently before upgrade (SQLite backup API); never copy only the main `.db` while writes are active. The v3 migration has a tested v2 fixture path but no live production restore drill yet.

## Ops

```bash
python -m search_engine.cli --database ./data/search.db init
# Authorize source and create run as in README
python -m search_engine.cli --database ./data/search.db worker --max-steps 100
python -m search_engine.cli --database ./data/search.db process --max-steps 100
python -m search_engine.cli --database ./data/search.db status
PYTHONPATH=src python -W error::ResourceWarning -m unittest discover -s tests -v
```

Compose `crawler` and `processor` services share the same single-host persistent SQLite volume. Do not move that volume to NFS. Neither should run OpenSearch directly.

## Validation and remaining risks

- Offline deterministic test gates cover extraction, encoding, canonical scope, header/meta noindex, exact/near relations, invalidation, noindex/delete/resurrection, bad content hash, stale fetch, cancellation, outbox atomic rollback, v2->v3 upgrade, and local HTTP-to-outbox E2E including 304.
- **NOT RUN:** production HTTP corpus, PDF, OpenSearch indexing/search, 100K+ scale, latency/load tests, backup/restore on production Linux, full parser security fuzzing, cross-host deployment.
- Limits: stdlib HTML parsing is not Readability-quality; UTF-8 fallback may lose information; SimHash 8-bit bands trade false candidate volume for bounded Hamming guarantee; current near-candidate cap can reduce recall. Content-addressed blobs can become orphaned on rolled-back transactions and require retention-aware GC.

## Sources

Stanford *Introduction to Information Retrieval* chapters 2–5, 8, 16–20: https://nlp.stanford.edu/IR-book/ . SQLite WAL: https://www.sqlite.org/wal.html . OpenSearch mapping / bulk API: https://docs.opensearch.org/latest/ .
