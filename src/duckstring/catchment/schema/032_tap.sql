-- Taps (plans/taps.md): named access points on a Pond (one major line) for an app to query. Each has its own
-- key (stored as a sha256 hash, as catchment_key), and a query arriving with it may send the Pond a Refresh:
-- when the newest demand on the Pond, or with none pending its freshness, is at least fresh_within_ms old
-- (0 = every query). The key reads the Pond line's exposed tables, as any read-level key does.
-- query_count, refresh_count and last_query_at are observability only, written in batches.
CREATE TABLE pond_tap (
    pond_id         INTEGER NOT NULL REFERENCES pond(id),
    name            TEXT    NOT NULL,
    key_hash        TEXT    NOT NULL UNIQUE,
    fresh_within_ms INTEGER NOT NULL DEFAULT 0 CHECK (fresh_within_ms >= 0),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_query_at   TEXT,
    query_count     INTEGER NOT NULL DEFAULT 0,
    refresh_count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (pond_id, name)
);
