-- Taps on Apps (plans/apps.md): a Tap on an App runs per request to the App, which already came through the
-- Catchment's gate, so it has no key. key_hash becomes nullable (NULL: an App's Tap); UNIQUE still holds for the
-- keys there are, since SQLite treats NULLs as distinct.
CREATE TABLE pond_tap_new (
    pond_id         INTEGER NOT NULL REFERENCES pond(id),
    name            TEXT    NOT NULL,
    key_hash        TEXT    UNIQUE,
    fresh_within_ms INTEGER NOT NULL DEFAULT 0 CHECK (fresh_within_ms >= 0),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_query_at   TEXT,
    query_count     INTEGER NOT NULL DEFAULT 0,
    refresh_count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (pond_id, name)
);
INSERT INTO pond_tap_new SELECT pond_id, name, key_hash, fresh_within_ms, created_at, last_query_at, query_count,
    refresh_count FROM pond_tap;
DROP TABLE pond_tap;
ALTER TABLE pond_tap_new RENAME TO pond_tap;
