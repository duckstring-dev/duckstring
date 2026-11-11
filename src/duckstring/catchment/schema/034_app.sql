-- Apps (plans/apps.md): an Outlet that serves HTTP instead of running Ripples. A version is an App when its
-- pond.toml declares [pond] type = "app"; its validated [app] section is stored here as JSON (NULL: not an App).
ALTER TABLE pond_version ADD COLUMN app TEXT;

-- An App's unit is an instance: one process, from start to stop. Kept as history, like pond_run, so it keys on
-- the version. stop_reason: idle | redeploy | killed | removed | shutdown | exited | start_failed.
CREATE TABLE app_instance (
    id              INTEGER PRIMARY KEY,
    pond_version_id INTEGER NOT NULL REFERENCES pond_version(id),
    started_at      TEXT    NOT NULL,
    ready_at        TEXT,
    stopped_at      TEXT,
    stop_reason     TEXT,
    exit_code       INTEGER,
    pid             INTEGER,
    port            INTEGER,
    log_path        TEXT    NOT NULL
);
CREATE INDEX app_instance_version ON app_instance (pond_version_id, started_at);
