"""Ordering a merge table's cold base (plans/data-plane-clustering.md): by its primary key by default, by
``cluster_by`` when declared (a plain sort for one column or ``interleave=False``, a rank-Hilbert key for
two or more), so reads filtering on those columns skip most row groups."""

from __future__ import annotations

import random
from datetime import datetime, timezone

import duckdb
import pytest

from duckstring import trickle_io as T
from duckstring.dataplane import ParquetDataPlane, _auto_cluster_bits, _bits_per_column, rank_hilbert_sql
from duckstring.trickle.io import DeltaError

F1 = datetime(2026, 6, 1, tzinfo=timezone.utc)
F2 = datetime(2026, 6, 2, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _compact_every_publish(monkeypatch):
    monkeypatch.setenv("DUCKSTRING_COMPACT_THRESHOLD", "1")  # the first publish checkpoints into a base


def _con():
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    return con


def _publish(con, out, rows_sql, **cluster):
    T.merge_table(con, "m", con.sql(rows_sql), F1, ("id",), **cluster)
    ParquetDataPlane().export(con, out, f=F1)


def _row_groups(out):
    """``[(column, min, max), …]`` per row group of the published base, from the Parquet footers."""
    con = duckdb.connect()
    rows = con.execute(
        f"SELECT file_name, row_group_id, path_in_schema, stats_min_value, stats_max_value "
        f"FROM parquet_metadata('{out}/m__base/*.parquet')"
    ).fetchall()
    groups: dict = {}
    for file, rg, col, lo, hi in rows:
        if col in ("id", "a", "b"):
            groups.setdefault((file, rg), {})[col] = (int(lo), int(hi))
    return list(groups.values())


def _fraction_touched(groups, col, values):
    """The mean share of row groups whose ``col`` range could hold each of ``values`` (lower = better)."""
    return sum(sum(g[col][0] <= v <= g[col][1] for g in groups) / len(groups) for v in values) / len(values)


_ROWS = ("SELECT i AS id, hash(i) % 1000 AS a, hash(i * 31) % 1000 AS b "
         "FROM range(1000000) r(i) ORDER BY hash(i * 7)")  # ids, a and b all arrive shuffled


@pytest.mark.timeout(120)
def test_the_default_base_is_ordered_by_primary_key(tmp_path):
    con = _con()
    _publish(con, tmp_path, _ROWS)
    groups = _row_groups(tmp_path)
    ranges = sorted(g["id"] for g in groups)
    assert len(ranges) > 4
    assert all(ranges[i][1] < ranges[i + 1][0] for i in range(len(ranges) - 1))  # disjoint pk ranges


@pytest.mark.timeout(120)
def test_an_interleaved_base_prunes_on_every_clustered_column(tmp_path, monkeypatch):
    """With ~100 row groups (a smaller row group stands in for a large table), interleaving narrows every
    row group on both columns, where sorting by (a, b) narrows only a."""
    from duckstring import dataplane

    monkeypatch.setattr(dataplane, "_ROW_GROUP_ROWS", 10_000)
    values = random.Random(1).sample(range(1000), 50)
    plain, sort, hilbert = tmp_path / "plain", tmp_path / "sort", tmp_path / "hilbert"
    _publish(_con(), plain, _ROWS)
    _publish(_con(), sort, _ROWS, cluster_by=["a", "b"], interleave=False)
    _publish(_con(), hilbert, _ROWS, cluster_by=["a", "b"])
    touched = {name: {col: _fraction_touched(_row_groups(d), col, values) for col in ("a", "b")}
               for name, d in (("plain", plain), ("sort", sort), ("hilbert", hilbert))}
    assert touched["plain"]["a"] > 0.95 and touched["plain"]["b"] > 0.95  # pk order: no help on either
    assert touched["sort"]["a"] < 0.05 and touched["sort"]["b"] > 0.9    # sorted: only the first column
    assert touched["hilbert"]["a"] < 0.25 and touched["hilbert"]["b"] < 0.25  # rank-Hilbert: both
    con = _con()
    read = ParquetDataPlane().read_select(hilbert, "m")
    assert con.execute(f"SELECT count(*), sum(a), sum(b) FROM ({read})").fetchone() == \
        con.execute(f"SELECT count(*), sum(a), sum(b) FROM ({_ROWS})").fetchone()


@pytest.mark.timeout(120)
def test_one_column_or_interleave_false_is_a_plain_sort(tmp_path):
    _publish(_con(), tmp_path / "one", _ROWS, cluster_by="a")
    a_ranges = sorted(g["a"] for g in _row_groups(tmp_path / "one"))
    assert all(a_ranges[i][1] <= a_ranges[i + 1][0] for i in range(len(a_ranges) - 1))
    _publish(_con(), tmp_path / "sorted", _ROWS, cluster_by=["b", "a"], interleave=False)
    b_ranges = sorted(g["b"] for g in _row_groups(tmp_path / "sorted"))
    assert all(b_ranges[i][1] <= b_ranges[i + 1][0] for i in range(len(b_ranges) - 1))


def test_the_rank_hilbert_key():
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES (1, 'a'), (2, 'b'), (3, 'c'), (4, 'd'), (NULL, 'e')) v(x, y)")
    rows = con.sql(rank_hilbert_sql('"t"', ["x", "y"], 2, [True, False]) + ' ORDER BY "_duckstring_key"').fetchall()
    # x ranks 0,0,1,2 (three buckets; NULL takes the reserved top bucket, 3); y ranks 0,0,1,2,3. On the 4x4
    # Hilbert curve those cells are visited at positions 0, 0, 2, 8 and 10.
    assert rows == [(1, "a", 0), (2, "b", 0), (3, "c", 2), (4, "d", 8), (None, "e", 10)]
    assert _bits_per_column(11, 2) == 6 and _bits_per_column(7, 3) == 3 and _bits_per_column(63, 2) == 31
    assert _auto_cluster_bits(100_000_000, 2) == 11  # ~814 row groups: cells just under one row group
    assert _auto_cluster_bits(1_000, 3) == 3         # at least one bit per column
    assert _auto_cluster_bits(10**30, 2) == 63


