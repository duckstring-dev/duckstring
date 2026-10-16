-- Taps (plans/taps.md): named access points on a Pond (one major line) for an app to query. Each has its own
-- key (stored as a sha256 hash, as catchment_key), and a query arriving with it may send the Pond a Refresh:
-- when the newest demand on the Pond, or with none pending its freshness, is at least fresh_within_ms old
-- (0 = every query). wait_ms (NULL = serve at once) makes the query wait up to that long for data that new.
-- tables is a JSON array of table names limiting the Tap to some of the Pond's exposed tables (NULL = all).
-- query_count, refresh_count and last_query_at are observability only, written in batches.
CREATE TABLE pond_tap (
    pond_id         INTEGER NOT NULL REFERENCES pond(id),
    name            TEXT    NOT NULL,
    key_hash        TEXT    NOT NULL UNIQUE,
    fresh_within_ms INTEGER NOT NULL DEFAULT 0 CHECK (fresh_within_ms >= 0),
    wait_ms         INTEGER CHECK (wait_ms IS NULL OR wait_ms > 0),
    tables          TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_query_at   TEXT,
    query_count     INTEGER NOT NULL DEFAULT 0,
    refresh_count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (pond_id, name)
);
