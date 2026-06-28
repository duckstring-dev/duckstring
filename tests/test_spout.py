"""Spouts — a Pond's egress bindings. Destination/credential validation, the driver CRUD, persistence
across a restart, and the CLI/HTTP surface. Execution (the egress worker + drivers) lands separately;
this is the construct + config, mirroring the window/trigger tests."""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from duckstring.catchment.db import connect, migrate
from duckstring.catchment.driver import Driver
from duckstring.catchment.launcher import NoopLauncher
from duckstring.catchment.routes.deploy import _register
from duckstring.cli import app as cli_app
from duckstring.egress import destination as dest

pytestmark = pytest.mark.timeout(5)

_CFG = {"sources": {}, "immediate_retries": 0, "source_retries": 0, "kind": "outlet"}


def _driver(tmp_path):
    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "sales", "1.0.0", "outlet", "ponds/sales/1.0.0", _CFG, [{"func": "f", "name": "agg", "parents": []}])
    return Driver(db, tmp_path, "http://x", NoopLauncher())


# ─── Destination / mode validation ────────────────────────────────────────────


def test_parse_destination_known_schemes():
    assert dest.parse_destination("file:///tmp/out").scheme == "file"
    assert dest.parse_destination("s3://bucket/prefix").scheme == "s3"
    assert dest.parse_destination("postgres://u@h/db").transactional is True
    assert dest.parse_destination("file:///x").transactional is False


def test_parse_destination_rejects_unknown_scheme():
    with pytest.raises(dest.DestinationError, match="unsupported destination scheme"):
        dest.parse_destination("ftp://host/x")


def test_parse_destination_rejects_empty_and_schemeless():
    with pytest.raises(dest.DestinationError):
        dest.parse_destination("")
    with pytest.raises(dest.DestinationError, match="no scheme"):
        dest.parse_destination("just-a-path")


def test_parse_destination_validates_env_reference_syntax():
    # A well-formed reference is fine and left intact (resolved only at egress time).
    d = dest.parse_destination("postgres://u:${env:PGPASS}@h/db")
    assert "${env:PGPASS}" in d.raw
    # An empty reference is malformed.
    with pytest.raises(dest.DestinationError):
        dest.parse_destination("postgres://u:${env:}@h/db")


def test_validate_mode():
    for m in ("auto", "full", "append"):
        assert dest.validate_mode(m) == m
    with pytest.raises(dest.DestinationError, match="unknown mode"):
        dest.validate_mode("sync")


# ─── Driver CRUD ───────────────────────────────────────────────────────────────


def test_driver_spout_add_list_remove(tmp_path):
    d = _driver(tmp_path)
    name = d.add_spout("sales@1", None, "revenue", "s3://bucket/sales", "auto")
    assert name == "revenue"  # default name = the table
    spouts = d.list_spouts("sales@1")
    assert len(spouts) == 1
    s = spouts[0]
    assert (s["name"], s["table"], s["destination"], s["mode"], s["schedule"]) == \
        ("revenue", "revenue", "s3://bucket/sales", "auto", "on-run")
    assert s["watermark"] is None and s["is_failed"] is False and s["failures"] == 0
    assert d.remove_spout("sales@1", "revenue") is True
    assert d.list_spouts("sales@1") == []
    assert d.remove_spout("sales@1", "revenue") is False  # already gone


def test_driver_spout_default_name_for_all_tables_and_collision(tmp_path):
    d = _driver(tmp_path)
    assert d.add_spout("sales@1", None, None, "file:///a", "full") == "file"  # all-tables → scheme
    assert d.add_spout("sales@1", None, None, "file:///b", "full") == "file-2"  # collision → suffix
    assert {s["name"] for s in d.list_spouts("sales@1")} == {"file", "file-2"}


def test_driver_spout_duplicate_explicit_name(tmp_path):
    d = _driver(tmp_path)
    d.add_spout("sales@1", "lake", None, "s3://bucket/a", "auto")
    with pytest.raises(ValueError, match="already exists"):
        d.add_spout("sales@1", "lake", None, "s3://bucket/b", "auto")


def test_driver_spout_rejects_bad_destination_and_mode(tmp_path):
    d = _driver(tmp_path)
    with pytest.raises(ValueError, match="unsupported destination scheme"):
        d.add_spout("sales@1", None, None, "ftp://x/y", "auto")
    with pytest.raises(ValueError, match="unknown mode"):
        d.add_spout("sales@1", None, None, "s3://b/p", "sync")


def test_spouts_persist_across_restart(tmp_path):
    d = _driver(tmp_path)
    d.add_spout("sales@1", "lake", None, "s3://bucket/sales", "auto")
    d.db.close()
    # A fresh Driver over the same root reads the persisted Spout — survives a Catchment restart.
    db2 = connect(tmp_path / "duck.db")
    migrate(db2)
    d2 = Driver(db2, tmp_path, "http://x", NoopLauncher())
    assert [s["name"] for s in d2.list_spouts("sales@1")] == ["lake"]


# ─── HTTP + CLI ────────────────────────────────────────────────────────────────


def _deploy_outlet(url: str) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pond.toml", '[pond]\nname = "sales"\nversion = "1.0.0"\ntype = "outlet"\n')
        zf.writestr(
            "src/pond.py",
            "from duckstring import ripple\n\n@ripple\ndef agg(pond):\n"
            "    pond.write_table('revenue', pond.con.sql('SELECT 1 AS id'))\n",
        )
    r = httpx.post(
        f"{url}/api/deploy",
        files={"pond": ("pond.zip", buf.getvalue(), "application/zip")},
        data={"name": "sales", "version": "1.0.0", "type": "outlet"}, timeout=15.0,
    )
    assert r.status_code == 200, r.text


@pytest.mark.timeout(30)
def test_cli_spout_roundtrip(runner, live_catchment):
    _deploy_outlet(live_catchment)  # registers 'dev' as the default catchment

    res = runner.invoke(cli_app, ["spout", "add", "sales", "--to", "s3://bucket/sales", "--table", "revenue"])
    assert res.exit_code == 0, res.output
    assert "added" in res.output

    res = runner.invoke(cli_app, ["spout", "ls", "sales"])
    assert res.exit_code == 0, res.output
    assert "revenue" in res.output and "s3://bucket/sales" in res.output

    res = runner.invoke(cli_app, ["spout", "rm", "sales", "revenue"])
    assert res.exit_code == 0, res.output

    res = runner.invoke(cli_app, ["spout", "ls", "sales"])
    assert "No spouts" in res.output


def test_cli_spout_add_rejects_table_and_all(runner):
    res = runner.invoke(cli_app, ["spout", "add", "sales", "--to", "s3://b/p", "--table", "x", "--all"])
    assert res.exit_code != 0
    assert "mutually exclusive" in res.output


def test_http_spout_bad_destination_is_422(live_catchment):
    _deploy_outlet(live_catchment)
    r = httpx.post(f"{live_catchment}/api/ponds/sales/spouts", json={"destination": "ftp://x/y"}, timeout=10.0)
    assert r.status_code == 422
    assert "unsupported destination scheme" in r.json()["detail"]
