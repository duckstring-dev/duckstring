"""Correctness checks for the encodings, run before trusting a benchmark: ``python bench/clustering/check.py``.

- The Hilbert SQL is a bijection onto ``[0, 2**(n*b))`` whose consecutive indices are grid neighbours.
- The Morton SQL is a bijection.
- The benchmark's ``qrank_hilbert`` key equals Duckstring's own ``rank_hilbert_sql`` key, ties and NULLs
  included.
- Binary-search bucketing equals one comparison per boundary.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
import encodings_sql as enc  # noqa: E402


def _grid_keys(con, n: int, b: int, hilbert: bool) -> list[tuple]:
    cells = list(itertools.product(range(1 << b), repeat=n))
    values = ", ".join("(" + ", ".join(map(str, c)) + ")" for c in cells)
    # Carry the original coordinates through every Hilbert layer.
    names = ", ".join(enc.bucket_cols(n))
    copies = ", ".join(f"_bk{i} AS _x{i}" for i in range(n))
    carried = f"SELECT *, {copies} FROM (VALUES {values}) t({names})"
    if hilbert:
        carried = enc.hilbert_transpose_sql(carried, n, b)
    return con.execute(f"SELECT {enc.interleave_expr(n, b)} AS k, {', '.join(f'_x{i}' for i in range(n))} "
                       f"FROM ({carried}) ORDER BY k").fetchall()


def check_curves(con) -> None:
    for n, b in [(2, 1), (2, 3), (2, 5), (3, 1), (3, 3), (4, 2)]:
        for hilbert in (False, True):
            rows = _grid_keys(con, n, b, hilbert)
            keys = [r[0] for r in rows]
            assert keys == list(range(1 << (n * b))), f"not a bijection: n={n} b={b} hilbert={hilbert}"
            if hilbert:
                for a, c in zip(rows, rows[1:], strict=False):
                    dist = sum(abs(x - y) for x, y in zip(a[1:], c[1:], strict=True))
                    assert dist == 1, f"Hilbert jump n={n} b={b}: {a} -> {c}"
    print("curves ok")


def check_duckstring_parity(con) -> None:
    """Duckstring's ``rank_hilbert_sql`` (exact boundaries below its threshold) gives the same key as the
    benchmark's ``qrank_hilbert``: exact quantile boundaries, a Hilbert curve. Ties and NULLs included, since
    a boundary is a value and equal values always share a bucket."""
    from duckstring import dataplane

    con.execute("""CREATE OR REPLACE TABLE t AS
        SELECT i AS id,
               CASE WHEN i % 17 = 0 THEN NULL ELSE (i * 7919) % 1000 END AS a,
               (i * i) % 97 AS b2,
               CASE WHEN i % 5 = 0 THEN NULL ELSE exp((i % 50) / 5.0) END AS c
        FROM range(20000) r(i)""")
    cols = ["a", "b2", "c"]
    stats = [{"nulls": con.execute(f"SELECT count(*) FILTER (WHERE {c} IS NULL) FROM t").fetchone()[0]} for c in cols]
    for b in (1, 3, 5):
        st = [dict(s, bounds=con.execute(enc.quantile_bounds_sql("t", c, b, s["nulls"] > 0)).fetchone()[0])
              for c, s in zip(cols, stats, strict=True)]
        ours = con.execute(f"SELECT id, _key FROM ({enc.keyed_sql('qrank_hilbert', 't', cols, b, st)}) ORDER BY id")
        ours = ours.fetchall()
        theirs = dataplane.rank_hilbert_sql("t", cols, b, [s["nulls"] > 0 for s in stats])
        theirs = con.execute(f'SELECT id, "_duckstring_key" FROM ({theirs}) ORDER BY id').fetchall()
        assert ours == theirs, f"rank-Hilbert key differs from Duckstring's at b={b}"
    print("qrank_hilbert matches duckstring.dataplane.rank_hilbert_sql")


def check_bisect(con) -> None:
    """Binary-search bucketing gives the same buckets as one comparison per boundary, ties and NULLs included."""
    con.execute("""CREATE OR REPLACE TABLE u AS
        SELECT i AS id, CASE WHEN i % 11 = 0 THEN NULL ELSE (i * 37) % 500 END AS v FROM range(5000) r(i)""")
    nulls = con.execute("SELECT count(*) FILTER (WHERE v IS NULL) FROM u").fetchone()[0]
    for b in (1, 2, 4, 6):
        bounds = con.execute(enc.quantile_bounds_sql("u", "v", b, True)).fetchone()[0]
        stats = [{"nulls": nulls, "bounds": bounds}]
        linear = con.execute(f"SELECT id, _bk0 FROM ({enc.qranked_sql('u', ['v'], b, stats)}) ORDER BY id").fetchall()
        bisect = con.execute(f"SELECT id, _bk0 FROM ({enc.qranked_sql('u', ['v'], b, stats, bisect=True)}) "
                             "ORDER BY id").fetchall()
        assert linear == bisect, f"binary-search buckets differ at b={b}"
    print("binary-search bucketing matches")


if __name__ == "__main__":
    con = duckdb.connect()
    check_curves(con)
    check_duckstring_parity(con)
    check_bisect(con)
