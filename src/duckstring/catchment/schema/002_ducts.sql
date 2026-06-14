-- Cross-Catchment Ducts (see plans/cross-catchment-ducts.md).
--
-- A duct is a one-directional conduit from an upstream (producing) Catchment into this (consuming)
-- one. It carries the upstream's address + credentials. A consumed upstream Pond is represented
-- locally as a Pond Draw: a real node (kind='inlet', pond.is_draw=1) with a single "draw" ripple
-- that performs the data transfer. Demand flows up the duct; data flows down.

-- ─── Producer side ──────────────────────────────────────────────────────────────

-- A Pond marked "open" accepts demand from any source. Under single-level auth this is a no-op gate
-- (placeholder for the future Read/Run/Write split); its only live effect today is tap_on_get.
CREATE TABLE pond_open (
    pond_id    INTEGER PRIMARY KEY REFERENCES pond(id),
    tap_on_get INTEGER NOT NULL DEFAULT 0  -- a data read on the query route fires a Tap (served stale)
);

-- ─── Consumer side ────────────────────────────────────────────────────────────

-- A conduit into this Catchment from an upstream one. auth_json holds the upstream's request headers
-- (a secret at rest — duck.db is chmod 0600 and this column is redacted from `catchment download`).
CREATE TABLE duct (
    id              INTEGER PRIMARY KEY,
    origin_catchment TEXT    NOT NULL UNIQUE,  -- the upstream's registered name (one duct per upstream)
    remote_url      TEXT    NOT NULL,
    auth_json       TEXT,                       -- JSON object of auth headers; NULL if the upstream is open
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Which upstream Ponds this duct draws. Each row materialises a local Pond Draw (the identity rows
-- are created/destroyed alongside this row by the /api/duct routes).
CREATE TABLE duct_to_pond (
    duct_id          INTEGER NOT NULL REFERENCES duct(id),
    source_pond_name TEXT    NOT NULL,
    major            INTEGER NOT NULL DEFAULT 1,
    incremental      INTEGER NOT NULL DEFAULT 0,  -- reserved: delta vs full-snapshot fetch (Trickle, later)
    PRIMARY KEY (duct_id, source_pond_name, major)
);

-- A Pond Draw: this selected Pond is fed by a duct, not executed by a Duck. Its freshness is the
-- polled upstream freshness; its "draw" ripple performs the transfer.
ALTER TABLE pond ADD COLUMN is_draw INTEGER NOT NULL DEFAULT 0;
