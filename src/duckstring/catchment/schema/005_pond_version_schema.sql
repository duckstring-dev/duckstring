-- Phase 2 (version contract): the output schema captured on a pond_version's accepted (published) run.
-- Keyed on pond_version (immutable artifact), like topology/history. One row per output column, so both
-- the high-water-mark contract (current) and a future pinned-minor contract are computable from it, and
-- the reserved primary_key flag is the home for Trickle's declared PKs (unused in Phase 2).
CREATE TABLE pond_version_schema (
    pond_version_id INTEGER NOT NULL REFERENCES pond_version(id),
    "table"         TEXT    NOT NULL,
    "column"        TEXT    NOT NULL,
    type            TEXT    NOT NULL,
    primary_key     INTEGER NOT NULL DEFAULT 0,   -- Trickle-prep: declared PK flag (not enforced yet)
    PRIMARY KEY (pond_version_id, "table", "column")
);
