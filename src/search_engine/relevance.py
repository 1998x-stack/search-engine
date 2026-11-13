"""Reproducible, graded IR evaluation for offline JSON query judgments/runs.

Unjudged search hits are treated as nonrelevant; report pool coverage separately.
Recall@k is omitted from macro recall for queries with zero known relevant docs.
See Stanford IIR ch. 8 and standard DCG/nDCG definitions.
"""
from __future__ import annotations

from collections import defaultdict
from math import log2
from statistics import fmean
from typing import Iterable


def _safe_judgments(qrels: dict[str, int]) -> dict[str, int]:
    if not isinstance(qrels, dict):
        raise ValueError("qrels must be document_id -> graded relevance")
    out = {}
    for doc_id, grade in qrels.items():
        if not isinstance(doc_id, str) or not doc_id or isinstance(grade, bool) or not isinstance(grade, int) or not 0 <= grade <= 3:
            raise ValueError("relevance grades must be integers from 0 to 3")
        out[doc_id] = grade
    return out


def evaluate_query(qrels: dict[str, int], results: Iterable[str], *, k: int = 10,
                   recall_k: int = 100) -> dict:
    if not 1 <= k <= 1000 or not 1 <= recall_k <= 1000:
        raise ValueError("k and recall_k must be 1–1000")
    truth = _safe_judgments(qrels)
    ranked = list(results)
    if not all(isinstance(doc_id, str) and doc_id for doc_id in ranked):
        raise ValueError("results must contain valid document IDs")
    if len(ranked) != len(set(ranked)):
        raise ValueError("ranked results must not contain duplicate document IDs")
    relevant = {doc_id for doc_id, grade in truth.items() if grade > 0}
    top_k = ranked[:k]
    hits = sum(doc_id in relevant for doc_id in top_k)
    dcg = sum((2 ** truth.get(doc_id, 0) - 1) / log2(pos + 2)
              for pos, doc_id in enumerate(top_k))
    ideal = sorted(truth.values(), reverse=True)[:k]
    idcg = sum((2 ** grade - 1) / log2(pos + 2)
               for pos, grade in enumerate(ideal))
    reciprocal = next((1 / (i + 1) for i, doc_id in enumerate(top_k)
                       if doc_id in relevant), 0.0)
    return {
        f"precision@{k}": hits / k,
        f"ndcg@{k}": (dcg / idcg) if idcg else 0.0,
        f"mrr@{k}": reciprocal,
        f"recall@{recall_k}": (len(relevant.intersection(ranked[:recall_k])) / len(relevant)
                               if relevant else None),
        "judged_fraction": (sum(doc_id in truth for doc_id in ranked[:k]) /
                            len(top_k) if top_k else 0.0),
        "known_relevant": len(relevant),
    }


def evaluate_corpus(queries: list[dict], *, k: int = 10,
                    recall_k: int = 100) -> dict:
    """Each row: {query_id, language: zh/en/mixed, judgments:{id:0-3}, results:[id,...]}."""
    if not isinstance(queries, list) or not queries:
        raise ValueError("At least one evaluation query is required")
    groups: dict[str, list[dict]] = defaultdict(list)
    seen = set()
    for row in queries:
        qid = row.get("query_id")
        language = row.get("language")
        if not isinstance(qid, str) or not qid or qid in seen:
            raise ValueError("Each query_id must be a unique nonempty string")
        if language not in ("zh", "en", "mixed"):
            raise ValueError("language must be zh, en, or mixed")
        seen.add(qid)
        measure = evaluate_query(row["judgments"], row["results"], k=k,
                                 recall_k=recall_k)
        groups[language].append(measure)
        groups["overall"].append(measure)

    def summarize(rows: list[dict]) -> dict:
        keys = (f"precision@{k}", f"ndcg@{k}", f"mrr@{k}",
                f"recall@{recall_k}", "judged_fraction")
        summary = {"queries": len(rows)}
        for key in keys:
            values = [row[key] for row in rows if row[key] is not None]
            summary[key] = fmean(values) if values else None
        summary["zero_known_relevant"] = sum(row["known_relevant"] == 0 for row in rows)
        return summary

    return {"protocol": "graded_0_to_3_unjudged_as_zero",
            "k": k, "recall_k": recall_k,
            "groups": {name: summarize(rows) for name, rows in sorted(groups.items())}}
