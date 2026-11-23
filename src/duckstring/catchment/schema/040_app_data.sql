-- Records and Media (plans/records-and-media.md): an App line's data held by the Catchment. Keyed on the line's
-- name and major, not on its `pond` row, because the data outlives the line: it is kept until purged.
CREATE TABLE app_data (
    name        TEXT    NOT NULL,
    major       INTEGER NOT NULL,
    records     INTEGER NOT NULL DEFAULT 0,  -- a Records schema and role exist for it
    media       INTEGER NOT NULL DEFAULT 0,  -- a Media directory or prefix exists for it
    state       TEXT    NOT NULL DEFAULT 'live',  -- trial | live | retired
    created_at  TEXT    NOT NULL,
    PRIMARY KEY (name, major)
);
