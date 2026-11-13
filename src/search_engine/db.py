"""SQLite control plane. Each short operation gets its own connection."""
from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
from typing import Iterator

def clock_ms() -> int:
    return time.time_ns() // 1_000_000

class Database:
    def __init__(self, path: str | Path, schema_path: str | Path | None = None):
        self.path = Path(path)
        self.schema_path = Path(schema_path) if schema_path else Path(__file__).resolve().parents[2] / 'schema/sqlite.sql'
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('PRAGMA busy_timeout=5000')
        return con

    def initialize(self) -> None:
        with self.connect() as con:
            con.executescript(self.schema_path.read_text(encoding='utf-8'))
            version = con.execute('PRAGMA user_version').fetchone()[0]
            if version != 2:
                raise RuntimeError(f'Unsupported SQLite schema version: {version}')

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        con = self.connect()
        try:
            con.execute('BEGIN IMMEDIATE')
            yield con
            con.execute('COMMIT')
        except BaseException:
            if con.in_transaction:
                con.execute('ROLLBACK')
            raise
        finally:
            con.close()
