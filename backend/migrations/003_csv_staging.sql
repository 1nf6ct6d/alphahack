CREATE TABLE dataset_snapshots (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('companies', 'payments', 'links')),
    source_hash TEXT NOT NULL,
    source_filename TEXT NOT NULL,
    encoding TEXT NOT NULL,
    delimiter TEXT NOT NULL,
    header_json TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    error_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE(kind, source_hash)
);
CREATE TABLE dataset_rows (
    snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots(id) ON DELETE CASCADE,
    record_number INTEGER NOT NULL,
    source_line_end INTEGER NOT NULL,
    raw_json TEXT NOT NULL,
    normalized_json TEXT,
    issues_json TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, record_number)
);
