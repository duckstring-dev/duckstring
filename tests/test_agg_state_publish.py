"""An aggregate's published state: a snapshot plus each run's changed and dropped groups
(dataplane._export_companions), which a Duck recovering from registry loss loads back exactly."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb

from duckstring import agg
from duckstring import trickle_io as T
from duckstring.core import Pond
from duckstring.dataplane import ParquetDataPlane, hydrate_registry
from duckstring.engine.core import NEVER


def ts(hour: int) -> datetime:
    return datetime(2026, 6, 16, tzinfo=timezone.utc) + timedelta(hours=hour)


def test_an_aggregates_state_is_published_as_its_change_and_loads_back_exactly(tmp_path):
    src = duckdb.connect(str(tmp_path / "src.duckdb"))
    src_dir = tmp_path / "ponds" / "fact" / "m1" / "data"
    snk = duckdb.connect(str(tmp_path / "snk.duckdb"))
    out = tmp_path / "ponds" / "o" / "m1" / "data"
    state_dir = out / "state" / "agg" / "by_k"
    rows = {i: (i % 40, i) for i in range(400)}  # 40 groups

    def run(f, pf):
        vals = ", ".join(f"({i}, {v}, {k})" for i, (k, v) in rows.items())
        T.merge_table(src, "f", src.sql(f"SELECT * FROM (VALUES {vals}) v(id, v, k)"), f, ("id",))
        ParquetDataPlane().export(src, src_dir, f=f)
        pond = Pond("o", "1.0.0", snk, root=tmp_path, source_majors={"fact": 1}, f=f, previous_f=pf)
        pond.trickle("fact.f", p=1.0).aggregate(by="k", total=agg.sum("v"), n=agg.count(), top=agg.max("v")).merge("by_k")
        ParquetDataPlane().export(snk, out, f=f)
        fresh = duckdb.connect()
        fresh.execute("SET TimeZone='UTC'")
        hydrate_registry(fresh, out)
        want = snk.sql('SELECT * FROM "_duckstring_agg_by_k" ORDER BY k').fetchall()
        assert fresh.sql('SELECT * FROM "_duckstring_agg_by_k" ORDER BY k').fetchall() == want

    run(ts(1), NEVER)
    assert [n.name for n in state_dir.glob("*.parquet")] == [T.part_name(ts(1))]
    rows[400] = (3, 999)  # one group changes
    run(ts(2), ts(1))
    assert [n.name for n in (state_dir / "deltas").glob("*.parquet")] == [T.part_name(ts(2))]
    assert duckdb.sql(f"SELECT count(*) FROM read_parquet('{state_dir / 'deltas' / T.part_name(ts(2))}')").fetchall() == [(1,)]
    for i in [i for i, (k, _) in rows.items() if k == 7]:
        del rows[i]  # group 7 empties
    run(ts(3), ts(2))
    gone = duckdb.sql(f"SELECT k FROM read_parquet('{state_dir / 'deltas' / T.part_name(ts(3))}') "
                      "WHERE _duckstring_gone").fetchall()
    assert gone == [(7,)]
    h = 4
    while (state_dir / "deltas").exists():  # the deltas outgrow the snapshot: a new one replaces them
        for g in range(10):
            rows[1000 + 10 * h + g] = ((h * 10 + g) % 40, h)  # ten groups change per run
        run(ts(h), ts(h - 1))
        h += 1
    assert [n.name for n in state_dir.glob("*.parquet")] == [T.part_name(ts(h - 1))]
    assert T.load_sidecar(out)["state"]["agg/by_k"]["delta_rows"] == 0
