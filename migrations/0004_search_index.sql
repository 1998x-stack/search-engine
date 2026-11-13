-- v3 -> v4, receipts of acknowledged OpenSearch projection operations.
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS index_receipts (
  index_alias TEXT NOT NULL,
  document_id TEXT NOT NULL REFERENCES documents(document_id),
  revision INTEGER NOT NULL CHECK(revision >= 1),
  op_id TEXT NOT NULL REFERENCES index_outbox(op_id),
  indexed_at_ms INTEGER NOT NULL,
  PRIMARY KEY(index_alias,document_id)
);
CREATE INDEX IF NOT EXISTS idx_outbox_lease ON index_outbox(state,leased_until_ms);
PRAGMA user_version=4;
COMMIT;
