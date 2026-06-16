-- Stable Catchment identity (a UUID minted once on first start) + the upstream identity a duct
-- points at. `origin_catchment` on a duct is a local alias; cross-mesh identity needs a stable id
-- each Catchment self-reports, so a downstream can resolve duct edges and cut cycles in the recursive
-- lineage view. See plans/cross-catchment-visibility.md.

-- Small key/value store for Catchment-level metadata. Seeded with `id` (uuid4) on first start, and
-- optionally `name` (a display alias passed at init). The id never changes.
CREATE TABLE catchment_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- The stable id of the upstream Catchment this duct draws from (fetched from its /api/catchment/identity
-- when the duct is created). NULL only if the upstream was unreachable at create time.
ALTER TABLE duct ADD COLUMN upstream_id TEXT;
