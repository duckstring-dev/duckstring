-- A Pond Run a later Run did the work of (plans/showcase-fixes.md, attention FINDINGS 15). Ripples keep demand
-- as a set, so a Ripple that hadn't yet started Run f2 when f3 arrived runs once, at f3, and f2 never
-- completes. Its rows were closed as `failed` ("abandoned"), which run history, `/metrics`' failure counter and a
-- failed Pond's error all read as a failure. Now they are `superseded`, with the freshness of the Run that
-- covered them. Existing rows keep what they say.
--
-- A table rebuild (the status CHECK can't be altered). `ripple_run` references `pond_run`, so foreign keys are
-- off while the parent is replaced; the reference resolves to the new table once it takes the name.
PRAGMA foreign_keys = OFF;

CREATE TABLE pond_run_new (
    pond_version_id INTEGER NOT NULL REFERENCES pond_version(id),
    f               TEXT    NOT NULL,             -- freshness identifying the Pond Run
    started_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    status          TEXT    NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'success', 'failed', 'killed', 'superseded')),
    retry           INTEGER NOT NULL DEFAULT 0,
    error           TEXT,                          -- failure message (Duck-level error), if any
    traceback       TEXT,                          -- full traceback for the failure, if any
    changed         INTEGER NOT NULL DEFAULT 1,
    source_pins     TEXT,
    superseded_by   TEXT,                          -- the freshness of the Run that covered a superseded one
    PRIMARY KEY (pond_version_id, f)
);
INSERT INTO pond_run_new
    (pond_version_id, f, started_at, finished_at, status, retry, error, traceback, changed, source_pins)
SELECT pond_version_id, f, started_at, finished_at, status, retry, error, traceback, changed, source_pins
FROM pond_run;
DROP TABLE pond_run;
ALTER TABLE pond_run_new RENAME TO pond_run;
CREATE INDEX idx_pond_run_version ON pond_run(pond_version_id);

PRAGMA foreign_keys = ON;
