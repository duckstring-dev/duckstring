-- Mark which Ripples are Trickles (the @trickle decorator variant — history-preserving incremental
-- I/O). A topology property of the deployed artifact, so it lives on the per-pond_version ripple row
-- alongside name/edges. Surfaced in /api/status so the UI can render Trickles distinctly.
ALTER TABLE ripple ADD COLUMN is_trickle INTEGER NOT NULL DEFAULT 0;
