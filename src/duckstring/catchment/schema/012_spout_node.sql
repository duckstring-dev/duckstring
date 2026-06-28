-- Flip a Spout into a real Pond — the egress dual of a Pond Draw (see plans/egress.md). A Spout now has
-- its own identity rows (pond_name kind='outlet', a synthetic pond_version, pond.is_spout=1), is wired to
-- its source via pond_to_pond, and runs as an engine node with a standing Wake (executed by the egress
-- worker, not a Duck). Its failures/run-history flow through the normal pond_run/ripple_run path — so the
-- failure-logging, traceback and /api/runs parity comes for free instead of being cloned.
--
-- pond_spout is re-keyed to the Spout's OWN pond_id and holds just the egress config; the fault/retry
-- state moves to pond_state/pond_retry (it's a real Pond now). Windows are dropped here and re-added on the
-- node model. (Unreleased — no data migration.)
ALTER TABLE pond ADD COLUMN is_spout INTEGER NOT NULL DEFAULT 0;

DROP TABLE IF EXISTS spout_window;
DROP TABLE IF EXISTS pond_spout;
CREATE TABLE pond_spout (
    pond_id     INTEGER NOT NULL PRIMARY KEY REFERENCES pond(id) ON DELETE CASCADE,
    table_name  TEXT,                          -- NULL = all of the source's published tables
    destination TEXT NOT NULL,                 -- URI; scheme picks the egress driver; ${env:NAME} creds
    mode        TEXT NOT NULL DEFAULT 'auto',  -- auto | full | append
    armed       INTEGER NOT NULL DEFAULT 1     -- the standing-Wake armed state (Sleep=0); is_killed → pond_state
);
