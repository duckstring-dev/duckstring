-- Refresh & repair (plans/refresh.md). A pending Refresh makes a Pond's next run a cold wipe-and-
-- rebuild (the Duck drops its registry and reads Sources in full, raising the published changelog
-- floor so downstream coverage-misses and reloads). `repairing` marks a Pond inside an active repair
-- plan — blocked from normal demand until its turn so it never starts partway. Both persist so a
-- pending refresh / an in-progress repair survives a Catchment restart.
ALTER TABLE pond_state ADD COLUMN refresh_pending INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pond_state ADD COLUMN repairing INTEGER NOT NULL DEFAULT 0;
