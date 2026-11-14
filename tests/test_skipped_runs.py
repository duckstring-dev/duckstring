"""What a Run that changed nothing publishes (the transit showcase, plans/showcase-fixes.md item 2): a merge table
that bootstrapped and never changed gained an empty marker part on every Run, and a Run that called
``pond.skip()`` republished every plain table, kept until the next real change."""

from __future__ import annotations

from datetime import timedelta

import pytest
from test_change_detection import F1, Producer, _sink

from duckstring.dataplane import HOLD_ALL, ParquetDataPlane
from duckstring.duck.core import DuckCore
from duckstring.engine import pond as ledger
from duckstring.trickle.io import RUN_KEY, changelog_name, part_name, table_parts


def _at(i: int):
    return F1 + timedelta(minutes=i)


def test_a_merge_table_that_never_changes_keeps_one_marker_part(tmp_path):
    prod = Producer(tmp_path)
    for i in range(40):
        p = prod.handle(_at(i))
        p.merge_table("timetable", p.con.sql("SELECT range AS k, 'stop ' || range AS v FROM range(5)"), pk="k")
        prod.publish(_at(i), retain_from=_at(i - 1) if i else None)
    assert table_parts(prod.data_dir, changelog_name("timetable")) == [part_name(_at(0))]
    # A covered consumer still reads an empty delta, not a full read.
    sink = _sink(tmp_path, _at(39), _at(30))
    delta = sink.read_delta("src.timetable")
    assert not delta.is_full and delta.is_empty()
    assert sorted(sink.read_table("src.timetable").fetchall())[0] == (0, "stop 0")


def test_the_marker_survives_a_change_and_a_quiet_spell_after_it(tmp_path):
    """After a real change the change log has parts again; once retention folds them, the newest stays, so the
    export never needs to write a marker per Run again."""
    prod = Producer(tmp_path)
    values = {0: "a"}
    for i in range(30):
        if i == 5:
            values = {0: "b", 1: "c"}
        p = prod.handle(_at(i))
        rows = ", ".join(f"({k}, '{v}')" for k, v in values.items())
        p.merge_table("dim", p.con.sql(f"SELECT * FROM (VALUES {rows}) s(k, v)"), pk="k", retain_n=1)
        prod.publish(_at(i), retain_from=_at(i - 1) if i else None)
    parts = table_parts(prod.data_dir, changelog_name("dim"))
    assert len(parts) <= 2 and part_name(_at(5)) in parts
    sink = _sink(tmp_path, _at(29), _at(20))
    assert sink.read_delta("src.dim").is_empty()
    assert sorted(sink.read_table("src.dim").fetchall()) == [(0, "b"), (1, "c")]


def test_a_skipped_run_holds_every_plain_table(tmp_path):
    prod = Producer(tmp_path)
    p = prod.handle(F1)
    p.write_table("feed_version", p.con.sql("SELECT 'v1' AS v"))
    prod.publish(F1)
    for i in range(1, 30):  # skipped Runs: the Ripple may even rewrite the table, unchanged
        p = prod.handle(_at(i))
        p.write_table("feed_version", p.con.sql("SELECT 'v1' AS v"))
        ParquetDataPlane().export(prod.con, prod.data_dir, f=_at(i), retain_from=F1, held=HOLD_ALL)
    assert prod.versions("feed_version") == [part_name(F1)]
    sidecar = prod.sidecar()
    assert sidecar["feed_version"]["f"] == F1.isoformat() and sidecar[RUN_KEY] == {"f": _at(29).isoformat()}
    assert _sink(tmp_path, _at(29), F1).read_table("src.feed_version").fetchall() == [("v1",)]


def test_a_table_never_published_is_published_by_a_skipped_run(tmp_path):
    """Holding needs a published version to keep: a table new this Run is published, skip or not."""
    prod = Producer(tmp_path)
    p = prod.handle(F1)
    p.write_table("first", p.con.sql("SELECT 1 AS x"))
    ParquetDataPlane().export(prod.con, prod.data_dir, f=F1, held=HOLD_ALL)
    assert prod.versions("first") == [part_name(F1)]


@pytest.mark.timeout(5)
def test_the_duck_publishes_a_skipped_run_holding_everything(tmp_path):
    con = ledger.connect(tmp_path / "pond.db")
    core = DuckCore("src", con, {"event": []})
    seen = {}

    def export(f, contract):
        seen[f] = core.held_for(f)
        return {}

    core.begin_run(F1, F1)
    core.ripple_completed("event", F1, export=export)
    core.begin_run(_at(1), _at(1), previous_f=F1)
    core.mark_skipped(_at(1))
    core.ripple_completed("event", _at(1), export=export)
    assert seen[F1] is not HOLD_ALL and seen[_at(1)] is HOLD_ALL
    assert core.held_for(_at(1)) is not HOLD_ALL  # only while that Run is published
