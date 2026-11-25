"""Change points survive combined files (dataplane.change_points): a Source's small per-Run parts are combined into
one file (union_parts), whose name gives only its two ends, but its rows keep their stamps. ``--next-change``, a
copied Puddle's history and ``/api/ponds/{name}/changes`` are built on these points; before, everything inside a
combined file was skipped (the attention showcase's dev Catchment stepped from 18 Nov 23:55 straight to 19 Nov
19:40, 2026-11-21)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from duckstring import dataplane
from duckstring.core import Pond
from duckstring.dataplane import ParquetDataPlane, change_points

pytestmark = pytest.mark.timeout(30)
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def ts(h: float) -> datetime:
    return T0 + timedelta(hours=h)


def test_every_runs_point_is_listed_after_its_parts_are_combined(tmp_path, monkeypatch):
    monkeypatch.setattr(dataplane, "_tier_files", lambda: 2)  # combine as soon as there are two parts
    data = tmp_path / "data"
    con = duckdb.connect(str(tmp_path / "reg.duckdb"))
    for h in range(1, 7):
        pond = Pond("src", "1.0.0", con, root=tmp_path, f=ts(h), sources=[], own_data_dir=data)
        pond.append_table("event", con.sql(f"SELECT {h} AS id"), pk="id")
        pond.merge_table("dim", con.sql(f"SELECT 1 AS k, {h} AS v"), pk="k")
        ParquetDataPlane().export(con, data, f=ts(h), exact_ints=True)
    names = [p.name for p in (data / "event").iterdir()]
    assert any("~" in n for n in names), names  # some parts were combined
    assert change_points(data) == [ts(h) for h in range(1, 7)]
    assert change_points(data, after=ts(2), upto=ts(4)) == [ts(3), ts(4)]
