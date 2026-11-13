#!/usr/bin/env python3
"""Compare BM25 vs RRF Hybrid on frozen ranked lists and graded judgments.

Input must contain ACTUAL human judgments for any quality claim; included fixture
is purely synthetic to validate math and control flow.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from statistics import fmean
from search_engine.hybrid import rrf_fuse
from search_engine.relevance import evaluate_corpus


def compare(rows: list[dict], *, k: int = 10, recall_k: int = 100,
            rrf_k: int = 60) -> dict:
    if not rows:
        raise ValueError("At least one judged query required")
    lexical, hybrid = [], []
    reranked = []
    for row in rows:
        if not all(key in row for key in ("query_id", "language", "judgments", "lexical", "vector")):
            raise ValueError("Missing required ranked-list fields")
        common = {key: row[key] for key in ("query_id", "language", "judgments")}
        lex = list(row["lexical"])
        vec = list(row["vector"])
        fused = [doc_id for doc_id,_ in rrf_fuse([lex,vec],k=rrf_k)]
        lexical.append({**common, "results":lex})
        hybrid.append({**common, "results":fused})
        if "reranked" in row:
            proposed = list(row["reranked"])
            if len(proposed) != len(set(proposed)) or not set(proposed).issubset(fused):
                raise ValueError("Reranked IDs must be a unique subset of retrieved candidates")
            # Reranking may only reorder supplied candidates; retain remainder in fused order.
            finished = proposed + [doc for doc in fused if doc not in set(proposed)]
            reranked.append({**common,"results":finished})

    result = {
        "protocol": "offline_graded_human_judgments_required",
        "lexical": evaluate_corpus(lexical,k=k,recall_k=recall_k),
        "hybrid": evaluate_corpus(hybrid,k=k,recall_k=recall_k),
        "rrf_k": rrf_k,
        "notes": "Candidate-list recall is limited by upstream lexical/vector top-k.",
    }
    baseline = result["lexical"]["groups"]["overall"]
    treatment = result["hybrid"]["groups"]["overall"]
    result["delta_hybrid_minus_lexical"] = {
        metric: (treatment[metric]-baseline[metric]
                 if treatment.get(metric) is not None and baseline.get(metric) is not None
                 else None)
        for metric in (f"ndcg@{k}",f"mrr@{k}",f"recall@{recall_k}")
    }
    if reranked:
        # Evaluate only queries for which a reranked run was provided; never compare
        # differently sized query sets as if they were a fair complete ablation.
        result["reranked_subset"] = evaluate_corpus(reranked,k=k,recall_k=recall_k)
        result["reranked_subset_queries"] = len(reranked)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare frozen retrieval runs")
    parser.add_argument("--input",required=True,type=Path)
    parser.add_argument("--output",type=Path)
    parser.add_argument("--k",type=int,default=10)
    parser.add_argument("--recall-k",type=int,default=100)
    parser.add_argument("--rrf-k",type=int,default=60)
    args = parser.parse_args(argv)
    rows=json.loads(args.input.read_text(encoding="utf-8"))
    result=compare(rows,k=args.k,recall_k=args.recall_k,rrf_k=args.rrf_k)
    encoded=json.dumps(result,ensure_ascii=False,indent=2,sort_keys=True)+"\n"
    if args.output:
        args.output.write_text(encoded,encoding="utf-8")
    else:
        print(encoded,end="")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
