-- Alerts: failure & freshness notifications delivered to external channels. See plans/alerts.md.
-- Operational config (like windows/spouts), catchment-side observability — NOT an engine node, NOT in
-- pond.toml (destinations/credentials are environment-specific). Alerting observes state transitions the
-- engine already computes; it adds no orchestration state.

-- A notification binding: where to deliver, what to deliver, and (optionally) a freshness SLA. Scoped by
-- pond *name* (a string, NULL = catchment-wide) so it survives version/major changes; a channel may name a
-- not-yet-deployed pond (like a spout can precede its source).
CREATE TABLE alert_channel (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    destination  TEXT NOT NULL,               -- URI; scheme picks the notifier; ${env:}/${secret:} refs intact
    scope_name   TEXT,                        -- a pond name, or NULL for catchment-wide
    events       TEXT NOT NULL DEFAULT 'all', -- CSV of kinds (failure/contract/spout/recovery/freshness), or 'all'
    stale_ms     INTEGER,                     -- freshness-SLA bound in ms; NULL = no freshness monitoring
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- The delivery outbox + dedup ledger + audit log in one. A row is enqueued 'pending' by Driver._emit_alert
-- and drained by the alert worker; the UNIQUE(channel_id, dedup_key) is the fire-once-per-episode fence.
CREATE TABLE alert_delivery (
    id          INTEGER PRIMARY KEY,
    channel_id  INTEGER NOT NULL REFERENCES alert_channel(id) ON DELETE CASCADE,
    dedup_key   TEXT NOT NULL,                -- "{kind}:{pond}:{f}" — one alert per episode per channel
    event_kind  TEXT NOT NULL,
    pond_name   TEXT,                         -- NULL for a catchment-wide event
    severity    TEXT NOT NULL,                -- info | warning | error
    payload     TEXT NOT NULL,               -- JSON: the rendered AlertEvent the notifier sends
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    sent_at     TEXT,
    UNIQUE (channel_id, dedup_key)
);

CREATE INDEX idx_alert_delivery_pending ON alert_delivery(status) WHERE status = 'pending';
