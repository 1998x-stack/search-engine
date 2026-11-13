# 05 — Retrieval, Ranking and Serving

**Textbook trace:** Stanford IIR chapters 6–9 and 11; chapter 21 for link-quality signals. BM25 is a probabilistic retrieval baseline; semantic retrieval is a modern extension beyond the textbook's original production stack.

## 1. Query plan and candidate generation

~~~text
Validated QueryPlan -> hard filters (tenant/ACL/source/language/date)
 -> lexical BM25 top 100–300             -> normalizer
 -> optional vector ANN top 100–300       -> normalizer
 -> optional title/exact retrieval top 50 -> rank fusion (RRF)
 -> group by canonical document -> feature enrichment
 -> rerank top 50 (P1) -> diversify/collapse duplicates
 -> snippet + safe highlight -> final top K + stable cursor
~~~

Numbers above are **initial budgets**, not benchmarked tuning. P0 must work with BM25 alone and give deterministic tie-breaks. Search latency should degrade gracefully if vector encoder/reranker is unavailable: lexical-only results + annotated degraded status, not an empty response.

## 2. Lexical baseline

BM25(q,d) = sum over query terms IDF(t) * [tf(t,d)*(k1+1)]/[tf(t,d)+k1*(1-b+b*|d|/avgdl)]. Tune k1/b on judged queries, not a single magic number. Fields: title boost, headings boost, body, tags; exact phrase/identifier matching for navigational intent. Store explain output for sampled queries to debug matched fields, but restrict exposure of sensitive metadata.

Do not assume term frequencies, vector cosine scores and authority scores are directly comparable. Use lexical field query construction and explicit score normalization when experimenting; RRF is a robust initial multi-list option.

## 3. Vector retrieval

P1 chooses a licensed multilingual embedding model and freezes model name, revision, dimension, preprocessing and normalization. Benchmark on zh, en and code-switched judgments first. ANN index tracks model_version and chunk_version; no mixing model versions. Batch offline embedding, backpressure and circuit breaker for inference. For chunk retrieval, map each chunk to parent_document_id and perform document-level aggregation: avoid five chunks from one document crowding the whole top 10.

Filter restricted tenants and ACLs **during ANN**, not only after. Test filtered ANN recall compared to brute force on small golden corpora.

## 4. RRF and later re-ranking

RRF(d) = sum over result lists 1/(k + rank_list(d)); k=60 is only a conventional baseline to tune. Missing doc from a list contributes 0. Merge by document_id and retain provenance (lexical_rank, vector_rank, exact_rank). Cross-Encoder P1 reranks only limited candidates under a timeout/cost budget; use fallback to RRF on overload.

P2 Learning-to-Rank may use title/exact/BM25/vector/freshness/document quality/link signals. Training data requires relevance judgments and defensible propensity correction for click-position bias. No CTR-only quality optimization and no secret personalization without user permission.

## 5. Ranking policy

- Hard constraints: scope, ACL, language/date filters, noindex/delete; never represent these as soft ranking features.
- Soft features: textual/semantic relevance, freshness conditioned on intent, verified source quality, dedup quality and optional diversity.
- Prevent topical spam: boilerplate weight down, domain/URL pattern caps, duplicate clustering and manual relevance audits.
- Date freshness is intent-dependent; “latest release” differs from “textbook definition”.
- Canonical grouping follows near-duplicate policy; expose source alternatives rather than destroying citations.

## 6. Result contract and pagination

Response: request_id, query_plan_summary, total_relation (exact or lower-bound), results[{document_id, title, url, snippet, score, updated_at, matched_fields}], next_cursor and degraded_components. Use search_after with deterministic secondary sort (document_id), preserve point-in-time snapshot or documented index alias generation to avoid unstable pages during refresh. Avoid deep from/size pagination. Show safe text snippets with escaped highlights; user-controlled HTML is never trusted.

## 7. Relevance evaluation and latency

Offline: labeled graded judgments (0–3) and fixed corpus snapshot; track nDCG@10, Recall@100, MRR@10, zero-result rate by zh/en/mixed and intent. Run ablations: BM25 only; BM25+RRF; +reranker; per-field boosts; near-duplicate grouping. Report confidence intervals and per-query regressions, not only mean uplift.

Performance: p50/p95/p99 end-to-end, per-stage duration, timeout rate, lexical-only fallback rate, cost/query, disk/index size. Test varying corpus size and concurrent clients. Suggested initial thresholds (to validate, not claims): p95 <300ms for P0 lexical at 10K docs on target Linux host, no unauthorized document leakage, Recall@100 non-regression after P1 improvements.

## 8. Failure and adversarial cases

Empty query, huge query, punctuation, multilingual ambiguity, typos, exact identifiers, contradictory clauses, script injection, stale index, duplicate updates, missing embedding, OpenSearch unavailable, partial shard failures, ACL leakage, index alias swap during pagination.

## References

- https://nlp.stanford.edu/IR-book/
- https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/rrf/
