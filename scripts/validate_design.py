#!/usr/bin/env python3
"""Offline static contract checks; does not imply the crawler/search is implemented."""
import json
from pathlib import Path
import sqlite3
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    required = [
        "README.md",
        *[f"docs/{i:02d}-{name}.md" for i, name in [
            (0, "ARCHITECTURE"), (1, "CRAWLING"), (2, "PROCESSING-DEDUP"),
            (3, "INDEXING"), (4, "QUERY-UNDERSTANDING"),
            (5, "RETRIEVAL-RANKING"), (6, "BACKGROUND-JOBS"),
            (7, "DATA-MODEL"), (8, "DEPLOYMENT"),
            (9, "ROADMAP-EVALUATION"), (10, "STANFORD-IR-MAPPING")
        ]],
        "schema/sqlite.sql",
        "infra/opensearch/documents-v1.json",
        "Dockerfile", "compose.yaml", ".env.example",
    ]
    missing = [name for name in required if not (ROOT / name).is_file()]
    if missing:
        raise AssertionError(f"missing expected design files: {missing}")

    schema = (ROOT / "schema/sqlite.sql").read_text(encoding="utf-8")
    with sqlite3.connect(":memory:") as con:
        con.executescript(schema)
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        expected = {"sources", "hosts", "urls", "crawl_runs", "fetches",
                    "documents", "document_versions", "duplicate_relations",
                    "jobs", "index_outbox"}
        assert expected <= tables, expected - tables
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1

    mapping = json.loads((ROOT / "infra/opensearch/documents-v1.json")
                         .read_text(encoding="utf-8"))
    assert mapping["mappings"]["dynamic"] == "strict"
    assert mapping["mappings"]["properties"]["title"]["fields"]["zh"]["analyzer"] == "cjk"
    assert mapping["settings"]["index"]["number_of_replicas"] == 0

    pkg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pkg["project"]["name"] == "startup-search-engine"

    print("STATIC DESIGN CHECKS PASS (does not test Docker/OpenSearch/crawl).")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"STATIC DESIGN CHECKS FAILED: {exc}", file=sys.stderr)
        raise
