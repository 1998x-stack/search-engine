# 03 — Index Engine, Schema and Lifecycle

**Textbook trace:** Stanford IIR chapters 1–5 (postings, dictionary, construction, compression), 6–7 (scoring), 11 (probabilistic IR). In production use OpenSearch/Lucene rather than implement custom posting segments for P0.

## 1. Index responsibilities and data model

SQLite is authoritative. OpenSearch is a materialized view rebuilt from document_versions. One canonical hit per document_id; fields include tenant/source, visibility, ACL principals (if P1), title, normalized content, url, lang, timestamps, digest, revision and deletion marker. Explicit mappings only; reject dynamic fields to prevent mapping explosion. Avoid putting arbitrary user metadata in unrestricted dynamic mappings.

Suggested aliases: search-documents-read -> search-documents-v001; search-documents-write -> same generation. Optional P1 search-chunks-read -> search-chunks-v001. Chunk record has parent_document_id, stable chunk_id, order, offsets and model_version; group chunk results by parent before final ranking. Never mix embeddings of different dimensions/models in the same vector field.

## 2. Physical inverted index

Lucene builds term dictionary, posting lists with doc IDs, term frequency and (if enabled) positions; stored fields and doc values support retrieval/filtering/sorting. Segment merges amortize updates. Delta/variable-byte and block-based compression improve IO; BM25 uses document length and corpus statistics. Avoid overindexing large content fields and excessive term vectors.

P0 document index: 1 primary, 0 replicas **local dev only**, refresh interval chosen by freshness target (e.g., 5 s); test indexing throughput vs search latency. Production requires replica/backup/SLO design, shard sizing from measured segment/doc counts and heap pressure, not a magic fixed shard count.

## 3. Multilingual analyzer strategy

English: standard Unicode tokenization + lowercase, optional English stopwords/stemmer for a dedicated *.en field; retain an exact *.raw keyword field for identifiers and exact phrases. Avoid stemming code identifiers, software version numbers and proper names.

Chinese: prefer a verified Chinese word segmentation plugin (compatible with exact OpenSearch release) or ICU analysis plugin; **test Chinese analyzer availability at startup**. Fallback when plugin is absent: standard fields + a CJK bigram subfield, not an assertion that standard analyzer gives good Chinese relevance. Index both text fields where needed; compare token examples using _analyze. For mixed zh/en query, route clauses to both fields and never silently discard Latin tokens.

Index fields/boosts: title^4, headings^2, body^1 (illustrative baseline), exact keyword phrase for navigation. Avoid aggressive synonym expansion at index time; version synonym resources and test against collision sets.

A concrete *design example* is provided at infra/opensearch/documents-v1.json. It deliberately uses built-in cjk analyzer for reproducibility and can be upgraded to ICU after plugin/version verification. Schema version must be encoded in index name.

## 4. Write path

1. Processor commits latest document version and index_outbox row in the **same SQLite transaction**.
2. Indexer leases outbox rows; batch upsert/delete with deterministic _id=document_id.
3. Track each bulk item response individually; retry 429/5xx with backoff; permanent mapping errors to DLQ.
4. Use document monotonic revision as an external version on OpenSearch writes (supported mode must be verified per deployment) so older tasks cannot resurrect deleted/replaced documents. Deletion must also advance a durable revision. If deleted index IDs lose external-version tombstone retention, guard with authoritative revision checks / rebuild reconciliation; don't assume OpenSearch alone prevents late resurrection forever.
5. Update outbox ACK only after successful remote result, then verify reconciliation asynchronously.

At-least-once processing is expected; idempotent doc IDs and revision fencing make it safe.

## 5. Index migration

Prepare new index generation and analyzers -> backfill snapshots -> replay outbox changes after recorded watermark -> compare counts, sample checksums, language tests and nDCG fixtures -> atomically switch read/write aliases -> observe -> retire old generation after rollback window. Snapshot OpenSearch independently; store raw content and SQLite backups to rebuild in full.

SLO candidates to validate: p95 search under 300ms on fixture data, indexing visibility <60s for normal updates, zero known stale tombstones after reconciliation. Not validated production claims.

## 6. Security and ACL

Filter tenant/source, visibility and allowed_principals **inside lexical and ANN search**, before candidate truncation. A post-hoc filter alone is insecure and harms recall. If no ACL implementation exists, support public-only indexes and reject restricted docs at ingestion. Escape/sanitize highlight output and never return private raw indexed fields.

## 7. Experiments

Analyze tokens on 100 zh, 100 en and 100 mixed queries; measure postings size, 1/10/100K doc corpus latency, storage per doc, bulk throughput, delete consistency and alias-switch correctness. Explicitly exercise plugin absence, disk-full, corrupted mapping and duplicate outbox delivery.

## References

- https://nlp.stanford.edu/IR-book/
- https://docs.opensearch.org/latest/analyzers/language-analyzers/icu/
- https://docs.opensearch.org/latest/vector-search/creating-vector-index/
