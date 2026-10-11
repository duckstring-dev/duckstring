"""``duckstring.trickle`` is the separately released ``trickle`` package (plans/trickle-extraction.md):
the old names must be the same objects, and Duckstring's published layout carries the engine's storage
format version, so a reader refuses a newer format instead of misreading it."""

from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import pytest

from duckstring.dataplane import ParquetDataPlane


def test_the_old_names_are_the_package():
    import trickle

    import duckstring.trickle
    import duckstring.trickle.builder
    import duckstring.trickle.io
    from duckstring import acc, agg, trickle_builder, trickle_io
    from duckstring.trickle.io import read_delta

    assert duckstring.trickle is trickle
    assert duckstring.trickle.io is trickle.io and duckstring.trickle.builder is trickle.builder
    assert read_delta is trickle.io.read_delta and trickle_io.read_delta is trickle.io.read_delta
    assert trickle_builder.TrickleBuilder is trickle.TrickleBuilder
    assert agg.sum is trickle.agg.sum and acc.sum is trickle.acc.sum


def test_a_newer_storage_format_is_refused_when_published(tmp_path):
    from trickle import io

    con = duckdb.connect()
    f = datetime(2026, 1, 1, tzinfo=timezone.utc)
    io.merge_table(con, "m", con.sql("SELECT 1 AS id"), f, ("id",))
    ParquetDataPlane().export(con, tmp_path, f=f)
    assert io.load_sidecar(tmp_path)["m"]["format_version"] == io.FORMAT_VERSION

    sidecar = io.load_sidecar(tmp_path)
    sidecar["m"]["format_version"] = io.FORMAT_VERSION + 1
    io.write_sidecar(tmp_path, sidecar)
    with pytest.raises(io.FormatVersionError):
        ParquetDataPlane().read_select(tmp_path, "m")
    with pytest.raises(io.FormatVersionError):
        io.read_delta(con, tmp_path, "m", f, f, dp=ParquetDataPlane())
