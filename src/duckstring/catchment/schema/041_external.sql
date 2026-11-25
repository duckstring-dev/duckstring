-- A Pond's declared external sources (plans/records-and-media.md): `[pond] external`, a JSON list of names drawn on
-- the canvas as nodes feeding it (an `app:name@major` entry is that App line's Records and Media). Visual only.
ALTER TABLE pond_version ADD COLUMN external TEXT;
