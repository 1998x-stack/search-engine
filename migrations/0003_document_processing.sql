-- Upgrade Phase 1 schema version 2 to version 3; run atomically.
BEGIN IMMEDIATE;
ALTER TABLE fetches ADD COLUMN x_robots_tag TEXT;
ALTER TABLE document_versions ADD COLUMN canonical_hint TEXT;
ALTER TABLE document_versions ADD COLUMN description TEXT;
CREATE TABLE document_signatures (
 document_id TEXT NOT NULL, revision INTEGER NOT NULL,
 algorithm_version TEXT NOT NULL, simhash_hex TEXT NOT NULL,
 token_count INTEGER NOT NULL CHECK(token_count>=0),
 PRIMARY KEY(document_id,revision,algorithm_version),
 FOREIGN KEY(document_id,revision) REFERENCES document_versions(document_id,revision)
);
CREATE TABLE signature_bands (
 document_id TEXT NOT NULL, revision INTEGER NOT NULL,
 algorithm_version TEXT NOT NULL, band_number INTEGER NOT NULL CHECK(band_number BETWEEN 0 AND 7),
 band_value INTEGER NOT NULL CHECK(band_value BETWEEN 0 AND 255),
 PRIMARY KEY(document_id,revision,algorithm_version,band_number),
 FOREIGN KEY(document_id,revision,algorithm_version)
   REFERENCES document_signatures(document_id,revision,algorithm_version)
);
CREATE INDEX idx_signature_candidate ON signature_bands(algorithm_version,band_number,band_value);
CREATE INDEX idx_exact_current ON document_versions(normalized_sha256,is_indexable,document_id,revision);
PRAGMA user_version=3;
COMMIT;
