"""Run reports (duckstring.reports, plans/showcase-fixes.md item 14): facts a Ripple declares about its Run,
carried downstream with the data in a hidden append table, and read back with ``pond.reports()``."""

from __future__ import annotations

import io
import json
import socket
import textwrap
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import httpx
import pytest

from duckstring.reports import REPORTS_TABLE, STAGED_TABLE

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


class Catchment:
    """A Catchment root whose Ponds run one Run at a time: build, collect the reports, publish."""

    def __init__(self, root: Path):
        self.root = root

    def line(self, name: str) -> Path:
        return self.root / "ponds" / name / "m1"

    def run(self, name: str, f: datetime, build=None, *, sources=(), previous_f=None) -> dict:
        """One Run of ``name`` at ``f``; returns what ``pond.reports()`` gave inside it."""
        from duckstring.core import Pond
        from duckstring.dataplane import ParquetDataPlane
        from duckstring.reports import clear, collect

        line = self.line(name)
        line.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(line / "registry.duckdb"))
        seen: dict = {}
        try:
            kw = dict(root=self.root, source_majors={s: 1 for s in sources}, previous_f=previous_f,
                      own_data_dir=line / "data")
            pond = Pond(name, "1.0.0", con, f=f, ripple="main", **kw)
            clear(con, f, "main")
            if build is not None:
                build(pond)
            seen = pond.reports()
            collect(Pond(name, "1.0.0", con, f=f, **kw))
            ParquetDataPlane().export(con, line / "data", f=f, exact_ints=True)
        finally:
            con.close()
        return seen

    def held(self, name: str) -> list[tuple]:
        """The rows of ``name``'s published reports table, ``(pond, run_f, reports)``."""
        from duckstring.dataplane import ParquetDataPlane

        con = duckdb.connect()
        rows = con.sql(ParquetDataPlane().read_select(self.line(name) / "data", REPORTS_TABLE)).fetchall()
        return sorted((p, rf.astimezone(timezone.utc), json.loads(r)) for p, rf, r, *_ in rows)

    def app_reports(self, name: str, sources) -> dict:
        """``pond.reports()`` on a handle with no Run (an App's): everything the Sources hold."""
        from duckstring.core import Pond

        con = duckdb.connect()
        pond = Pond(name, "1.0.0", con, root=self.root, source_majors={s: 1 for s in sources})
        return pond.reports()


@pytest.fixture
def catchment(tmp_path):
    return Catchment(tmp_path / "root")


def table(name="t"):
    def build(pond):
        pond.write_table(name, pond.con.sql("SELECT 1 AS x"))
    return build


def reporting(**reports):
    def build(pond):
        pond.write_table("t", pond.con.sql("SELECT 1 AS x"))
        pond.report(reports)
    return build


def test_a_report_reaches_the_next_pond_with_its_run(catchment):
    catchment.run("a", at(0), reporting(data_as_of="17:00"))
    seen = catchment.run("b", at(1), table(), sources=["a"])
    assert seen == {"a@1": {at(0): {"data_as_of": "17:00"}}}
    assert catchment.held("b") == [("a@1", at(0), {"data_as_of": "17:00"})]


def test_reports_carry_through_a_chain_and_merge_with_the_next_ponds(catchment):
    catchment.run("a", at(0), reporting(data_as_of="17:00"))
    catchment.run("b", at(1), reporting(rows=3), sources=["a"])
    seen = catchment.run("c", at(2), table(), sources=["b"])
    assert seen == {"a@1": {at(0): {"data_as_of": "17:00"}}, "b@1": {at(1): {"rows": 3}}}


def test_a_pond_catching_up_sees_every_upstream_run_it_read(catchment):
    for i in range(3):
        catchment.run("a", at(i), reporting(n=i))
    seen = catchment.run("b", at(3), table(), sources=["a"])
    assert seen == {"a@1": {at(0): {"n": 0}, at(1): {"n": 1}, at(2): {"n": 2}}}
    # The next Run reads only what arrived since.
    catchment.run("a", at(4), reporting(n=4))
    seen = catchment.run("b", at(5), table(), sources=["a"], previous_f=at(3))
    assert seen == {"a@1": {at(4): {"n": 4}}}
    assert [r[1] for r in catchment.held("b")] == [at(0), at(1), at(2), at(4)]


def test_a_report_reached_through_two_sources_is_kept_once(catchment):
    catchment.run("a", at(0), reporting(data_as_of="17:00"))
    catchment.run("b", at(1), table(), sources=["a"])
    catchment.run("c", at(1), table(), sources=["a"])
    catchment.run("d", at(2), table(), sources=["b", "c"])
    assert catchment.held("d") == [("a@1", at(0), {"data_as_of": "17:00"})]


def test_an_overlapping_window_adds_nothing_twice(catchment):
    catchment.run("a", at(0), reporting(n=0))
    catchment.run("b", at(1), table(), sources=["a"])
    catchment.run("b", at(2), table(), sources=["a"])  # previous_f unset: reads a's reports in full again
    assert catchment.held("b") == [("a@1", at(0), {"n": 0})]


