"""Many small Runs (plans/showcase-fixes.md item 0): does a Run's time grow with the number of Runs before it?

Two Ponds published through the data plane, as a Duck runs them: each Run on a fresh cursor of the Pond's
persistent registry, then the export. The Inlet `obs` appends 200 observations a Run and rewrites a 50-row
dimension `stop`, which changes once every 500 Runs (otherwise unchanged: an empty merge). The Sink `scored`
joins each Run's new observations to the dimension (a builder `.append()` on the spine-key fast path) and keeps
an incremental per-stop aggregate. The transit showcase saw Run time rise linearly over 210 Runs.

Run from the repo root: `uv run python bench/small_runs/run.py [RUNS]` (default 2000). Prints, per block of 100
Runs, the median Inlet and Sink Run times, the files each Pond has published, and the views stored in the Sink's
registry."""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

from duckstring.core import Pond
from duckstring.dataplane import ParquetDataPlane
from duckstring.storage import LocalStorage
from duckstring.trickle import NEVER, agg

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
BATCH, STOPS, DIM_EVERY = 200, 50, 500


def files(path: Path) -> int:
    return sum(1 for _ in path.rglob("*.parquet"))


def main(runs: int) -> None:
    root = Path(tempfile.mkdtemp(prefix="ds-small-runs-"))
    dirs = {n: root / "ponds" / n / "m1" / "data" for n in ("obs", "scored")}
    regs = {n: duckdb.connect(str(root / f"{n}.duckdb")) for n in ("obs", "scored")}
    dp = ParquetDataPlane()
    prev = NEVER
    block: dict[str, list[float]] = {"obs": [], "scored": []}
    print("runs    obs ms  scored ms  obs files  scored files  stored views")
    for i in range(runs):
        f = T0 + timedelta(seconds=31 * i)

        start = time.perf_counter()
        cur = regs["obs"].cursor()
        p = Pond("obs", "1.0.0", cur, root, f=f, previous_f=prev, own_data_dir=LocalStorage(dirs["obs"]))
        p.append_table("stop_obs", cur.sql(
            f"SELECT {i * BATCH} + range AS obs_id, range % {STOPS} AS stop_id, (range * 7 + {i}) % 300 AS delay "
            f"FROM range({BATCH})"), pk="obs_id")
        version = i // DIM_EVERY
        p.merge_table("stop", cur.sql(f"SELECT range AS stop_id, 'stop ' || range || ' v{version}' AS name "
                                      f"FROM range({STOPS})"), pk="stop_id")
        cur.close()
        dp.export(regs["obs"].cursor(), LocalStorage(dirs["obs"]), f=f, retain_from=prev if prev != NEVER else None)
        block["obs"].append(time.perf_counter() - start)

        start = time.perf_counter()
        cur = regs["scored"].cursor()
        s = Pond("scored", "1.0.0", cur, root, source_majors={"obs": 1}, f=f, previous_f=prev,
                 source_f={"obs": f.isoformat()}, own_data_dir=LocalStorage(dirs["scored"]))
        (s.trickle("obs.stop_obs").join(s.trickle("obs.stop"), on="stop_id")
         .select("s0.obs_id, s0.stop_id, s0.delay, s1.name")
         .append("enriched", pk="obs_id", on_conflict="keep_prior", log_drops=False))
        (s.trickle("obs.stop_obs").aggregate(by="stop_id", n=agg.count(), late=agg.sum("delay"))
         .merge("per_stop"))
        cur.close()
        dp.export(regs["scored"].cursor(), LocalStorage(dirs["scored"]), f=f,
                  retain_from=prev if prev != NEVER else None)
        block["scored"].append(time.perf_counter() - start)

        prev = f
        if (i + 1) % 100 == 0:
            views = regs["scored"].execute(
                "SELECT count(*) FROM duckdb_views() WHERE NOT temporary AND NOT internal").fetchall()[0][0]
            print(f"{i + 1:>5}  {statistics.median(block['obs']) * 1000:>7.1f}  "
                  f"{statistics.median(block['scored']) * 1000:>9.1f}  {files(dirs['obs']):>9}  "
                  f"{files(dirs['scored']):>12}  {views:>12}", flush=True)
            block = {"obs": [], "scored": []}


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 2000)
