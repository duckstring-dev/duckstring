-- Duckstring Catchment schema (freshness / push-token runtime).
--
-- Identity is a three-table split:
--   pond_name    — the abstract named entity (any version). Kind is a design property.
--   pond_version — a specific deployed snapshot + its immutable topology & run history.
--   pond         — the SELECTED version, one per (pond_name, major); upserted on deploy. This is
--                  "the Pond" referenced by all live demand/freshness/graph tables.
--
-- Conventions: singular table names; {parent}_to_{child} association tables; FK columns {table}_id;
-- inter-pond and intra-pond concerns kept in separate tables. Freshness is UTC ISO-8601 text.

-- ─── Identity ──────────────────────────────────────────────────────────────────

CREATE TABLE pond_name (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL CHECK (kind IN ('inlet', 'pond', 'outlet')),
    git_branch TEXT
);

CREATE TABLE pond_version (
    id                INTEGER PRIMARY KEY,
    pond_name_id      INTEGER NOT NULL REFERENCES pond_name(id),
    version           TEXT    NOT NULL,           -- full semver e.g. "1.2.3"
    major             INTEGER NOT NULL,
    source_path       TEXT    NOT NULL,           -- relative to catchment root, ponds/{name}/{version}/
    immediate_retries INTEGER NOT NULL DEFAULT 0,
    source_retries    INTEGER NOT NULL DEFAULT 0,
    deployed_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (pond_name_id, version)
);

-- The selected Pond: one per (pond_name, major), pointing at the chosen version.
CREATE TABLE pond (
    id              INTEGER PRIMARY KEY,
    pond_name_id    INTEGER NOT NULL REFERENCES pond_name(id),
    major           INTEGER NOT NULL,
    pond_version_id INTEGER NOT NULL REFERENCES pond_version(id),
    UNIQUE (pond_name_id, major)
);

-- ─── Topology (immutable, per pond_version) ─────────────────────────────────────

CREATE TABLE ripple (
    id              INTEGER PRIMARY KEY,
    pond_version_id INTEGER NOT NULL REFERENCES pond_version(id),
    name            TEXT    NOT NULL,
    UNIQUE (pond_version_id, name)
);

-- Intra-pond parent/child edges. All implicitly required.
CREATE TABLE ripple_to_ripple (
    sink_id   INTEGER NOT NULL REFERENCES ripple(id),
    source_id INTEGER NOT NULL REFERENCES ripple(id),
    PRIMARY KEY (sink_id, source_id)
);

-- ─── Live demand / freshness / graph (keyed on the selected Pond) ───────────────

-- Inter-pond sources from pond.toml [sources]; rewritten on redeploy. Sink is the selected Pond;
-- source is a version-line (pond_name + major) so a sink may be deployed before its source exists.
CREATE TABLE pond_to_pond (
    pond_id             INTEGER NOT NULL REFERENCES pond(id),       -- sink (selected Pond)
    source_pond_name_id INTEGER NOT NULL REFERENCES pond_name(id),  -- source version-line
    source_major        INTEGER NOT NULL DEFAULT 1,
    required            INTEGER NOT NULL DEFAULT 1,
    min_version         TEXT,
    PRIMARY KEY (pond_id, source_pond_name_id, source_major)
);

-- Pond-level freshness state (mirrors the engine's PondState; durable across restarts).
CREATE TABLE pond_state (
    pond_id           INTEGER PRIMARY KEY REFERENCES pond(id),
    start_f           TEXT,
    end_f             TEXT,
    d_ms              INTEGER NOT NULL DEFAULT 0,
    has_pull          INTEGER NOT NULL DEFAULT 0,
    has_received_pull INTEGER NOT NULL DEFAULT 0
);

-- The push target set per Pond.
CREATE TABLE pond_target (
    pond_id  INTEGER NOT NULL REFERENCES pond(id),
    target_f TEXT    NOT NULL,
    PRIMARY KEY (pond_id, target_f)
);

-- Batch-availability windows on Inlets (RFC-5545-flavoured recurrence). The first window opens at
-- start_anchor for duration_seconds, recurring every freq_interval x freq_unit; valid_days restricts
-- weekdays (CSV of MON..SUN, NULL=all); until_time ends the recurrence. Timestamps are UTC ISO-8601.
CREATE TABLE pond_window (
    pond_id          INTEGER NOT NULL REFERENCES pond(id),
    name             TEXT    NOT NULL,
    start_anchor     TEXT    NOT NULL,
    duration_seconds INTEGER NOT NULL,
    freq_unit        TEXT    NOT NULL CHECK (freq_unit IN ('SECOND', 'MINUTE', 'HOUR', 'DAY', 'WEEK')),
    freq_interval    INTEGER NOT NULL DEFAULT 1,
    valid_days       TEXT,
    until_time       TEXT,
    PRIMARY KEY (pond_id, name)
);

-- Standing triggers. Tap/Pulse are one-shot (no row). Tide carries a staleness bound.
CREATE TABLE pond_trigger (
    pond_id INTEGER PRIMARY KEY REFERENCES pond(id),
    kind    TEXT    NOT NULL CHECK (kind IN ('wave', 'tide')),
    bound_ms INTEGER,
    status  TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused'))
);

-- ─── Run history (append-only, keyed on pond_version) ───────────────────────────

CREATE TABLE pond_run (
    pond_version_id INTEGER NOT NULL REFERENCES pond_version(id),
    f               TEXT    NOT NULL,             -- freshness identifying the Pond Run
    started_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT    NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'success', 'failed')),
    retry           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (pond_version_id, f)
);

CREATE TABLE ripple_run (
    pond_version_id INTEGER NOT NULL REFERENCES pond_version(id),
    f               TEXT    NOT NULL,
    ripple_id       INTEGER NOT NULL REFERENCES ripple(id),
    started_at      TEXT,
    finished_at     TEXT,
    status          TEXT    NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'success', 'failed')),
    retry           INTEGER NOT NULL DEFAULT 0,
    log_path        TEXT,
    PRIMARY KEY (pond_version_id, f, ripple_id),
    FOREIGN KEY (pond_version_id, f) REFERENCES pond_run(pond_version_id, f)
);

CREATE INDEX idx_pond_version_name ON pond_version(pond_name_id);
CREATE INDEX idx_ripple_version    ON ripple(pond_version_id);
CREATE INDEX idx_pond_to_pond_src  ON pond_to_pond(source_pond_name_id);
CREATE INDEX idx_pond_run_version  ON pond_run(pond_version_id);
