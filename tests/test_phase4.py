"""Phase 4 query planning, filter safety and graded relevance math (offline)."""
from __future__ import annotations
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from search_engine.query import plan_query, diagnostic_tokens, suggest_typo
from search_engine.relevance import evaluate_query, evaluate_corpus
from search_engine.search import SearchService, search_query


class QueryPlanningTests(unittest.TestCase):
    def test_language_english(self):
        p = plan_query("python inverted index")
        self.assertEqual(p.detected_language, "en")
        self.assertIn("python", p.tokens)
        self.assertEqual(p.intent, "informational")

    def test_chinese_compounds_and_unknown_bigrams(self):
        p = plan_query("中文分词 搜索引擎")
        self.assertEqual(p.detected_language, "zh")
        self.assertIn("中文分词", p.tokens)
        self.assertIn("搜索引擎", p.tokens)
        self.assertEqual(diagnostic_tokens("陌生词"), ("陌生", "生词", "词"))

    def test_mixed_language_and_identifiers(self):
        p = plan_query("OpenSearch 中文分词 C++ performance")
        self.assertEqual(p.detected_language, "mixed")
        self.assertIn("opensearch", p.tokens)
        self.assertIn("中文分词", p.tokens)
        self.assertIn("c++", p.tokens)

    def test_fullwidth_nfkc_does_not_destroy_identifiers(self):
        p = plan_query("Ｐｙｔｈｏｎ Ｃ＋＋ 搜索")
        self.assertEqual(p.normalized, "Python C++ 搜索")
        self.assertIn("c++", p.tokens)

    def test_quotes_and_exclusions(self):
        p = plan_query('python "inverted index" -"spam page" -advertising')
        self.assertEqual(p.phrases, ("inverted index",))
        self.assertEqual(p.excluded, ("spam page", "advertising"))
        payload = search_query('python "inverted index" -"spam page" -advertising')
        clauses = payload["query"]["bool"]
        self.assertEqual(len(clauses["must"]), 1)
        self.assertEqual(len(clauses["must_not"]), 2)
        self.assertEqual(clauses["must"][0]["multi_match"]["slop"], 0)

    def test_conflicting_filters_are_rejected(self):
        for kwargs in (
            {"q": "foo lang:zh", "language": "en"},
            {"q": "foo source:a", "source_id": "b"},
        ):
            with self.assertRaises(ValueError):
                plan_query(**kwargs)

    def test_site_exact_filter_and_language(self):
        q = search_query("搜索引擎 lang:zh site:Example.org source:docs")
        filters = q["query"]["bool"]["filter"]
        self.assertIn({"term": {"language": "zh"}}, filters)
        self.assertIn({"term": {"host": "example.org"}}, filters)
        self.assertIn({"term": {"source_id": "docs"}}, filters)
        self.assertIn({"term": {"visibility": "public"}}, filters)
        self.assertIn({"term": {"is_indexable": True}}, filters)

    def test_date_exclusive_before_and_inclusive_after(self):
        q = search_query("IR after:2026-01-01 before:2026-03-01")
        filters = q["query"]["bool"]["filter"]
        ranges = [f["range"]["updated_at_ms"] for f in filters if "range" in f]
        self.assertEqual(len(ranges), 1)
        self.assertEqual(ranges[0]["gte"], int(datetime(2026,1,1,tzinfo=timezone.utc).timestamp()*1000))
        self.assertEqual(ranges[0]["lt"], int(datetime(2026,3,1,tzinfo=timezone.utc).timestamp()*1000))

    def test_stable_plan_with_positive_terms(self):
        p = plan_query('ranking from:2026-01-01')
        self.assertEqual(p.retrieval_text, "ranking")
        self.assertEqual(p.intent, "informational")

    def test_unknown_filters_are_literal_terms(self):
        p = plan_query("CVE-2026-12345 error")
        self.assertIn("CVE-2026-12345", p.positive)
        self.assertEqual(p.intent, "troubleshooting")

    def test_suggestions_are_not_applied_to_search(self):
        p = plan_query("pyhton tutorial")
        self.assertEqual(p.suggestion, {"original": "pyhton", "candidate": "python"})
        query = search_query("pyhton tutorial")
        self.assertEqual(query["query"]["bool"]["should"][0]["multi_match"]["query"], "pyhton tutorial")
        self.assertIsNone(plan_query("pyhton", rewrite_mode="off").suggestion)

    def test_synonym_expansion_opt_in_and_low_weight(self):
        lexical = search_query("搜索引擎")
        expanded = search_query("搜索引擎", rewrite_mode="expand")
        self.assertEqual(len(lexical["query"]["bool"]["should"]), 3)
        should = expanded["query"]["bool"]["should"]
        self.assertGreater(len(should), 3)
        self.assertEqual(should[-1]["multi_match"]["boost"], 0.2)
        self.assertEqual(should[0], lexical["query"]["bool"]["should"][0])
        self.assertIn({"term": {"visibility": "public"}}, expanded["query"]["bool"]["filter"])

    def test_expansion_not_applied_to_navigation_phrase(self):
        p = plan_query('"搜索引擎"', rewrite_mode="expand")
        self.assertEqual(p.expansions, ())

    def test_unbounded_lexical_operators_rejected(self):
        bad = [
            '"unfinished', 'hello "word"tail', 'hello -', 'site:example.org',
            "foo lang:any", "foo site:.com", "foo site:example.org/path",
            "foo site:localhost:8080", "foo before:2026-99-99",
            "foo after:2026-12-31 before:2026-01-01",
            "foo lang:zh lang:en", "foo source:bad/name",
            "a"*201, "hello\nworld", "foo\u200bbar",
            "foo " + " ".join(f"term{i}" for i in range(40)),
        ]
        for text in bad:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    plan_query(text)

    def test_hard_filters_are_not_in_should(self):
        for mode in ("off", "suggest", "expand"):
            q = search_query("倒排索引 lang:zh source:unit site:example.org",rewrite_mode=mode)
            b = q["query"]["bool"]
            self.assertEqual(len(b["filter"]), 5)
            self.assertEqual(b["minimum_should_match"], 1)
            self.assertTrue(all("term" in item for item in b["filter"]))
            self.assertIn("must_not", search_query("python -spam")["query"]["bool"])

    def test_bounded_limit_and_pagination(self):
        for bad in (0, 21, True, 1.5):
            with self.assertRaises(ValueError):
                search_query("test", limit=bad)
        for bad in (-1, 201, True):
            with self.assertRaises(ValueError):
                search_query("test", offset=bad)

    def test_empty_and_negative_only_query_rejected(self):
        for q in ("", "    ", "-bad", "site:example.org -spam"):
            with self.assertRaises(ValueError):
                plan_query(q)

    def test_intents(self):
        self.assertEqual(plan_query("how to build search").intent, "tutorial")
        self.assertEqual(plan_query("Elasticsearch vs OpenSearch").intent, "comparison")
        self.assertEqual(plan_query("python exception traceback").intent, "troubleshooting")
        self.assertEqual(plan_query('"exact phrase"').intent, "navigation")

    def test_rewrite_guards_no_wildcard_escalation(self):
        for invalid in ("expand-all", "AUTO", ""):
            with self.assertRaises(ValueError):
                plan_query("search", rewrite_mode=invalid)

    def test_valid_und_language_filter_is_not_dropped(self):
        q = search_query("test",language="und")
        self.assertIn({"term": {"language": "und"}}, q["query"]["bool"]["filter"])

    def test_query_service_preserves_public_filters_and_response_analysis(self):
        class Recorder:
            def __init__(self):
                self.body = None
            def request(self,method,path,body):
                self.body = body
                return {"hits": {"total": {"value": 1}, "hits": [{
                    "_id": "doc1", "_source": {
                        "document_id": "doc1", "title": "Guide",
                        "canonical_url": "https://example.org/a",
                        "description": "Snippet", "language": "en",
                    }, "_score": 1.25,
                }]}}
        client = Recorder()
        reply = SearchService(client).search("pyhton tutorial site:example.org", rewrite_mode="suggest")
        self.assertEqual(reply["total"], 1)
        self.assertEqual(reply["results"][0]["document_id"], "doc1")
        self.assertEqual(reply["analysis"]["suggestion"]["candidate"], "python")
        self.assertIn({"term": {"is_indexable": True}}, client.body["query"]["bool"]["filter"])


