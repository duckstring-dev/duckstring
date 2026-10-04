"""Table and Object references: ``"name"`` / ``"source.name"``, with backticks for names containing dots,
and a clear error (suggesting backticks) for a reference to a Source the Pond doesn't declare."""
from __future__ import annotations

from datetime import datetime, timezone

import duckdb
import pytest

from duckstring.core import Pond, Puddle, parse_ref
from duckstring.dataplane import ParquetDataPlane


@pytest.mark.parametrize("ref, expected", [
    ("sale_line", (None, "sale_line")),
    ("sales.sale_line", ("sales", "sale_line")),
    ("sales.daily.v2", ("sales", "daily.v2")),          # unquoted: split at the first dot only
    ("`model.pkl`", (None, "model.pkl")),
    ("forecasting.`model.pkl`", ("forecasting", "model.pkl")),
    ("`fore.casting`.`model.pkl`", ("fore.casting", "model.pkl")),
    ("`fore.casting`.model", ("fore.casting", "model")),
])
def test_parse_ref(ref, expected):
    assert parse_ref(ref) == expected


@pytest.mark.parametrize("ref", ["`model.pkl", "`a`b", "sales.`x`y", "", ".x", "x.", "``"])
def test_parse_ref_rejects_malformed(ref):
    with pytest.raises(ValueError):
        parse_ref(ref)


def _pond(tmp_path, con, *, sources=("sales",)):
    staging, own = tmp_path / "staging", tmp_path / "own"
    staging.mkdir(exist_ok=True)
    own.mkdir(exist_ok=True)
    return Pond("forecasting", "1.0.0", con, root=tmp_path, f=datetime(2026, 1, 1, tzinfo=timezone.utc),
                staging_dir=staging, own_data_dir=own, sources=list(sources))


def test_own_dotted_object_reads_with_backticks(tmp_path):
    pond = _pond(tmp_path, duckdb.connect())
    pond.write_object("model.pkl", b"weights")
    assert pond.read_object("`model.pkl`") == b"weights"
    assert pond.object_path("`model.pkl`").read_bytes() == b"weights"


def test_unquoted_dotted_name_for_an_undeclared_source_suggests_backticks(tmp_path):
    pond = _pond(tmp_path, duckdb.connect())
    pond.write_object("model.pkl", b"weights")
    with pytest.raises(ValueError, match=r"'model' is not a Source of 'forecasting'.*`model.pkl`"):
        pond.read_object("model.pkl")
    with pytest.raises(ValueError, match="is not a Source"):
        pond.read_table("nowhere.table")


def test_source_table_with_a_dotted_name(tmp_path):
    src = duckdb.connect()
    src.execute('CREATE TABLE "daily.v2" AS SELECT 7 AS n')
    ParquetDataPlane().export(src, tmp_path / "ponds" / "sales" / "data")
    src.close()
    con = duckdb.connect()
    pond = _pond(tmp_path, con)
    assert pond.read_table("sales.`daily.v2`").fetchall() == [(7,)]
    assert con.sql('SELECT n FROM "daily.v2"').fetchall() == [(7,)]  # registered under its literal name
    assert pond.read_table("sales.daily.v2").fetchall() == [(7,)]    # unquoted: split at the first dot


def test_own_name_as_source_means_own(tmp_path):
    con = duckdb.connect()
    con.execute("CREATE TABLE summary AS SELECT 1 AS n")
    pond = _pond(tmp_path, con)
    assert pond.read_table("forecasting.summary").fetchall() == [(1,)]


def test_undeclared_check_is_skipped_when_sources_are_unknown(tmp_path):
    """A handle built without knowing the Pond's Sources keeps the old behaviour: no declared-Source check."""
    con = duckdb.connect()
    pond = Pond("forecasting", "1.0.0", con, root=tmp_path)
    from duckstring.core import MissingSourceAsset

    with pytest.raises(MissingSourceAsset):
        pond.read_table("anything.table")


def test_puddle_targets_accept_backticks(tmp_path):
    p = Puddle("sales.`daily.v2`", root=tmp_path)
    assert (p.source, p.table) == ("sales", "daily.v2")
    whole = Puddle("sales", root=tmp_path)
    assert (whole.source, whole.table) == ("sales", None)


@pytest.mark.parametrize("name", ["model.pkl", "forecast.v2.pkl", "_scratch.bin"])
def test_dotted_object_names_are_allowed(name):
    from duckstring.objects import validate_object_name

    assert validate_object_name(name) == name


@pytest.mark.parametrize("name", ["..", ".hidden", "model.", "a..b", "1model", "a/b", "_duckstring_x.pkl"])
def test_unsafe_object_names_are_rejected(name):
    from duckstring.objects import ObjectError, validate_object_name

    with pytest.raises(ObjectError):
        validate_object_name(name)
