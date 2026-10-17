"""A left-deep star chain `fact ⋈ d0 ⋈ … ⋈ d{N-1}`: one incremental run, against `ivm=False`. Run from the repo
root: `uv run python bench/trickle_join/chain.py [N …]` (default 2 4 6 8). `ONLY_FACT=1` changes only the fact
table; otherwise 10k new facts arrive and 1 in 1,000 rows of every dimension changes."""

import os
import sys
import time
from datetime import datetime, timezone

import duckdb
import trickle
from trickle import NEVER

FACT, DIM, BATCH = 5_000_000, 100_000, 10_000
ONLY_FACT = os.environ.get("ONLY_FACT") == "1"


def t(h):
    return datetime(2026, 6, 16, h, tzinfo=timezone.utc)


def main(n):
    con = duckdb.connect()

    def sources(f, pf, first, rows, seed):
        e = trickle.at(con, f, pf)
        keys = ", ".join(f"(hash(({first} + i) * {j + 3}) % {DIM})::INT AS k{j}" for j in range(n))
        e.append_table("fact", con.sql(f"SELECT {first} + i AS id, {keys} FROM range({rows}) t(i)"),
                       pk="id", fail_on_conflict=False)
        for j in range(n):
            bump = "" if seed is None else f"+ CASE WHEN hash(i + {seed + j}) % 1000 = 0 THEN 1 ELSE 0 END"
            e.merge_table(f"d{j}", con.sql(f"SELECT i AS k{j}, (i % 97) {bump} AS v{j} FROM range({DIM}) t(i)"),
                          pk=f"k{j}")

    def build(f, pf, **kw):
        e = trickle.at(con, f, pf)
        b = e.source("fact").alias("f")
        for j in range(n):
            b = b.join(e.source(f"d{j}").alias(f"d{j}"), on=f"k{j}")
        b.select("f.id, " + ", ".join(f"d{j}.v{j}" for j in range(n))).merge("out", pk="id", **kw)

    sources(t(1), NEVER, 0, FACT, None)
    build(t(1), NEVER)
    sources(t(2), t(1), FACT, BATCH, None if ONLY_FACT else 7)
    stamp = t(2).isoformat()
    for label, kw in (("incremental", {}), ("ivm=False", {"ivm": False})):
        times = []
        for _ in range(3):
            t0 = time.perf_counter()
            build(t(2), t(1), **kw)
            times.append(time.perf_counter() - t0)
            out = con.sql(f"SELECT count(*), sum(_duckstring_d) FROM out__changelog "
                          f"WHERE _duckstring_f = '{stamp}'").fetchone()
            con.execute(f"DELETE FROM out__changelog WHERE _duckstring_f = '{stamp}'")
        print(f"joins={n}  {label:12s} {sorted(times)[1] * 1000:7.0f} ms   changelog rows, net: {out}")


if __name__ == "__main__":
    for arg in sys.argv[1:] or ["2", "4", "6", "8"]:
        main(int(arg))