class RelevanceTests(unittest.TestCase):
    def test_perfect_ndcg_and_recall(self):
        score = evaluate_query({"a": 3,"b": 2,"c": 1}, ["a","b","c"],k=3,recall_k=3)
        self.assertAlmostEqual(score["ndcg@3"], 1.0)
        self.assertAlmostEqual(score["recall@3"], 1.0)
        self.assertAlmostEqual(score["mrr@3"], 1.0)

    def test_reversed_grades_lower_ndcg(self):
        score = evaluate_query({"a": 3, "b": 1}, ["b","a"],k=2,recall_k=2)
        self.assertLess(score["ndcg@2"], 1)
        self.assertEqual(score["recall@2"], 1)

    def test_unjudged_results_count_as_zero(self):
        score = evaluate_query({"a": 3, "x": 0}, ["unknown","a"],k=2,recall_k=2)
        self.assertEqual(score["precision@2"], 0.5)
        self.assertEqual(score["judged_fraction"], 0.5)
        self.assertAlmostEqual(score["mrr@2"], 0.5)

    def test_zero_relevance_recall_is_null(self):
        score = evaluate_query({"a": 0},["a"],k=1,recall_k=2)
        self.assertIsNone(score["recall@2"])
        self.assertEqual(score["ndcg@1"], 0.0)

    def test_duplicate_results_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_query({"a":2},["a","a"])
        with self.assertRaises(ValueError):
            evaluate_query({"a":True},["a"])

    def test_language_breakdown_macro(self):
        rows = [
            {"query_id": "zh1","language":"zh","judgments":{"a":2},"results":["a"]},
            {"query_id": "en1","language":"en","judgments":{"a":2},"results":["noise"]},
            {"query_id": "mix1","language":"mixed","judgments":{"m":3},"results":["m"]},
        ]
        summary = evaluate_corpus(rows,k=1,recall_k=1)
        self.assertEqual(summary["groups"]["overall"]["queries"],3)
        self.assertEqual(summary["groups"]["zh"]["ndcg@1"],1.0)
        self.assertEqual(summary["groups"]["en"]["ndcg@1"],0.0)
        self.assertAlmostEqual(summary["groups"]["overall"]["recall@1"],2/3)

    def test_reject_duplicate_query_ids_and_missing_languages(self):
        row = {"query_id":"q1","language":"zh","judgments":{"a":2},"results":["a"]}
        with self.assertRaises(ValueError):
            evaluate_corpus([row,row])
        with self.assertRaises(ValueError):
            evaluate_corpus([{**row,"language":"any"}])

    def test_offline_fixture_reproducible(self):
        import json
        path = Path(__file__).resolve().parents[1] / "tests/fixtures/relevance-demo.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        summary = evaluate_corpus(rows,k=10,recall_k=100)
        self.assertEqual(summary["groups"]["overall"]["queries"],3)
        self.assertEqual(set(summary["groups"]), {"zh","en","mixed","overall"})


if __name__ == "__main__":
    unittest.main()
