-- Spout execution state (the egress worker): a per-Spout delivery watermark + fault/retry, mirroring a
-- Pond's. An egress failure never fails the Pond Run — the data is published and correct locally; egress
-- is downstream of the boundary — so a failure just parks the Spout (see plans/egress.md). The watermark
-- is the last Pond freshness this Spout has delivered; the worker egresses only when the Pond's published
-- freshness has advanced past it (idempotent snapshot writes, so re-delivery is harmless).
ALTER TABLE pond_spout ADD COLUMN watermark TEXT;                       -- last delivered pond freshness (ISO); NULL = never
ALTER TABLE pond_spout ADD COLUMN retries    INTEGER NOT NULL DEFAULT 0; -- retry budget: attempts before parking
ALTER TABLE pond_spout ADD COLUMN failures   INTEGER NOT NULL DEFAULT 0; -- failed attempts in the current episode
ALTER TABLE pond_spout ADD COLUMN is_failed  INTEGER NOT NULL DEFAULT 0; -- parked: exhausted the retry budget
ALTER TABLE pond_spout ADD COLUMN error      TEXT;                       -- last failure message
