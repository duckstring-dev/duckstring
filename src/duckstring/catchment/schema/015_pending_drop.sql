-- Per-Pond pending table deletes (see plans/deletes.md). A delete-table request records the table name
-- here (survives a Catchment restart) and forces a run; at the next BeginRun dispatch the Driver hands the
-- names to the Duck (which drops each table's whole registry collection + published data via
-- executor.wipe_table) and clears the rows. Objects are removed directly (no registry), so they are not
-- tracked here. Keyed on `pond` (the selected version) like the other operational-config tables.
CREATE TABLE pond_pending_drop (
    pond_id    INTEGER NOT NULL REFERENCES pond(id),
    table_name TEXT    NOT NULL,
    PRIMARY KEY (pond_id, table_name)
);
