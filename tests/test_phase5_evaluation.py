"""Synthetic tests for comparison pipeline; not search quality measurements."""
from __future__ import annotations
import json
from pathlib import Path
import unittest

from search_engine.hybrid import rrf_fuse
from scripts.evaluate_hybrid import compare

ROOT = Path(__file__).resolve().parents[1]


class HybridEvaluationTests(unittest.TestCase):
    def test_fixed_synthetic_fixture(self):
        rows=json.loads((ROOT/"tests/fixtures/hybrid-synthetic.json").read_text())
        result=compare(rows)
        self.assertEqual(result["lexical"]["groups"]["overall"]["queries"],3)
        self.assertEqual(result["hybrid"]["groups"]["overall"]["queries"],3)
        self.assertEqual(result["reranked_subset_queries"],1)
        self.assertEqual(result["protocol"],"offline_graded_human_judgments_required")

    def test_reject_fabricated_reranked_id(self):
        with self.assertRaises(ValueError):
            compare([{"query_id":"x","language":"en","judgments":{"a":3},
                      "lexical":["a"],"vector":[],"reranked":["not-retrieved"]}])

    def test_rrf_is_deterministic(self):
        self.assertEqual(rrf_fuse([["a","b"],["b","c"]]),
                         rrf_fuse([["a","b"],["b","c"]]))


if __name__=="__main__":
    unittest.main()