def test_the_hilbert_key_visits_every_cell_through_neighbours():
    """Over a full grid of distinct values the key is a bijection onto [0, 2**(k*b)) whose consecutive values
    are grid neighbours: the defining property of a Hilbert curve."""
    import itertools

    con = duckdb.connect()
    for k, b in [(2, 3), (3, 2), (4, 1)]:
        side = 1 << b
        cells = list(itertools.product(range(side), repeat=k))
        cols = [f"c{i}" for i in range(k)]
        values = ", ".join("(" + ", ".join(map(str, c)) + ")" for c in cells)
        con.execute(f"CREATE OR REPLACE TABLE g AS SELECT * FROM (VALUES {values}) v({', '.join(cols)})")
        rows = con.sql(rank_hilbert_sql("g", cols, b, [False] * k) + ' ORDER BY "_duckstring_key"').fetchall()
        assert [r[-1] for r in rows] == list(range(side ** k))
        for a, c in zip(rows, rows[1:], strict=False):
            assert sum(abs(x - y) for x, y in zip(a[:-1], c[:-1], strict=True)) == 1


def test_equal_values_share_a_bucket():
    """A boundary is a value, not a row position, so ties never straddle buckets and the key is
    deterministic."""
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT i AS id, i % 3 AS a, (i * 7) % 5 AS b FROM range(3000) r(i)")
    sql = rank_hilbert_sql("t", ["a", "b"], 3, [False, False])
    keys = con.sql(f'SELECT a, b, count(DISTINCT "_duckstring_key") FROM ({sql}) GROUP BY ALL').fetchall()
    assert all(n == 1 for _, _, n in keys)
    first = con.sql(f'SELECT id, "_duckstring_key" FROM ({sql}) ORDER BY id').fetchall()
    assert first == con.sql(f'SELECT id, "_duckstring_key" FROM ({sql}) ORDER BY id').fetchall()


def test_approximate_boundaries_cluster_like_exact_ones():
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT i AS id, exp((i % 1000) / 100.0) AS a, (i * 7919) % 1000 AS b "
                "FROM range(200000) r(i)")
    exact = rank_hilbert_sql("t", ["a", "b"], 4, [False, False])
    approx = rank_hilbert_sql("t", ["a", "b"], 4, [False, False], [True, True])
    # Ranking flattens the skewed column, so each sixteenth of the curve holds about a sixteenth of the rows.
    for sql in (exact, approx):
        sizes = [n for (n,) in con.sql(f'SELECT count(*) FROM ({sql}) GROUP BY "_duckstring_key" >> 4').fetchall()]
        assert len(sizes) <= 16 and max(sizes) < 2 * 200000 / 16


@pytest.mark.timeout(120)
def test_a_large_base_uses_approximate_boundaries(tmp_path, monkeypatch):
    """Past the exact-quantile threshold a numeric column's boundaries come from approx_quantile; a string
    column, which approx_quantile can't take, stays exact. Both still cluster."""
    from duckstring import dataplane

    monkeypatch.setattr(dataplane, "_EXACT_QUANTILE_ROWS", 1000)
    monkeypatch.setattr(dataplane, "_ROW_GROUP_ROWS", 10_000)
    rows = ("SELECT i AS id, hash(i) % 1000 AS a, lpad((hash(i * 31) % 1000)::VARCHAR, 4, '0') AS b "
            "FROM range(1000000) r(i) ORDER BY hash(i * 7)")
    _publish(_con(), tmp_path, rows, cluster_by=["a", "b"])
    con = duckdb.connect()
    groups: dict = {}
    for file, rg, col, lo, hi in con.execute(
            f"SELECT file_name, row_group_id, path_in_schema, stats_min_value, stats_max_value "
            f"FROM parquet_metadata('{tmp_path}/m__base/*.parquet')").fetchall():
        if col in ("a", "b"):
            groups.setdefault((file, rg), {})[col] = (int(lo), int(hi))
    values = random.Random(3).sample(range(1000), 50)
    for col in ("a", "b"):
        assert _fraction_touched(list(groups.values()), col, values) < 0.25


