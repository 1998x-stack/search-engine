-- v4 -> v5, derived vector index progress; does NOT change lexical outbox.
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS vector_receipts (
  model_key TEXT NOT NULL,
  document_id TEXT NOT NULL REFERENCES documents(document_id),
  revision INTEGER NOT NULL CHECK(revision>=1),
  indexed_at_ms INTEGER NOT NULL,
  PRIMARY KEY(model_key,document_id)
);
CREATE INDEX IF NOT EXISTS idx_vector_receipts_document ON vector_receipts(document_id,revision);
PRAGMA user_version=5;
COMMIT;
