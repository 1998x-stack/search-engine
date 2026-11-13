# 02 — Document Processing, Canonicalization and Deduplication

**Textbook trace:** Stanford IIR chapters 2–5, 19; near-duplicate/shingling discussion in Web crawling and indexes.

## 1. Immutable pipeline

~~~text
Raw fetch -> MIME/encoding verify -> safe extraction (HTML/PDF/text)
 -> boilerplate removal -> metadata and links
 -> language detection -> Unicode normalization
 -> canonical URL assessment -> exact hash
 -> near-duplicate candidate generation -> cluster policy
 -> document version + outbox -> indexed projection
~~~

Every processor has processor_version and content_hash. Re-running with the same input, version and config must return equivalent output. Preserve raw bytes for auditing (respecting license/retention) and parsed text as separate versioned artifacts. A parser error is a recoverable document failure, not an empty document.

HTML: remove scripts, style, menus and repetitive template blocks while preserving headings, title, important anchors and structured metadata; detect soft 404. PDF: use text layer with extraction limits; postpone OCR to an explicit opt-in worker. Apply Unicode NFKC cautiously (store both display_original and normalized_for_search); normalize whitespace and HTML entities; avoid losing code snippets, units or Chinese punctuation that carries meaning. Language detection by document and segment.

## 2. Identity vs content

- URL identity: source_id + canonical fetch URL; separate redirect aliases and canonical hint evidence.
- Logical document: stable document_id within a source; may have many URLs and versions.
- Raw digest: sha256(bytes), detects identical responses.
- Normalized exact digest: sha256(cleaned substantive text + extractor profile), detects identical text across wrappers.
- Near-duplicate: SimHash/MinHash signatures used only for *candidate* retrieval; semantic embedding similarity is **not** a safe duplicate decision.

Do **not** collapse unrelated pages solely because they share a title, template, footer, or the same syndicated paragraph.

## 3. Exact and near dedup algorithm

**Exact:** compute content sha256 after defined canonicalization; exact duplicates get separate provenance/url aliases but share a representative content group. Record the normalization version to avoid false equivalence after algorithm updates.

**Near duplicate candidate selection:** for text of sufficient length, create word/character shingles (for mixed Chinese/English, test character 3–5 grams vs word 3-grams). Generate 64-bit SimHash (fast neighborhood detection) and/or MinHash signature (approximate Jaccard), then LSH buckets to avoid O(N^2) comparisons. Store signature version.

**Verification:** candidates are compared with actual normalized shingles using Jaccard J(A,B)=|A intersection B|/|A union B|, plus minimum length ratio, title and source checks. Start with a **calibrated**, not universal, near-threshold (e.g., trial 0.85) and derive precision/recall from annotated examples. Short pages, boilerplate-heavy pages and mixed-language text need separate thresholds.

**Clustering:** similarity is not transitive. Never blindly union-find A~B and B~C into A~C. Require new member to meet cluster representative threshold, or keep pairwise duplicate edges and choose a canonical representative with quality/freshness/source policy. Retain all document version provenance for takedown/reindex.

## 4. Canonical representative policy

Choose within *verified* duplicate groups: owner/authorized original if known, stable URL, explicit valid canonical, substantive completeness, freshness, language, link provenance. Never treat rel=canonical as a security or identity proof; log its evidence and avoid cross-domain collapse without policy.

Store duplicate_relations (doc_a, doc_b, similarity, algorithm_version, decision, reviewed_at) and representative_document_id; keep URL aliases independently. In result serving collapse near-duplicate hits as a **presentation policy** while allowing an expanded view. Group membership should be reversible.

## 5. Versioning and deletion

Same normalized digest as current version -> update fetch_seen and freshness, no new active version. Changed substantive content -> increment monotonic document revision, persist extracted text blob, store document_version row, atomically enqueue INDEX_UPSERT. Gone/noindex/authorized removal -> tombstone version with INDEX_DELETE. A stale worker cannot overwrite newer revision: index operations carry monotonic revision and are conflict rejected/reconciled.

Retention: raw copies may have shorter TTL than extracted metadata. Delete/erasure requests propagate to raw store, versions, backups retention policy, OpenSearch index and caches; maintain audit evidence without keeping removed private content.

## 6. Tests

Golden fixtures for HTML boilerplate, UTF-8/GBK (where allowed), zh-en mixed text, PDF text extraction, redirect/canonical loops, tracking URL params, hashes stable under irrelevant changes, meaningful page edit creates version, copied menu not a duplicate, same article syndicated across URLs, A~B~C nontransitivity, idempotent retries, deletion and resurrection ordering.

## References

- https://nlp.stanford.edu/IR-book/
