"""Does a Source whose change only adds rows need the change-fraction check (`p`)? (plans/showcase-fixes.md item 19's
follow-up.) Over `p` (default 0.3) of a Source's rows in one Run's change, the builder recomputes the whole output
and diffs it against what it wrote. For a change that only adds rows, joining just the new rows is a subset of the
recompute's join, so the check may only cost time.

Each case: a fact table of N rows (append) and a 1M-row dimension (merge) are bootstrapped, then one Run adds
`frac * N` facts. The Run is timed with the check on (p=0.3) and off (p=1.0), from a fresh database each time
(best of REPS). Run from the repo root: `uv run python bench/p_threshold/run.py`.

Cases:
- `join.merge`: facts joined to the dimension, written with `.merge()`.
- `join.append`: the same, written with `.append()`.
- `aggregate`: facts summed per dimension key with `.aggregate().merge()`.
- `join.merge+dim`: as `join.merge`, with 1,000 dimension rows repriced in the same Run (both sources change).
"""

import os
import time
from datetime import datetime, timezone

import duckdb
import trickle
from trickle import NEVER, agg

N = int(os.environ.get("N", 4_000_000))
DIM = 1_000_000
REPS = int(os.environ.get("REPS", 2))
FRACS = [float(x) for x in os.environ.get("FRACS", "0.1,0.3,0.5,1,2,4").split(",")]
CASES = os.environ.get("CASES", "join.merge,join.append,aggregate,join.merge+dim").split(",")
SEL = "s0.order_id, s0.product_id, s0.quantity, s1.unit_price, s0.quantity * s1.unit_price AS revenue"


def t(h):
    return datetime(2026, 6, 16, h, tzinfo=timezone.utc)


def facts(e, first, n):
    e.append_table("fact", e.con.sql(f"""
        SELECT {first} + i AS order_id, (hash(i + {first}) % {DIM})::INT + 1 AS product_id,
               (hash(i * 7 + {first}) % 8)::INT + 1 AS quantity FROM range({n}) t(i)"""), pk="order_id")


def dim(e, reprice):
    drift = f"CASE WHEN product_id % {DIM // 1000} = 0 THEN 1.1 ELSE 1.0 END *" if reprice else ""
    e.merge_table("product", e.con.sql(f"""
        SELECT i + 1 AS product_id, {drift} (5.0 + ((i + 1) * 37 % 2000) * 0.5) AS unit_price FROM range({DIM}) t(i)"""),
        pk="product_id")


def build(e, case, p):
    if case == "aggregate":
        return (e.source("fact", p=p).aggregate(by="product_id", units=agg.sum("quantity"), n=agg.count())
                .merge("out"))
    joined = e.source("fact", p=p).join(e.source("product", p=p), on="product_id").select(SEL)
    if case == "join.append":
        return joined.append("out", pk="order_id")
    return joined.merge("out", pk="order_id")


def run(case, frac, p):
    con = duckdb.connect()
    e = trickle.at(con, t(1), NEVER)
    facts(e, 0, N)
    dim(e, False)
    build(e, case, p)
    e = trickle.at(con, t(2), t(1))
    facts(e, N, int(frac * N))
    if case.endswith("+dim"):
        dim(e, True)
    t0 = time.perf_counter()
    build(trickle.at(con, t(2), t(1)), case, p)
    took = time.perf_counter() - t0
    check = con.sql("SELECT count(*), sum(hash(COLUMNS(* EXCLUDE (_duckstring_f))))::HUGEINT FROM out").fetchall()[0] \
        if case == "join.append" else con.sql("SELECT count(*) FROM out").fetchall()[0]
    con.close()
    return took, check


def main():
    print(f"N = {N:,} facts, {DIM:,} dimension rows, best of {REPS}; seconds for the timed Run")
    print(f"{'case':<16}{'frac':>6}{'p=0.3':>9}{'p=1.0':>9}  {'ratio':>6}")
    for case in CASES:
        for frac in FRACS:
            res = {}
            for p in (0.3, 1.0):
                best, check = None, None
                for _ in range(REPS):
                    took, check = run(case, frac, p)
                    best = took if best is None else min(best, took)
                res[p] = (best, check)
            assert res[0.3][1] == res[1.0][1], (case, frac, res)
            print(f"{case:<16}{frac:>6}{res[0.3][0]:>9.2f}{res[1.0][0]:>9.2f}  {res[1.0][0] / res[0.3][0]:>6.2f}",
                  flush=True)


if __name__ == "__main__":
    main()
