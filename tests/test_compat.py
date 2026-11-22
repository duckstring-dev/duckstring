"""``duckstring pond check`` (compat.py): a static scan of a Pond's code for APIs a Duckstring release removed, with
their replacements, run locally, at deploy and by the Catchment over what is deployed, so a Pond that will break on
a new version is named before it runs (the attention showcase's six Ponds, 2026-11-21)."""

from __future__ import annotations

import textwrap

import pytest

from duckstring.cli import app
from duckstring.compat import check_project, fix_project

pytestmark = pytest.mark.timeout(30)

_CODE = """
    from duckstring import ripple

    @ripple
    def load(pond):
        rows = []
        rows.append(1)  # a list's append: never flagged
        pond.append_table("event", pond.con.sql("SELECT 1 AS id"), pk="id", fail_on_conflict=False)
        pond.append_table("strict", pond.con.sql("SELECT 1 AS id"), pk="id",
                          fail_on_conflict=True)
        pond.trickle("src.t").append("out", pk="id", fail_on_conflict=False)
        flag = False
        pond.append_table("dynamic", pond.con.sql("SELECT 1 AS id"), fail_on_conflict=flag)
"""


def _pond(path, code=_CODE):
    (path / "src").mkdir(parents=True, exist_ok=True)
    (path / "pond.toml").write_text('[pond]\nname = "p"\nversion = "1.0.0"\n')
    (path / "src" / "pond.py").write_text(textwrap.dedent(code))
    return path


def test_each_removed_use_is_named_with_its_replacement(tmp_path):
    findings = check_project(_pond(tmp_path))
    assert [(f.path, f.line) for f in findings] == [("src/pond.py", n) for n in (8, 10, 11, 13)]
    assert 'on_conflict="skip_check"' in findings[0].message  # append_table: False
    assert 'on_conflict="fail"' in findings[1].message
    assert 'on_conflict="keep_prior"' in findings[2].message  # the builder's .append(): False
    assert findings[3].fix is None and "on_conflict" in findings[3].message  # not a literal: named, not fixed
    assert all("0.9.0" in f.message for f in findings)


def test_fix_rewrites_the_literal_uses_only(tmp_path):
    root = _pond(tmp_path)
    assert fix_project(root) == 3
    text = (root / "src" / "pond.py").read_text()
    assert 'pk="id", on_conflict="skip_check")' in text
    assert 'on_conflict="fail")' in text and 'pk="id", on_conflict="keep_prior")' in text
    assert [f.line for f in check_project(root)] == [13]  # the one it can't decide


def test_files_a_deploy_leaves_out_are_not_checked(tmp_path):
    root = _pond(tmp_path)
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / ".venv" / "lib" / "x.py").write_text("pond.append_table('t', r, fail_on_conflict=False)\n")
    (root / "src" / "broken.py").write_text("def (:\n")  # a file that doesn't parse is skipped, not fatal
    assert {f.path for f in check_project(root)} == {"src/pond.py"}


def test_the_cli_reports_and_fixes(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(_pond(tmp_path))
    result = runner.invoke(app, ["pond", "check"])
    assert result.exit_code == 1 and "src/pond.py:8" in result.output and "--fix" in result.output
    result = runner.invoke(app, ["pond", "check", "--fix"])
    assert result.exit_code == 1 and "Fixed 3" in result.output and "src/pond.py:13" in result.output
    (tmp_path / "src" / "pond.py").write_text("from duckstring import ripple\n")
    result = runner.invoke(app, ["pond", "check"])
    assert result.exit_code == 0 and "No removed APIs" in result.output


def test_deploy_and_status_name_a_pond_that_uses_a_removed_api(tmp_path):
    import io
    import zipfile

    from fastapi.testclient import TestClient

    from duckstring.catchment.app import create_app

    project = _pond(tmp_path / "project")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for f in project.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(project))
    root = tmp_path / "root"
    root.mkdir()
    with TestClient(create_app(root)) as client:
        r = client.post("/api/deploy", files={"pond": ("pond.zip", buf.getvalue(), "application/zip")},
                        data={"name": "p", "version": "1.0.0"})
        assert r.status_code == 200, r.text
        assert [c["line"] for c in r.json()["compat"]] == [8, 10, 11, 13]
        pond = next(x for x in client.get("/api/status").json()["ponds"] if x["name"] == "p")
        assert [c["line"] for c in pond["compat"]] == [8, 10, 11, 13]
    # A restarted Catchment (a new Duckstring, say) scans what is deployed.
    with TestClient(create_app(root)) as client:
        pond = next(x for x in client.get("/api/status").json()["ponds"] if x["name"] == "p")
        assert len(pond["compat"]) == 4


def test_a_table_check_against_duckdb_tables_is_named(tmp_path):
    """Since 0.9 an append table's name in a Pond's working database is a view, as a merge table's was: a check for
    it in duckdb_tables() misses it (six attention Ponds decided where to resume that way, and reloaded)."""
    code = """
        from duckstring import ripple

        @ripple
        def load(pond):
            n = pond.con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name = 'hour_stat'").fetchall()
            m = pond.con.execute(f"SELECT count(*) FROM information_schema.tables WHERE table_name = '{n}'").fetchall()
    """
    found = check_project(_pond(tmp_path, code))
    assert [(f.line, "information_schema.tables" in f.message) for f in found] == [(6, True)]
    assert fix_project(tmp_path) == 0  # named, not rewritten: the replacement query differs
