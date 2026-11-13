#!/usr/bin/env python3
"""Offline relevance evaluation; use versioned corpus, judgments and run files."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from search_engine.relevance import evaluate_corpus


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate fixed search runs")
    parser.add_argument("--input", type=Path, required=True,
                        help="JSON array with query_id, language, judgments and results")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--recall-k", type=int, default=100)
    args = parser.parse_args(argv)
    rows = json.loads(args.input.read_text(encoding="utf-8"))
    summary = evaluate_corpus(rows, k=args.k, recall_k=args.recall_k)
    encoded = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
