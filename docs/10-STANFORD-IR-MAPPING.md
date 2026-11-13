# 10 — Stanford Introduction to Information Retrieval: Study-to-Design Mapping

Source: Christopher D. Manning, Prabhakar Raghavan, Hinrich Schütze, *Introduction to Information Retrieval* (Cambridge UP, 2008), online chapters: https://nlp.stanford.edu/IR-book/

**Method:** map textbook principles to explicit system decisions, experiments and gaps. This is a chapter guide, not a reproduction of textbook pages. The Stanford book predates modern neural ANN, so hybrid retrieval is supplemented by OpenSearch documentation.

| IIR chapter | Central principle | This repository's design | Validation |
|---|---|---|---|
| 1 Boolean retrieval | exact set operations | strict filters, boolean QueryPlan | must/must-not fixtures |
| 2 Term vocab/postings | analyzers, postings, positions | multi-field index, term and phrase tests | _analyze + phrase queries |
| 3 Dictionary/tolerant | wildcard/edit distance, k-gram | spell suggestions and token normalization | typo/identifier fixture |
| 4 Index construction | batch, merge, segment | OpenSearch backfill, immutable generations | bulk benchmark/rebuild |
| 5 Index compression | gap encoding and compression | benchmark Lucene segment footprint | disk per doc/index stats |
| 6 Scoring/term weighting | tf-idf/vector model | BM25 field boosts and baseline | per-intent judgments |
| 7 Efficient top-K | candidate pruning | bounded retrieval and reranking | p95 & recall tradeoff |
| 8 IR evaluation | test collection, precision/recall/MAP | nDCG, MRR, Recall, split dataset | reproducible reports |
| 9 Relevance feedback/expansion | query expansion risks/benefit | synonyms, suggestions, query trace | ablation & regression |
| 10 XML retrieval | structural fields | title/headings/source facets | field weighting |
| 11 Probabilistic IR | BM25 families | BM25 as lexical primary score | k1/b grid |
| 12 Language models for IR | query/document language models | comparison research, not P0 dependency | optional offline experiment |
| 13–15 Classification | document metadata classifiers | language/type/quality feature proposals | classification labels |
| 16–18 Clustering/LSI | document similarity/grouping | dedup clustering; semantic P1 | pairwise precision/recall |
| 19 Web search basics | web properties, spam | source scope, quality and policy | spam/quality fixtures |
| 20 Crawling and indexes | frontier, politeness, freshness | host scheduler, robots, recrawl | politeness/freshness tests |
| 21 Link analysis | PageRank/HITS | quality/link features *deferred* | offline link ablation |

## Key algorithm reminders

### Dictionary and postings

Build term -> posting list of (doc_id, frequency, optional positions), enabling intersections, phrase and proximity. Search production relies on Lucene rather than reimplementing compression or segment merges in Python.

### Retrieval model discipline

Separate filtering from ranking. Establish a lexical BM25 baseline and quality judgments before tuning semantic retrieval. Candidate recall sets an upper bound on reranking performance: an absent relevant document cannot be recovered by a reranker. Measure Recall@100 first, then nDCG@10.

### Evaluation discipline

Collect graded relevance judgments, define corpus snapshot, estimate uncertainty, and preserve baselines. Avoid using click positions as uncorrected ground truth. Compare queries grouped by language and intent.

### Crawling discipline

The textbook emphasizes prioritization, politeness and distributed scalability. Implement host-aware frontier and rate limits even before adding cross-host distribution. Never turn “anti-anti-crawling” into identity evasion.

### Modern extensions beyond the book

- Dense embeddings and ANN/HNSW; fixed dimensions and versioned embedding pipeline.
- Hybrid lexical+dense candidates with RRF; cross-encoder reranking.
- ACL-aware ANN, secure SSRF-proof fetch gateway, and transactional outbox.
- Continuous incremental indexing and model/analyzer version migration.

These are engineering additions; don't attribute them to the 2008 book.

## Primary references

1. https://nlp.stanford.edu/IR-book/
2. https://www.rfc-editor.org/rfc/rfc9309.html
3. https://www.sqlite.org/wal.html
4. https://docs.opensearch.org/latest/analyzers/language-analyzers/icu/
5. https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/rrf/
6. https://docs.opensearch.org/latest/vector-search/creating-vector-index/
