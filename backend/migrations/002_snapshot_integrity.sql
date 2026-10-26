-- Preserve the event content used by an overview, even after source refresh.
ALTER TABLE news_snapshot_events ADD COLUMN event_json TEXT;
ALTER TABLE news_snapshot_events ADD COLUMN extraction_status TEXT;
UPDATE news_snapshot_events SET
    event_json = (SELECT event_json FROM news_events WHERE id = event_id),
    extraction_status = (SELECT extraction_status FROM news_events WHERE id = event_id);

CREATE TABLE runtime_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO runtime_state (key, value)
SELECT 'news_snapshot', id FROM news_snapshots
ORDER BY created_at DESC, id DESC LIMIT 1;

-- A lease prevents concurrent duplicate paid calls across API processes.
CREATE TABLE operation_leases (
    key TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at REAL NOT NULL
);
