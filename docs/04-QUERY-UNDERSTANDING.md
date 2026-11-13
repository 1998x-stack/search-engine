# 04 — Query Understanding (Chinese / English / Mixed)

**Textbook trace:** Stanford IIR chapters 2–3 (vocabulary, tolerant retrieval), 6–7 (vector scoring), 9 (relevance feedback/expansion), 12 (language models).

## 1. Deterministic query pipeline

Input -> length/control-character validation -> Unicode normalize -> language/script detection -> tokenization with index-compatible analyzer -> intent/entity features -> spell suggestion -> bounded rewrite/expansion -> AST -> retrieval planner. Preserve raw query for debugging only under privacy policy. Logs should be sampled/redacted and retention-limited.

A compact QueryPlan: raw, normalized, detected_languages[], intent, must_filters[], should_clauses[], exact_phrases[], lexical_fields[], vector_enabled, requested_k, analyzer_version, rewrite_trace. Return a bounded explain trace to operators, not full internal sensitive signals to untrusted users.

## 2. Chinese treatment

Challenges: no whitespace boundaries, segmentation ambiguity (研究生命 / 研究生 命), simplified/traditional equivalence, named entities, transliterated technical terms, mixed digits/units, recent words. Use validated lexicon-aware segmenter or ICU/CJK analyzer; use character n-gram recall as fallback. Preserve numeric strings, punctuation within identifiers, quotes and title phrases. Expansion of synonym pairs (人工智能/AI, 爬虫/抓取) can help but must be intent-gated and evaluated; translations are not exact synonyms.

Query cases: “苹果芯片” can refer to vendor chips not fruit; “深度学习 2025 论文” demands time filter and document type; “C++ 内存泄漏” must retain C++ token. Navigation intent may emphasize exact title/domain; research intent favors broader recall.

## 3. English treatment

Tokenize Unicode, casefold; maintain exact forms for CamelCase, version strings, URLs, package IDs and terms like C#, .NET. Optional light stemming for general prose: connect / connecting, but avoid over-stemming code and biomedical terms. Use edit distance + corpus frequency + context for **suggestions**, not silent rewrite of valid rare terms. Typo tolerance is field- and intent-dependent.

Queries: “python requests timeout” -> exact package term + body match; “how to fix timeout in requests” -> semantic expansion allowed if lexical intent preserved; “CVE-2026-...” -> exact match first.

## 4. Code-switched and multilingual

Example “OpenSearch 中文分词 performance”: clause for literal OpenSearch keyword, Chinese analysis on 中文分词, English stemming/normalization on performance. Deduplicate shared tokens; combine multi_field clauses with appropriate boosts. Language detector may be uncertain on short queries: run both analyzers rather than hard-select. Evaluate simplified/traditional mapping and English stemming independently.

## 5. Query grammar and safety

P0 parse: words, quoted phrase, minus-negation, site/domain filter, lang:zh|en, from:/before: ISO date, type:. Validate each operator; set max clauses (e.g. 32), query length, pagination and cost budgets. Avoid unbounded regex, wildcard-leading scans, arbitrary DSL pass-through and script_score from users. Escape OpenSearch operators in user literal text; build typed DSL in backend.

Planner chooses: navigational -> exact/title/domain boost; informational -> multi_match BM25; fresh -> time filter/boost; error code -> keyword; broad natural-language -> optional vector. Search filters must apply before candidate retrieval; do not let rewriter weaken access constraints.

## 6. Rewriting safeguards

- Correct only high-confidence typos with a visible “searching for” indicator and ability to force original.
- Query expansion limited to small inspected synonym sets; log applied rules.
- LLM expansion is P2 opt-in and **never** allowed to fabricate filters, remove quoted terms, cross tenants or drop access restrictions.
- Zero results: lower optional constraints, propose spell suggestions, then explain rather than silently return unrelated documents.

## 7. Evaluation corpus

Build at least 100 labelled queries each zh/en/mixed for P1 and 30 each P0 initial; include named entities, rare tokens, typo variants, punctuation, CJK compounds, simplified/traditional variants, negative clauses, title navigation, bilingual jargon, snippets, injection attempts. Report tokenization accuracy and nDCG/Recall *per language group*. Record analyzer and synonym versions with judgments.

## References

- https://nlp.stanford.edu/IR-book/
- https://docs.opensearch.org/latest/analyzers/language-analyzers/icu/
