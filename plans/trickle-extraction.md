# Extracting Trickle into its own package

Status: **plan** (2026-10-09). Nothing built yet.

## Why

Trickle (incremental joins, aggregates and scans over Z-sets) is the strongest single feature Duckstring has,
and today it can only be had by adopting the whole platform. Published on its own, it is a feature anyone using
DuckDB can try with `pip install` and ten lines of Python, with no server, no Ponds and no new concepts beyond
"write your join once and it updates incrementally". Each user of the package is also a natural candidate for
Duckstring, which is where the orchestration, versioning and catalog come in.

The package was built for this. `duckstring/trickle/` imports nothing else from Duckstring (with one exception,
below), its only host seam is the `Context` protocol, and the PEP-562 shims (`duckstring.trickle_io`,
`trickle_builder`, `agg`, `acc`) already exist to repoint.

## What's already true, and what isn't

Checked against the code on 2026-10-09:

- `trickle/` is 4,800 lines across `context`, `io`, `builder`, `agg`, `acc`, `capture` and `lineage`. Its only
  third-party imports are DuckDB, plus `ibis` (lazy, in `.sql()`) and `sqlglot` (lazy, in `lineage.py`).
- **One leak:** `builder._flock_comprehensive` does `from .. import flock` (builder.py:727). The Flock has to
  become a host hook before the code can move.
- Optional host hooks already use the right pattern: `record_lineage_write` and `count_table` are read with
  `getattr(ctx, ..., None)`. The Flock hook should follow it.
- **No standalone host exists.** Every caller today is a Duckstring `Pond`, which supplies the epoch (`f`,
  `previous_f`) and reads Sources from the Parquet data plane. A user without Duckstring has no way to drive the
  builder. This is the main piece of new work (see "The standalone host").
- The tests drive Trickle through `Pond` and `ParquetDataPlane` (`tests/test_trickle.py`, 2,838 lines;
  `test_merge_view`, `test_capture`, `test_column_lineage`). They need splitting into engine tests, which move,
  and integration tests, which stay.
- `io.py` also holds the Parquet publication format of a Trickle (sidecar, per-run parts, `landed_after`,
  `read_delta(con, data_dir, ..., dp=)`). It's pure DuckDB and pathlib, so it can move with the engine, but it's
  Duckstring's interchange format rather than something a standalone user calls.

## The standalone host

A standalone user has one DuckDB database and wants tables in it kept up to date incrementally. The host needs
to supply what `Context` asks for, from that database alone:

- **An epoch per run.** `f` is `max(now(), last_epoch + 1 µs)`, so epochs are monotonic even if the clock
  steps back. `previous_f` is the last committed epoch, or `NEVER` on the first run.
- **Run state in the database.** A small table (under the reserved prefix) holds, per named pipeline, the last
  committed epoch and any pending one. Several independent pipelines can share a database by name.
- **Crash and retry.** A run that fails leaves its epoch pending. The next run reuses the pending epoch instead
  of minting a new one, so it replays at the same `f`, which the engine already makes idempotent. The epoch is
  committed only when the run finishes cleanly.
- **Source reads from the same database.** A table written by `append_table`/`merge_table` (it has a row in
  the meta table) gives its delta through `read_registry_delta`, generalised to an arbitrary `previous_f`. Any
  other table is read in full (`is_full=True`), which sends the builder down its comprehensive path. That is
  correct, just not incremental, and the docs should say so.
- **Snapshot sources become change sources with `merge_table`.** The common case is a source that arrives as a
  full snapshot (a file drop, an API export). `merge_table(name, snapshot, pk=...)` diffs it against the prior
  state and writes the changelog, after which everything downstream is incremental. This is the headline
  pattern for the README.

Sketch (names to be settled in phase 2):

```python
import duckdb
import trickle
from trickle import agg

con = duckdb.connect("shop.duckdb")

with trickle.run(con, pipeline="nightly") as t:
    # Full snapshots in, change sets out.
    t.merge_table("orders", con.sql("FROM read_parquet('drops/orders/*.parquet')"), pk="order_id")
    t.merge_table("products", con.sql("FROM read_csv('drops/products.csv')"), pk="product_id")

    # Written once; each run only recomputes the order lines whose order or product changed.
    (t.source("orders")
       .join(t.source("products"), on="product_id")
       .mutate(revenue="s0.quantity * s1.unit_price")
       .aggregate(by="s1.category", revenue=agg.sum("revenue"), orders=agg.count())
       .merge("revenue_by_category"))
```

`trickle.run` returns the host object. It satisfies `Context` and exposes the Pond-style conveniences
(`source` = `TrickleBuilder(self, ref)`, `append_table`, `merge_table`, `read_delta`, `read_table`). Duckstring's
`Pond` stays a separate host. Neither depends on the other.

## Decisions for the author

1. **Distribution and import name.** Decided (2026-10-09): distribution `duckstring-trickle`, import `trickle`,
   docs on duckstring.com. The package is deliberately presented as part of Duckstring: it shares the
   `_duckstring_` namespace and the domain, and every install names the parent project. "DuckDB" goes in the
   description, keywords and README title ("Trickle: incremental joins and aggregates for DuckDB") for search,
   instead of in the name, which also avoids leaning on the DuckDB Foundation's trademark. The PyPI `trickle` is
   a Tornado IOStream wrapper with a single 0.1 release from 2013, so an import clash is very unlikely.
   `import duckstring.trickle` is ruled out because it would collide with Duckstring's own package.