def test_an_app_reads_everything_its_sources_hold(catchment):
    for i in range(2):
        catchment.run("a", at(i), reporting(n=i))
    assert catchment.app_reports("app", ["a"]) == {"a@1": {at(0): {"n": 0}, at(1): {"n": 1}}}


def test_calls_merge_and_a_relaunched_ripple_reports_afresh(catchment):
    from duckstring.core import Pond
    from duckstring.reports import clear, staged

    line = catchment.line("a")
    line.mkdir(parents=True)
    con = duckdb.connect(str(line / "registry.duckdb"))
    one = Pond("a", "1.0.0", con, root=catchment.root, f=at(0), ripple="one")
    two = Pond("a", "1.0.0", con, root=catchment.root, f=at(0), ripple="two")
    one.report({"a": 1, "b": 1})
    one.report({"b": 2})
    two.report({"b": 3, "c": 3})  # Ripple-name order: two's keys win over one's
    assert staged(con, at(0)) == {"a": 1, "b": 3, "c": 3}
    clear(con, at(0), "two")
    assert staged(con, at(0)) == {"a": 1, "b": 2}


def test_a_replayed_export_collects_the_same_reports(catchment):
    from duckstring.core import Pond
    from duckstring.reports import collect

    catchment.run("a", at(0), reporting(n=0))
    line = catchment.line("a")
    con = duckdb.connect(str(line / "registry.duckdb"))
    try:
        collect(Pond("a", "1.0.0", con, root=catchment.root, f=at(0)))  # again, at the same f
        rows = con.execute(f"SELECT count(*) FROM {REPORTS_TABLE}").fetchall()[0][0]
    finally:
        con.close()
    assert rows == 1


def test_earlier_runs_staged_reports_are_dropped_at_export(catchment):
    catchment.run("a", at(0), reporting(n=0))
    catchment.run("a", at(1), reporting(n=1))
    con = duckdb.connect(str(catchment.line("a") / "registry.duckdb"))
    try:
        fs = [r[0].astimezone(timezone.utc) for r in con.execute(f"SELECT f FROM {STAGED_TABLE}").fetchall()]
    finally:
        con.close()
    assert fs == [at(1)]  # kept until a later Run's export, so a replay of this one still has it


def test_a_pipeline_that_never_reports_writes_nothing(catchment):
    from duckstring.trickle.io import load_sidecar

    catchment.run("a", at(0), table())
    catchment.run("b", at(1), table(), sources=["a"])
    for name in ("a", "b"):
        assert REPORTS_TABLE not in load_sidecar(catchment.line(name) / "data")
        assert not (catchment.line(name) / "data" / REPORTS_TABLE).exists()


def test_the_reports_table_is_published_but_hidden(catchment):
    from duckstring.dataplane import ParquetDataPlane
    from duckstring.schema_contract import extract_schema
    from duckstring.trickle.io import load_sidecar, sidecar_tables

    catchment.run("a", at(0), reporting(n=0))
    data = catchment.line("a") / "data"
    assert REPORTS_TABLE in sidecar_tables(load_sidecar(data))
    assert ParquetDataPlane().list_tables(data) == ["t"]
    con = duckdb.connect(str(catchment.line("a") / "registry.duckdb"))
    try:
        assert set(extract_schema(con)) == {"t"}
    finally:
        con.close()


def test_a_lost_registry_keeps_the_reports_it_published(catchment):
    from duckstring.dataplane import hydrate_registry

    catchment.run("a", at(0), reporting(n=0))
    line = catchment.line("a")
    (line / "registry.duckdb").unlink()
    con = duckdb.connect(str(line / "registry.duckdb"))
    try:
        hydrate_registry(con, line / "data")
        rows = con.execute(f"SELECT pond, reports FROM {REPORTS_TABLE}").fetchall()
    finally:
        con.close()
    assert rows == [("a@1", '{"n": 0}')]


def test_the_app_handle_reads_reports_and_cant_report(catchment, monkeypatch):
    import duckstring.app as ds_app

    catchment.run("a", at(0), reporting(n=0))
    monkeypatch.setenv(ds_app.ENV, json.dumps({"name": "dash", "major": 1, "root": str(catchment.root),
                                               "sources": ["a"], "source_majors": {"a": 1}}))
    monkeypatch.setattr(ds_app, "_handle", None)
    pond = ds_app.pond()
    assert pond.reports() == {"a@1": {at(0): {"n": 0}}}
    with pytest.raises(TypeError, match="no Pond Run"):
        pond.report({"n": 1})


@pytest.mark.parametrize("bad", [["not", "a", "dict"], {1: "x"}, {"x": object()}])
def test_a_report_that_isnt_json_is_refused(catchment, bad):
    from duckstring.core import Pond

    con = duckdb.connect()
    with pytest.raises(TypeError):
        Pond("a", "1.0.0", con, root=catchment.root, f=at(0)).report(bad)


def test_datetimes_are_reported_as_iso_strings(catchment):
    catchment.run("a", at(0), reporting(data_as_of=at(-180)))
    seen = catchment.run("b", at(1), table(), sources=["a"])
    assert seen["a@1"][at(0)] == {"data_as_of": at(-180).isoformat()}


