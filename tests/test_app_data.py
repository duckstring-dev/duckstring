"""Records and Media on a Catchment (plans/records-and-media.md): an App line gets its own schema, role and
Media directory when it starts, and reads other lines' Records. Needs PostgreSQL (`duckstring install postgres`)."""

from __future__ import annotations

import pytest
from test_app import _deploy, _zip  # noqa: F401  (shared App helpers)

from duckstring.app import app_config
from duckstring.install import find_postgres

pytestmark = [pytest.mark.timeout(120),
              pytest.mark.skipif(find_postgres() is None, reason="no PostgreSQL here (duckstring install postgres)")]

_APP_PY = '''import os

from fastapi import FastAPI, Request

from duckstring import app as ds

api = FastAPI()


@api.get("/healthz")
def healthz():
    return {"ok": True}


@api.post("/rate/{score}")
def rate(score: int):
    with ds.records() as con:
        con.execute("CREATE TABLE IF NOT EXISTS ratings (score int)")
        con.execute("INSERT INTO ratings VALUES (%s)", (score,))
    with ds.media().open("uploads/note.txt", "w") as f:
        f.write(f"rated {score}")
    return {"ok": True}


@api.get("/total")
def total():
    with ds.records() as con:
        own = con.execute("SELECT coalesce(sum(score), 0) FROM ratings").fetchone()[0]
    return {"total": own, "media": os.environ["DUCKSTRING_MEDIA_URL"], "files": ds.media().find("")}


@api.get("/read/{schema}")
def read(schema: str):
    with ds.records() as con:
        try:
            got = con.execute(f"SELECT sum(score) FROM {schema}.ratings").fetchone()[0]
        except Exception as e:
            return {"error": type(e).__name__}
        return {"sum": got}


@api.post("/write/{schema}")
def write(schema: str):
    with ds.records() as con:
        try:
            con.execute(f"INSERT INTO {schema}.ratings VALUES (1)")
        except Exception as e:
            return {"error": type(e).__name__}
    return {"ok": True}
'''


def _toml(name: str, extra: str = "records = true\nmedia = true\n") -> str:
    return (f'[pond]\nname = "{name}"\nversion = "1.0.0"\ntype = "app"\n\n[app]\n'
            'command = "uvicorn main:api --port $PORT --root-path $DUCKSTRING_APP_PATH"\nhealth = "/healthz"\n' + extra)


def test_records_and_media_are_declared_in_app():
    cfg = app_config({"pond": {"type": "app", "name": "feedback"}, "app": {"command": "x", "records": True}})
    assert cfg["records"] is True and cfg["media"] is False
    for bad in ({"pond": {"type": "app", "name": "Feed-Back"}, "app": {"command": "x", "records": True}},
                {"pond": {"type": "app"}, "app": {"command": "x", "media": "yes"}}):
        with pytest.raises(ValueError):
            app_config(bad)


def test_an_app_line_writes_its_records_and_media_and_reads_another_lines(catchment_client):
    client = catchment_client
    for name in ("feedback", "admin"):
        r = _deploy(client, name, {"pond.toml": _toml(name), "main.py": _APP_PY})
        assert r.status_code == 200, r.text

    assert client.post("/apps/feedback/v1/rate/4").json() == {"ok": True}
    assert client.post("/apps/feedback/v1/rate/5").json() == {"ok": True}
    got = client.get("/apps/feedback/v1/total").json()
    assert got["total"] == 9
    root = client.app.state.root
    assert got["media"] == str(root / "media" / "feedback" / "m1")
    assert (root / "media" / "feedback" / "m1" / "uploads" / "note.txt").read_text() == "rated 5"

    # Another App reads the line's Records and can't write them.
    assert client.get("/apps/admin/v1/read/feedback_v1").json() == {"sum": 9}
    assert client.post("/apps/admin/v1/write/feedback_v1").json() == {"error": "InsufficientPrivilege"}

    lines = client.app.state.driver.app_data()
    assert [(d["name"], d["major"], d["records"], d["media"], d["state"]) for d in lines] == [
        ("admin", 1, True, True, "live"), ("feedback", 1, True, True, "live")]
    status = client.get("/api/catchment/records").json()
    assert status["running"] and status["in_use"]


def test_an_app_without_records_gets_neither(catchment_client):
    client = catchment_client
    r = _deploy(client, "plain", {"pond.toml": _toml("plain", extra=""), "main.py": _APP_PY})
    assert r.status_code == 200, r.text
    assert client.get("/apps/plain/v1/healthz").json() == {"ok": True}
    assert client.app.state.driver.app_data() == []
    assert not client.app.state.driver.records.pg.running()  # nothing started PostgreSQL


def test_the_settings_routes(catchment_client):
    client = catchment_client
    assert client.get("/api/catchment/media").json() == {"target": "local", "in_use": False}
    r = client.put("/api/catchment/media", json={"target": "relative/dir"})
    assert r.status_code == 422
    r = client.put("/api/catchment/records", json={"memory": "256MB"})
    assert r.status_code == 200 and r.json()["memory"] == 256 * 10**6
    r = client.put("/api/catchment/records", json={"target": "ftp://nope"})
    assert r.status_code == 422


def test_pond_run_gives_an_app_local_records_and_media(tmp_path, monkeypatch):
    import subprocess

    from duckstring.local import app as local_app
    from duckstring.local import load_project

    (tmp_path / "pond.toml").write_text(_toml("feedback"))
    project = load_project(tmp_path)
    env = local_app.app_env(project, 8123)
    assert env["DUCKSTRING_MEDIA_URL"] == str(tmp_path / "puddles" / "app" / "media")

    seen = {}

    def fake_serve(project, cfg, port):
        url = local_app.os.environ["DUCKSTRING_RECORDS_URL"]
        seen["url"] = url
        seen["out"] = subprocess.run([str(find_postgres()[0] / "psql"), url, "-Atc", "SHOW search_path"],
                                     capture_output=True, text=True).stdout.strip()
        return 0

    monkeypatch.setattr(local_app, "_serve", fake_serve)
    assert local_app.serve_app(project, 8123) == 0
    assert seen["url"].startswith("postgresql://feedback_v1:")
    assert seen["out"] == "feedback_v1"
    assert "DUCKSTRING_RECORDS_URL" not in local_app.os.environ
    assert (tmp_path / "puddles" / "app" / "records" / "pg" / "PG_VERSION").is_file()  # kept between runs
