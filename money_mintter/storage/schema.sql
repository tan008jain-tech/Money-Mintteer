CREATE TABLE IF NOT EXISTS runtime_snapshots (
    id BIGSERIAL PRIMARY KEY,
    captured_at TIMESTAMPTZ NOT NULL,
    schema_version INTEGER NOT NULL,
    snapshot JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runtime_snapshots_captured_at
    ON runtime_snapshots (captured_at DESC);

CREATE TABLE IF NOT EXISTS runtime_events (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runtime_events_created_at
    ON runtime_events (created_at DESC);