2. **Separate repo or a second package in this repo.** A separate repo (`duckstring-dev/trickle`) gets its own
   README, stars and issue tracker, which is most of the point of extracting it. A package in this repo (a uv
   workspace member) keeps one CI and lets one change touch both sides atomically. Recommendation: a separate
   repo, because discoverability is the goal, with Duckstring depending on a pinned range. Engine changes then
   need a release before Duckstring can use them, which is the trade.
3. **The `_duckstring_` column prefix.** Every Trickle table carries `_duckstring_f`/`_duckstring_d` and
   `_duckstring_*` companions (43 literal uses in `trickle/` besides `SYSTEM_PREFIX`). Renaming to `_trickle_`
   is a storage format change for every existing Duckstring registry and published table, so it needs a
   migration. Recommendation: keep `_duckstring_` for now and document it as the format's namespace. It also
   quietly advertises where the package came from.
4. **License.** Duckstring is Apache-2.0. Recommendation: the same.

## Phases

### 1. Close the seam (in this repo, no behaviour change)

- Replace the Flock import with an optional host hook: `getattr(ctx, "dispatch_comprehensive", None)`, called
  with what `flock.comprehensive` takes today and returning a relation or `None`. `Pond` implements it by
  calling `flock.comprehensive`. The Flock's mode ladder, OOM fail-up and env handling stay in Duckstring.
- Add a `format_version` to the meta table (`_duckstring_trickle`) and the `_trickle.json` sidecar, defaulting
  to 1 when absent. A later format change can then migrate instead of guessing.
- Add a test that imports `duckstring.trickle` with the rest of `duckstring` blocked (an import hook), so the
  seam can't regress.
- Exit: full suite green; `grep "from \.\." src/duckstring/trickle` is empty.

### 2. Build the standalone host (in this repo, inside `trickle/`)

- `trickle/session.py`: `run(con, pipeline=...)` as above, with the epoch table, pending-epoch replay and
  commit on clean exit.
- Generalise `read_registry_delta` so the host can read any Trickle table's window `(previous_f, f]` from the
  registry, with the same coverage rule (bootstrap or a gap below the floor gives a full read).
- Engine tests that use only the standalone host: bootstrap, an incremental run, a crash mid-run followed by
  a replay, two pipelines in one database, a plain (non-Trickle) source falling back to full reads, and the
  README example end to end with values checked against a from-scratch recompute.
- Exit: the README example runs with nothing from Duckstring imported.

### 3. Split the tests

- Inventory `test_trickle.py`, `test_merge_view.py`, `test_capture.py` and `test_column_lineage.py`. Tests of
  engine behaviour (joins, aggregates, scans, Z-set I/O, retention, capture, lineage) move to the standalone
  host. Tests of `Pond`, the data plane, draws, hydration and the deployed-Duck e2e stay in Duckstring.
- Keep a thin Duckstring-side suite that drives the same scenarios through `Pond`, so the Duckstring host stays
  covered.
- Exit: the moved tests pass against the standalone host only.

### 4. Move the code

- Create the new repo with the package, the moved tests, CI on the DuckDB floor and pre-release (matching
  Duckstring's `duckdb>=1.5,<2`), `ruff`, and a release workflow with PyPI Trusted Publishing.
- Extras: `duckstring-trickle[lineage]` for `sqlglot`. `ibis` stays an undeclared lazy import, as now.
- In Duckstring: depend on `duckstring-trickle` with a compatible range, make `duckstring.trickle` a forwarding
  shim like the existing ones, and delete the moved code and tests.
- **Pond environments:** the Catchment's Duckstring is installed into each Pond environment last, which already
  overrides a Pond's locked DuckDB. The same must hold for `duckstring-trickle`, because a Pond's Ducks write the
  format the Catchment reads. Check `environments.duckstring_requirement` and the `--no-install-package` step
  install it from the Catchment's own environment, and add a test.
- Exit: Duckstring's full suite, including the Trickle e2e (`test_trickle_chain_runs_end_to_end`), passes
  against the published package.

### 5. Docs and launch

- README: one paragraph of what it does, the snapshot-to-incremental example, what's supported (six join types,
  the aggregate and scan families), what isn't (holistic aggregates, DISTINCT, recursion), and a link to
  Duckstring for orchestration.
- The Z-sets blog post's "Trying it in DuckDB" section switches to `pip install duckstring-trickle`.
- `docs/docs/reference/python/` keeps documenting the builder for Pond users and links to the package.
- Announce through the channels in the launch plan, leading with the killer-feature angle.

## Risks

- **Versions drifting apart.** A Duckstring release built against one Trickle format must not read another
  silently. `format_version` (phase 1) plus a compatible range in Duckstring's dependency covers this.
- **Two places to fix a bug.** Engine bugs found through Duckstring need a Trickle release first. A short
  release checklist in the new repo keeps that cheap.
- **Standalone semantics that differ from Pond semantics.** Both hosts must give the same answers for the same
  inputs. The thin Duckstring-side suite in phase 3 is the guard.
