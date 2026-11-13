-- Phase 1 authoritative single-host control-plane schema, SQLite >=3.35.
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS sources (
 source_id TEXT PRIMARY KEY, name TEXT NOT NULL, root_url TEXT NOT NULL,
 allowed_hosts_json TEXT NOT NULL CHECK(json_valid(allowed_hosts_json)),
 policy_json TEXT NOT NULL CHECK(json_valid(policy_json)),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS hosts (
 host_id TEXT PRIMARY KEY, hostname TEXT NOT NULL,
 robots_txt_digest TEXT, robots_checked_at_ms INTEGER,
 robots_status TEXT NOT NULL DEFAULT 'unknown'
   CHECK(robots_status IN ('unknown','allowed','disallowed','unreachable','challenge')),
 next_allowed_at_ms INTEGER NOT NULL DEFAULT 0,
 inflight INTEGER NOT NULL DEFAULT 0 CHECK(inflight>=0),
 max_inflight INTEGER NOT NULL DEFAULT 1 CHECK(max_inflight BETWEEN 1 AND 100),
 min_interval_ms INTEGER NOT NULL DEFAULT 1000 CHECK(min_interval_ms>=0),
 circuit_open_until_ms INTEGER NOT NULL DEFAULT 0,
 updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS crawl_runs (
 run_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
 state TEXT NOT NULL DEFAULT 'queued'
  CHECK(state IN ('queued','running','pausing','cancelled','completed','failed')),
 config_json TEXT NOT NULL CHECK(json_valid(config_json)),
 created_at_ms INTEGER NOT NULL,started_at_ms INTEGER,finished_at_ms INTEGER,
 updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
 document_id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(source_id),
 canonical_url TEXT NOT NULL,latest_revision INTEGER NOT NULL DEFAULT 0 CHECK(latest_revision>=0),
 is_deleted INTEGER NOT NULL DEFAULT 0 CHECK(is_deleted IN (0,1)),
 created_at_ms INTEGER NOT NULL,updated_at_ms INTEGER NOT NULL,
 UNIQUE(source_id,canonical_url)
);
CREATE TABLE IF NOT EXISTS urls (
 url_id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(source_id),
 host_id TEXT NOT NULL REFERENCES hosts(host_id),
 normalized_url TEXT NOT NULL,original_url TEXT NOT NULL,
 parent_url_id TEXT REFERENCES urls(url_id),document_id TEXT REFERENCES documents(document_id),
 depth INTEGER NOT NULL DEFAULT 0 CHECK(depth>=0),
 status TEXT NOT NULL DEFAULT 'discovered'
  CHECK(status IN ('discovered','ready','blocked','fetched','failed','deleted')),
 first_seen_at_ms INTEGER NOT NULL,last_seen_at_ms INTEGER,next_fetch_at_ms INTEGER,
 UNIQUE(source_id,normalized_url)
);
CREATE TABLE IF NOT EXISTS fetches (
 fetch_id TEXT PRIMARY KEY,url_id TEXT NOT NULL REFERENCES urls(url_id),
 run_id TEXT REFERENCES crawl_runs(run_id),fetched_at_ms INTEGER NOT NULL,
 finished_at_ms INTEGER,http_status INTEGER CHECK(http_status BETWEEN 100 AND 599),
 final_url TEXT,content_type TEXT,raw_sha256 TEXT,raw_blob_ref TEXT,
 bytes_received INTEGER CHECK(bytes_received>=0),etag TEXT,last_modified TEXT,
 x_robots_tag TEXT, error_class TEXT,error_message TEXT
);
CREATE TABLE IF NOT EXISTS document_versions (
 document_id TEXT NOT NULL REFERENCES documents(document_id),
 revision INTEGER NOT NULL CHECK(revision>=1),
 fetch_id TEXT REFERENCES fetches(fetch_id),normalized_sha256 TEXT,text_blob_ref TEXT,
 title TEXT,language TEXT,parser_version TEXT NOT NULL,
 canonical_hint TEXT, description TEXT,
 is_indexable INTEGER NOT NULL CHECK(is_indexable IN (0,1)),
 is_tombstone INTEGER NOT NULL DEFAULT 0 CHECK(is_tombstone IN (0,1)),
 created_at_ms INTEGER NOT NULL,PRIMARY KEY(document_id,revision),
 CHECK(is_tombstone=0 OR is_indexable=0)
);
CREATE TABLE IF NOT EXISTS duplicate_relations (
 left_document_id TEXT NOT NULL REFERENCES documents(document_id),
 right_document_id TEXT NOT NULL REFERENCES documents(document_id),
 method TEXT NOT NULL,algorithm_version TEXT NOT NULL,
 score REAL NOT NULL CHECK(score BETWEEN 0 AND 1),
 decision TEXT NOT NULL CHECK(decision IN ('candidate','duplicate','distinct','review')),
 examined_at_ms INTEGER NOT NULL,
 PRIMARY KEY(left_document_id,right_document_id,method,algorithm_version),
 CHECK(left_document_id<right_document_id)
);
CREATE TABLE IF NOT EXISTS jobs (
 job_id TEXT PRIMARY KEY,run_id TEXT REFERENCES crawl_runs(run_id),
 kind TEXT NOT NULL,entity_id TEXT NOT NULL,
 payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
 state TEXT NOT NULL DEFAULT 'ready'
  CHECK(state IN ('ready','leased','retry','succeeded','dead','cancelled')),
 priority INTEGER NOT NULL DEFAULT 0,scheduled_at_ms INTEGER NOT NULL,
 attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt>=0),
 max_attempts INTEGER NOT NULL DEFAULT 5 CHECK(max_attempts>0),
 lease_owner TEXT,lease_epoch INTEGER NOT NULL DEFAULT 0 CHECK(lease_epoch>=0),
 leased_until_ms INTEGER,idempotency_key TEXT NOT NULL UNIQUE,
 last_error_class TEXT,correlation_id TEXT,
 created_at_ms INTEGER NOT NULL,updated_at_ms INTEGER NOT NULL,
 CHECK(state!='leased' OR (lease_owner IS NOT NULL AND leased_until_ms IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS index_outbox (
 op_id TEXT PRIMARY KEY,document_id TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>=1),
 op_type TEXT NOT NULL CHECK(op_type IN ('upsert','delete')),payload_ref TEXT,
 state TEXT NOT NULL DEFAULT 'pending'
  CHECK(state IN ('pending','leased','retry','acked','dead')),
 attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt>=0),scheduled_at_ms INTEGER NOT NULL,
 lease_owner TEXT,lease_epoch INTEGER NOT NULL DEFAULT 0 CHECK(lease_epoch>=0),
 leased_until_ms INTEGER,last_error_class TEXT,created_at_ms INTEGER NOT NULL,
 updated_at_ms INTEGER NOT NULL,
 FOREIGN KEY(document_id,revision) REFERENCES document_versions(document_id,revision),
 UNIQUE(document_id,revision,op_type),
 CHECK(state!='leased' OR (lease_owner IS NOT NULL AND leased_until_ms IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS host_robots (
 host_id TEXT PRIMARY KEY REFERENCES hosts(host_id) ON DELETE CASCADE,
 body TEXT NOT NULL,status_code INTEGER NOT NULL CHECK(status_code BETWEEN 100 AND 599),
 fetched_at_ms INTEGER NOT NULL,expires_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS host_leases (
 token TEXT PRIMARY KEY,
 host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE CASCADE,
 job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
 leased_until_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_urls_host_due ON urls(host_id,next_fetch_at_ms);
CREATE INDEX IF NOT EXISTS idx_urls_document ON urls(document_id);
CREATE INDEX IF NOT EXISTS idx_fetches_url_time ON fetches(url_id,fetched_at_ms DESC);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_id,is_deleted);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(state,scheduled_at_ms,priority DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_expired ON jobs(state,leased_until_ms);
CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(run_id,state);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON index_outbox(state,scheduled_at_ms);
CREATE INDEX IF NOT EXISTS idx_outbox_expired ON index_outbox(state,leased_until_ms);
CREATE INDEX IF NOT EXISTS idx_outbox_document ON index_outbox(document_id,revision DESC);
CREATE INDEX IF NOT EXISTS idx_host_leases_expiry ON host_leases(host_id,leased_until_ms);
CREATE TABLE IF NOT EXISTS document_signatures (
 document_id TEXT NOT NULL, revision INTEGER NOT NULL,
 algorithm_version TEXT NOT NULL, simhash_hex TEXT NOT NULL,
 token_count INTEGER NOT NULL CHECK(token_count>=0),
 PRIMARY KEY(document_id,revision,algorithm_version),
 FOREIGN KEY(document_id,revision) REFERENCES document_versions(document_id,revision)
);
CREATE TABLE IF NOT EXISTS signature_bands (
 document_id TEXT NOT NULL, revision INTEGER NOT NULL,
 algorithm_version TEXT NOT NULL, band_number INTEGER NOT NULL CHECK(band_number BETWEEN 0 AND 7),
 band_value INTEGER NOT NULL CHECK(band_value BETWEEN 0 AND 255),
 PRIMARY KEY(document_id,revision,algorithm_version,band_number),
 FOREIGN KEY(document_id,revision,algorithm_version)
   REFERENCES document_signatures(document_id,revision,algorithm_version)
);
CREATE INDEX IF NOT EXISTS idx_signature_candidate
 ON signature_bands(algorithm_version,band_number,band_value);
CREATE INDEX IF NOT EXISTS idx_exact_current
 ON document_versions(normalized_sha256,is_indexable,document_id,revision);
CREATE TABLE IF NOT EXISTS index_receipts (
 index_alias TEXT NOT NULL,
 document_id TEXT NOT NULL REFERENCES documents(document_id),
 revision INTEGER NOT NULL CHECK(revision>=1),
 op_id TEXT NOT NULL REFERENCES index_outbox(op_id),
 indexed_at_ms INTEGER NOT NULL,
 PRIMARY KEY(index_alias,document_id)
);
CREATE INDEX IF NOT EXISTS idx_outbox_lease ON index_outbox(state,leased_until_ms);
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
