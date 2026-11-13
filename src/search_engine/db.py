"""SQLite control plane. Each short operation gets its own connection."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import fcntl
import time
from typing import Iterator


def clock_ms() -> int:
    return time.time_ns() // 1_000_000


class ManagedConnection(sqlite3.Connection):
    """Unlike sqlite3's default context manager, close on context exit."""
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Database:
    def __init__(self, path: str | Path, schema_path: str | Path | None = None):
        self.path = Path(path)
        self.schema_path = Path(schema_path) if schema_path else Path(__file__).resolve().parents[2] / 'schema/sqlite.sql'
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=5, isolation_level=None, factory=ManagedConnection)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('PRAGMA busy_timeout=5000')
        return con

    def initialize(self) -> None:
        # Both the crawler and processor containers may start simultaneously.
        # Serialize schema inspection+executescript with a same-host advisory lock.
        # WAL is unsupported on NFS and this lock is not a multi-host protocol.
        lock_path = self.path.with_name(self.path.name + '.migration.lock')
        with open(lock_path, 'a+b') as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                with self.connect() as con:
                    version = con.execute('PRAGMA user_version').fetchone()[0]
                    if version == 0:
                        if con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()[0]:
                            raise RuntimeError('Unversioned nonempty SQLite database requires manual migration')
                        con.executescript(self.schema_path.read_text(encoding='utf-8'))
                    elif version == 2:
                        migration = self.schema_path.parent.parent / 'migrations' / '0003_document_processing.sql'
                        con.executescript(migration.read_text(encoding='utf-8'))
                    elif version not in (3, 4, 5):
                        raise RuntimeError(f'Unsupported SQLite schema version: {version}')
                    version = con.execute('PRAGMA user_version').fetchone()[0]
                    if version == 3:
                        migration4 = self.schema_path.parent.parent / 'migrations' / '0004_search_index.sql'
                        con.executescript(migration4.read_text(encoding='utf-8'))
                    version = con.execute('PRAGMA user_version').fetchone()[0]
                    if version == 4:
                        migration5 = self.schema_path.parent.parent / 'migrations' / '0005_vector_projection.sql'
                        con.executescript(migration5.read_text(encoding='utf-8'))
                    actual = con.execute('PRAGMA user_version').fetchone()[0]
                    if actual != 5:
                        raise RuntimeError(f'Schema initialization failed: expected 5, got {actual}')
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

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
