#!/usr/bin/env python3
"""Lightweight deterministic static contracts; not a live search test."""
import json
from pathlib import Path
import sqlite3
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def main():
    for i,slug in enumerate(['ARCHITECTURE','CRAWLING','PROCESSING-DEDUP','INDEXING',
       'QUERY-UNDERSTANDING','RETRIEVAL-RANKING','BACKGROUND-JOBS','DATA-MODEL',
       'DEPLOYMENT','ROADMAP-EVALUATION','STANFORD-IR-MAPPING','PHASE1-IMPLEMENTATION','PHASE2-IMPLEMENTATION']):
        assert (ROOT / f'docs/{i:02d}-{slug}.md').is_file()
    with sqlite3.connect(':memory:') as con:
        con.executescript((ROOT/'schema/sqlite.sql').read_text())
        tables={r[0] for r in con.execute('SELECT name FROM sqlite_master WHERE type="table"')}
        assert {'sources','hosts','host_leases','host_robots','urls','jobs','fetches','index_outbox','document_signatures','signature_bands'} <= tables
        assert con.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert con.execute('PRAGMA user_version').fetchone()[0] == 3
    mapping=json.loads((ROOT/'infra/opensearch/documents-v1.json').read_text())
    assert mapping['mappings']['dynamic']=='strict'
    assert mapping['mappings']['properties']['title']['fields']['zh']['analyzer']=='cjk'
    assert tomllib.loads((ROOT/'pyproject.toml').read_text())['project']['name']=='startup-search-engine'
    print('PHASE 2 STATIC CONTRACTS PASS; index/search E2E is NOT RUN')

if __name__=='__main__':
    main()