@pytest.mark.parametrize("kwargs, message", [
    ({"cluster_bits": 8}, "need cluster_by"),
    ({"cluster_by": "a", "cluster_bits": 8}, "only when interleaving"),
    ({"cluster_by": ["a", "b"], "interleave": False, "cluster_bits": 8}, "only when interleaving"),
    ({"cluster_by": ["a", "b"], "cluster_bits": 1}, "from 2"),
    ({"cluster_by": ["a", "a"]}, "twice"),
    ({"cluster_by": ["nope"]}, "not in 'm'"),
])
def test_invalid_clustering_is_refused(tmp_path, kwargs, message):
    con = _con()
    with pytest.raises(DeltaError, match=message):
        T.merge_table(con, "m", con.sql("SELECT 1 AS id, 2 AS a, 3 AS b"), F1, ("id",), **kwargs)


def test_the_declaration_is_per_write(tmp_path):
    con = _con()
    T.merge_table(con, "m", con.sql("SELECT 1 AS id, 2 AS a"), F1, ("id",), cluster_by="a")
    assert T.read_meta(con)["m"]["cluster"] == {"by": ["a"], "interleave": False, "bits": None}
    T.merge_table(con, "m", con.sql("SELECT 1 AS id, 2 AS a"), F2, ("id",))  # dropped: back to pk order
    assert T.read_meta(con)["m"]["cluster"] is None


def test_the_builder_and_sql_ripples_declare_it_too(tmp_path):
    from duckstring.core import Pond, RippleDeclarationError, load_ripples

    con = _con()
    pond = Pond(name="p", version="1", con=con, root=tmp_path, f=F1)
    pond.merge_table("src", con.sql("SELECT range AS id, range % 3 AS a, range % 5 AS b FROM range(10)"), pk="id")
    pond.trickle("src").select("s0.id, s0.a, s0.b").merge("out", pk="id", cluster_by=["a", "b"], cluster_bits=4)
    assert T.read_meta(con)["out"]["cluster"] == {"by": ["a", "b"], "interleave": True, "bits": 4}

    (tmp_path / "pond.toml").write_text(
        '[pond]\nname = "p"\nversion = "1.0.0"\n\n[ripples.r]\nsql = "r.sql"\nwrite = "merge"\npk = "id"\n'
        'cluster_by = ["a", "b"]\ncluster_bits = 6\n')
    (tmp_path / "r.sql").write_text("SELECT 1 AS id, 2 AS a, 3 AS b")
    assert load_ripples(tmp_path).by_name["r"]["sql"].cluster == {"by": ["a", "b"], "interleave": True, "bits": 6}
    (tmp_path / "pond.toml").write_text(
        '[pond]\nname = "p"\nversion = "1.0.0"\n\n[ripples.r]\nsql = "r.sql"\nwrite = "append"\n'
        'cluster_by = ["a"]\n')
    with pytest.raises(RippleDeclarationError, match="not \"append\""):
        load_ripples(tmp_path)


# ─── plain tables (write_table) ──────────────────────────────────────────────────


def _published_groups(con, out, table):
    """Per row group ``{column: (min, max)}`` of a plain table's published version."""
    path = ParquetDataPlane().table_path(out, table)
    groups: dict = {}
    for rg, col, lo, hi in con.execute(
        f"SELECT row_group_id, path_in_schema, stats_min_value, stats_max_value FROM parquet_metadata('{path}')"
    ).fetchall():
        groups.setdefault(rg, {})[col] = (int(lo), int(hi))
    return list(groups.values())


@pytest.mark.timeout(120)
def test_a_plain_table_is_ordered_as_it_is_written(tmp_path):
    from duckstring.core import Pond

    values = random.Random(2).sample(range(1000), 50)
    con = _con()
    pond = Pond(name="p", version="1", con=con, root=tmp_path, f=F1)
    pond.write_table("unordered", con.sql(_ROWS))
    pond.write_table("by_a", con.sql(_ROWS), cluster_by="a")
    pond.write_table("both", con.sql(_ROWS), cluster_by=["a", "b"])
    ParquetDataPlane().export(con, tmp_path / "out", f=F1)

    a_ranges = sorted(g["a"] for g in _published_groups(con, tmp_path / "out", "by_a"))
    assert all(a_ranges[i][1] <= a_ranges[i + 1][0] for i in range(len(a_ranges) - 1))  # sorted by a
    plain = _published_groups(con, tmp_path / "out", "unordered")
    both = _published_groups(con, tmp_path / "out", "both")
    for col in ("a", "b"):
        assert _fraction_touched(both, col, values) < _fraction_touched(plain, col, values)
    assert con.execute('SELECT count(*), sum(a) FROM "both"').fetchone() == \
        con.execute(f"SELECT count(*), sum(a) FROM ({_ROWS})").fetchone()


def test_write_table_refuses_an_unknown_cluster_column(tmp_path):
    from duckstring.core import Pond

    con = _con()
    pond = Pond(name="p", version="1", con=con, root=tmp_path, f=F1)
    with pytest.raises(DeltaError, match="not in 't'"):
        pond.write_table("t", con.sql("SELECT 1 AS a"), cluster_by="nope")