# ─── a local run (`pond run`) carries reports as a Duck does ───────────────────


def test_a_local_run_carries_its_sources_reports(tmp_path, monkeypatch, catchment):
    from duckstring.cli.config import register_catchment, set_default_catchment
    from duckstring.local.hydrate import hydrate
    from duckstring.local.project import load_project
    from duckstring.local.runner import run_pond

    catchment.run("a", at(0), reporting(data_as_of="17:00"))
    register_catchment("prod", url="http://127.0.0.1:9", kind="local", root=str(catchment.root))
    set_default_catchment("prod")
    project = tmp_path / "b"
    (project / "src").mkdir(parents=True)
    (project / "pond.toml").write_text('[pond]\nname = "b"\nversion = "1.0.0"\n\n[sources]\na = "1.0.0"\n')
    (project / "src" / "pond.py").write_text(textwrap.dedent("""
        from duckstring import ripple

        @ripple
        def main(pond):
            seen = str(pond.reports()).replace("'", "''")
            pond.write_table("seen", pond.con.sql(f"SELECT '{seen}' AS reports"))
            pond.report({"rows": 1})
    """))
    monkeypatch.chdir(project)
    proj = load_project(project)
    hydrate(proj, live=True)
    result = run_pond(proj)
    assert result.ok, [r.error for r in result.ripples]
    con = duckdb.connect()
    from duckstring.dataplane import ParquetDataPlane

    rows = con.sql(ParquetDataPlane().read_select(proj.out_dir, REPORTS_TABLE)).fetchall()
    assert sorted(r[0] for r in rows) == ["a@1", "b@1"]
    seen = con.sql(ParquetDataPlane().read_select(proj.out_dir, "seen")).fetchall()[0][0]
    assert "17:00" in seen


# ─── end to end, through a Catchment and real Ducks ───────────────────────────


_INLET = """
from duckstring import ripple


@ripple
def load(pond):
    pond.write_table("event", pond.con.sql("SELECT 1 AS id"))
    pond.report({"data_as_of": "17:00"})


@ripple(parents=[load])
def tally(pond):
    pond.report({"rows": 1})
"""

_OUTLET = """
from duckstring import ripple


@ripple
def show(pond):
    seen = repr(sorted(pond.reports())).replace("'", "''")
    pond.write_table("seen", pond.con.sql(f"SELECT '{seen}' AS reports"))
"""


def _zip(path: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for f in sorted(path.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(path))
    return buf.getvalue()


@pytest.mark.timeout(60)
def test_reports_travel_through_real_ducks(tmp_path, monkeypatch):
    import uvicorn

    from duckstring.catchment.app import create_app
    from duckstring.dataplane import ParquetDataPlane

    projects = {
        "rin": ('[pond]\nname = "rin"\nversion = "1.0.0"\ntype = "inlet"\n', _INLET),
        "rout": ('[pond]\nname = "rout"\nversion = "1.0.0"\ntype = "outlet"\n\n[sources]\nrin = "1.0.0"\n', _OUTLET),
    }
    root = tmp_path / "root"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    monkeypatch.delenv("DUCKSTRING_DISABLE_DUCKS", raising=False)
    monkeypatch.setenv("DUCKSTRING_CATCHMENT_URL", url)
    server = uvicorn.Server(uvicorn.Config(create_app(root), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                httpx.get(f"{url}/api/health", timeout=1.0)
                break
            except Exception:
                time.sleep(0.05)
        for name, (toml, code) in projects.items():
            d = tmp_path / name
            (d / "src").mkdir(parents=True)
            (d / "pond.toml").write_text(toml)
            (d / "src" / "pond.py").write_text(code)
            kind = "inlet" if name == "rin" else "outlet"
            r = httpx.post(f"{url}/api/deploy", files={"pond": ("pond.zip", _zip(d), "application/zip")},
                           data={"name": name, "version": "1.0.0", "type": kind}, timeout=15.0)
            assert r.status_code == 200, r.text
        httpx.post(f"{url}/api/ponds/rout/refresh", timeout=5.0)

        def done():
            ponds = httpx.get(f"{url}/api/status", timeout=5.0).json()["ponds"]
            p = next((p for p in ponds if p["name"] == "rout"), None)
            assert not (p or {}).get("is_failed"), p
            return p is not None and p.get("end_f") is not None and p.get("status") == "idle"

        deadline = time.monotonic() + 45
        while not done():
            assert time.monotonic() < deadline, "rout never completed"
            time.sleep(0.25)
        data = root / "ponds" / "rout" / "m1" / "data"
        con = duckdb.connect()
        rows = con.sql(ParquetDataPlane().read_select(data, REPORTS_TABLE)).fetchall()
        assert [(r[0], json.loads(r[2])) for r in rows] == [("rin@1", {"data_as_of": "17:00", "rows": 1})]
        seen = con.sql(ParquetDataPlane().read_select(data, "seen")).fetchall()[0][0]
        assert seen == "['rin@1']"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
