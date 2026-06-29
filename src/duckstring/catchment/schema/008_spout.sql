-- Spouts: a Pond's egress bindings — "pour this table out to there" (see plans/egress.md). Operational
-- config (CLI/API, persisted, survives redeploys), like windows — NOT declared in pond.toml, because
-- destinations and credentials are environment-specific. Keyed on `pond` (the selected version) so a
-- Spout follows the live major line; `pond_id` is stable across redeploys of the same major, so a Spout
-- survives them. Credentials live in the destination URI as ${env:NAME} references, resolved only at
-- egress time (egress/credentials.py) — never stored resolved.
--
-- This migration is the Spout *construct* (config + CRUD). Execution state (fault/retry, watermarks) is
-- added by the egress-worker migration when that lands.
CREATE TABLE pond_spout (
    pond_id     INTEGER NOT NULL REFERENCES pond(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,                  -- operator handle, unique per Pond (the rm/resync target)
    table_name  TEXT,                           -- NULL = all of the Pond's published tables
    destination TEXT NOT NULL,                  -- URI; scheme selects the egress driver; ${env:NAME} creds
    mode        TEXT NOT NULL DEFAULT 'auto',   -- auto | full | append
    schedule    TEXT NOT NULL DEFAULT 'on-run', -- 'on-run' (v1); a staleness bound is the reserved extension
    PRIMARY KEY (pond_id, name)
);
