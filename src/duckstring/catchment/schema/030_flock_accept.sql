-- The Flock's named acceptances (plans/flock-coverage.md, part 3): the known, bounded differences a Pond
-- accepts from a SQL engine (float_order, half_rounding), comma-separated. Declared in pond.toml
-- ([flock] accept) on the version; an operator override on pond_duck ('none' = accept nothing) wins.
ALTER TABLE pond_version ADD COLUMN flock_accept TEXT;
ALTER TABLE pond_duck ADD COLUMN flock_accept TEXT;
