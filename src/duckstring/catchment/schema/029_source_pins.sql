-- Versioned overwrite tables (plans/versioned-overwrite.md): the Source freshnesses a Pond Run was pinned
-- to when it was dispatched, as JSON {source name: iso}. The Run's overwrite Source reads use exactly these
-- versions, a re-dispatch after a Catchment restart re-sends them, and each Source keeps the versions its
-- in-flight Sink Runs are pinned to (the begin_run job's retain_from).
ALTER TABLE pond_run ADD COLUMN source_pins TEXT;
