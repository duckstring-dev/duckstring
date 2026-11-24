-- App scaling (plans/app-scaling.md): the operator's overrides of an App line's scaling settings, kept across
-- redeploys (NULL = the App's own), and a count pinned by `app scale --to` until hold_until.
CREATE TABLE app_scale (
    pond_id INTEGER PRIMARY KEY REFERENCES pond(id) ON DELETE CASCADE,
    min_instances INTEGER,
    max_instances INTEGER,
    concurrency INTEGER,
    connections INTEGER,
    requests REAL,
    cpu REAL,
    memory REAL,
    load REAL,
    hold_count INTEGER,
    hold_until TEXT
);

-- Why each instance started: the first request, the warm floor, or the scaler's reason.
ALTER TABLE app_instance ADD COLUMN start_reason TEXT;
