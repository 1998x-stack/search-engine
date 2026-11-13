"""SQLite schema smoke and referential constraints; not an integration test."""
from pathlib import Path
import sqlite3
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SQLiteSchemaTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript((ROOT / "schema/sqlite.sql").read_text("utf-8"))

    def tearDown(self):
        self.db.close()

    def test_expected_tables(self):
        names = {r[0] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"jobs", "index_outbox", "documents", "urls"} <= names)

    def test_foreign_keys_enabled(self):
        self.assertEqual(
            self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1
        )

    def test_bad_json_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("""
                INSERT INTO sources
                (source_id,name,root_url,allowed_hosts_json,policy_json,created_at_ms,updated_at_ms)
                VALUES ('s','test','https://example.org','BAD','{}',0,0)
            """)

    def test_unique_idempotency_key(self):
        self.db.execute("""
            INSERT INTO jobs
            (job_id,kind,entity_id,idempotency_key,scheduled_at_ms,created_at_ms,updated_at_ms)
            VALUES('j1','FETCH_URL','u1','k1',0,0,0)
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("""
                INSERT INTO jobs
                (job_id,kind,entity_id,idempotency_key,scheduled_at_ms,created_at_ms,updated_at_ms)
                VALUES('j2','FETCH_URL','u1','k1',0,0,0)
            """)

    def test_tombstone_cannot_be_indexable(self):
        self.db.execute("""
            INSERT INTO sources(source_id,name,root_url,allowed_hosts_json,policy_json,created_at_ms,updated_at_ms)
            VALUES ('s','sample','https://example.org','["example.org"]','{}',0,0)
        """)
        self.db.execute("""
            INSERT INTO documents(document_id,source_id,canonical_url,created_at_ms,updated_at_ms)
            VALUES ('d','s','https://example.org/a',0,0)
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("""
                INSERT INTO document_versions(document_id,revision,parser_version,is_indexable,is_tombstone,created_at_ms)
                VALUES('d',1,'v1',1,1,0)
            """)


if __name__ == "__main__":
    unittest.main()
