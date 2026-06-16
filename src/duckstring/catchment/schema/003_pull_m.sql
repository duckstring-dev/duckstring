-- Persist the active pull's minted epoch (pull_m) alongside has_pull, so a Pond holding a pending
-- pull across a restart keeps its demand epoch. Matters for a pull-driven Pond Draw waiting on its
-- upstream: pull_m is the solicitation epoch it forwards, and (unlike remote_f) nothing re-derives it.
-- See plans/minted-freshness.md. NULL = no pull (NEVER).
ALTER TABLE pond_state ADD COLUMN pull_m TEXT;
