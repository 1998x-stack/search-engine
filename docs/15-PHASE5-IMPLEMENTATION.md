# Phase 5 — Hybrid Retrieval (BM25 + HNSW + RRF + optional Cross-Encoder)

## Scope and evidence

This phase builds on merged Phase 1–4. It implements a **functional optional vector retrieval pipeline**, not a blanket statement of improved relevance. CI exercises actual OpenSearch 3.9 HNSW with deterministic vectors; the pretrained multilingual embedding and CrossEncoder packages are optional and **not downloaded or benchmarked in CI**.

## Architecture

```text
SQLite canonical document_versions v5 + raw blob store
       |                         |
       | lexical Outbox          | vector reconciliation scan + model encoder
       v                         v
OpenSearch search-documents    OpenSearch search-vectors (384 dim HNSW, Lucene)
       | BM25 top20                | filtered kNN top20
       +-------------+-------------+
                     |
       SQLite revision/visibility check
                     |
             RRF(k=60), unique doc IDs
                     |
       optional bounded CrossEncoder top20
                     |
           Search API / search CLI
```

Vector storage has its own index and read/write aliases, preserving the lexical index generation and providing a clear failure boundary. SQLite owns document identity, revisions, noindex and deletion state; OpenSearch is a rebuildable projection, not ground truth.

## Real model and versioning

- Default suggested model: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, 384 dimensions. Operator should review its license and **pin a concrete immutable model commit** before live usage; default `main` is development convenience only.
- `SentenceTransformerEmbedder` lazily imports the optional `[semantic]` package, normalizes vectors and rejects invalid 0/NaN/inf or incorrect dimensions.
- model_key = SHA256(model_id,revision,dimension,MAX_TEXT_CHARS). kNN requires exact model_key, so mismatched generations do not silently share scores.
- Optional CrossEncoder uses `cross-encoder/ms-marco-MiniLM-L6-v2` (English-first); enable only with language-appropriate evaluation. No learned/optimized query expansion added in Phase 5.
- Dataset preprocessing truncates each document to 4000 decoded characters, prefixed with title; evaluate long-document chunking in a later phase.

## Vector projection and SQLite v5

`vector_receipts(model_key, document_id, revision, indexed_at_ms)` checkpoints latest projected versions; version 4 -> 5 schema migration creates this table without changing the existing lexical outbox. Existing v2/v3/v4 fixtures migrate through the ordered schema sequence. A projector selects latest document revisions missing a receipt, embeds indexable content, and writes `_id=document_id` with `version_type=external_gte`.

On `noindex` or 404/410, a **content-free logical tombstone** with higher revision is written to vector index. Before presentation, hybrid search rechecks SQLite latest revision, `is_indexable`, deletion state, and explicit source/site/lang/date. This prevents old vector hits appearing before the eventual vector index tombstone catches up.

Bulk replies are checked per item; receipts only advance after a successful matching `_id`, and only if SQLite still reports that revision as latest. Failed responses remain eligible for retry on the next scan. This is an idempotent **single-worker reconciliation strategy**, not a durable distributed embedding queue.

**Index loss caveat:** receipts refer to the previous vector index projection. Creating an empty replacement index without re-seeding receipt state causes missing ANN results. After an explicitly managed replacement and with semantic writers stopped, `vector-reset-receipts --confirm` clears only current model's checkpoints, then `vector-sync` reprojects latest versions. There is NO automatic atomic online vector alias rebuild; operators must separately stage/swap the vector index and verify counts. Changing embedding models requires new index generation and reviewed cutover.

## Query and retrieval

- `mode=lexical` remains default and backward compatible.
- `mode=hybrid` performs BM25 top 20 and Lucene HNSW kNN top 20 (explicit ANN filter inside the kNN query). Vector candidate budget is currently limited to 20, a documented upper bound on recall.
- Same `visibility=public`, `is_indexable=true`, `source_id`, exact `host`, `language`, UTC timestamp bounds and `model_key` filters are applied **during ANN search**, with authoritative SQLite revalidation afterward.
- Required quoted phrases and excluded terms deliberately use lexical-only fallback, rather than letting semantic hits violate `must`/`must_not` semantics.
- Reciprocal Rank Fusion: `RRF(d)=sum(1/(60+rank_i(d)))` for unique IDs in each list; tie-break best position then ID. Raw BM25 and kNN scores are never directly added.
- Optional `rerank=true` scores only a bounded shortlist using an injected CrossEncoder; falls back to RRF if the reranker is unavailable.
- `mode_requested`, `mode_actual`, `degraded_components`, stage timing and `total_relation=candidate_only` expose honest behavior. Hybrid `offset>0` is rejected until stable cursor pagination exists.

## Operator workflow

```bash
# Already running Phase 3 lexical index and SQLite v5
python -m pip install -e '.[semantic]'
export SEARCH_EMBEDDING_MODEL_REVISION='<pinned-model-commit>'
python -m search_engine.cli --database ./data/search.db vector-init --index search-vectors-v001
python -m search_engine.cli --database ./data/search.db vector-sync --batch-size 8
export SEARCH_SEMANTIC_ENABLED=1
python -m search_engine.cli --database ./data/search.db search '中文 检索' --mode hybrid
# After offline vector-index replacement, stop vector writers; then optionally run
# python -m search_engine.cli vector-reset-receipts --confirm
# and vector-sync to reconstruct the full vector projection.
# Opt-in reranking requires SEARCH_RERANK_ENABLED=1 and an appropriate model
```

Development Compose provides `semantic-api` on localhost:8001 and `vector-projector` in `--profile semantic`; the semantic image downloads heavyweight dependencies and is **not part of standard Docker CI**. Share a model cache volume. Never run the dev OpenSearch cluster with disabled security on the public internet.

## Evaluation

Run `PYTHONPATH=src python scripts/evaluate_hybrid.py --input tests/fixtures/hybrid-synthetic.json` to compare frozen lexical, vector and RRF candidates with graded nDCG/MRR/Recall by language. The supplied fixture is **synthetic** and validates calculations only; it is not a product KPI.

Before declaring a quality gain, collect 100+ human-graded queries per zh/en/mixed group, build development/holdout splits and version corpus/analyzers/model revisions. Compare BM25-only, BM25+ANN+RRF and +CrossEncoder separately; measure Recall@100, nDCG@10, p95/p99 query latency, model cold-start, memory and operating cost. Actual pretrained-model inference and long-duration performance testing remain **NOT RUN**.

## Test evidence scope

- Offline unit tests: phase 1–4 regression plus phase 5 RRF, latest-revision, 410/noindex, language/site/model filtering, deterministic input vectors, fallback and reranking.
- Real OpenSearch 3.9: 384-dimensional Lucene HNSW index creation, kNN with inline filters, vector-sync, hybrid retrieval and tombstones; no synthetic fake-vectors claim of semantic accuracy.
- Container: default lightweight runtime Docker image, SQLite CLI and Compose config; heavy semantic image build is optional and not included in baseline CI.

## Primary references

- Stanford IIR (retrieval ranking, evaluation): https://nlp.stanford.edu/IR-book/
- OpenSearch 3.9 efficient kNN filtering: https://docs.opensearch.org/3.9/vector-search/filter-search-knn/efficient-knn-filtering/
- OpenSearch kNN vector mappings: https://docs.opensearch.org/3.9/mappings/supported-field-types/knn-vector/
- Hugging Face model card: https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
