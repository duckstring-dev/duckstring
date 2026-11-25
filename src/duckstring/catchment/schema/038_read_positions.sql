-- Read positions (plans/source-read-positions.md): each Pond Run reads each Source up to what that Source had
-- published when the Run was dispatched (its read position), and the next Run goes on from there. The column held
-- the same values as version pins; the Runs recorded before this read their Trickle Sources only up to their own
-- freshness, so their values are cleared: a Run with no recorded positions is followed by one reading from the
-- previous Run's freshness, as before. A Run in flight across the upgrade is pinned again when it is re-dispatched.
ALTER TABLE pond_run RENAME COLUMN source_pins TO read_positions;
UPDATE pond_run SET read_positions = NULL;
