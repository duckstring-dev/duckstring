"""Records and Media on a Catchment (plans/records-and-media.md): an App line gets its own schema, role and
Media directory when it starts, and reads other lines' Records. Needs PostgreSQL (`duckstring install postgres`)."""

from __future__ import annotations

import pytest
from test_app import _deploy, _zip  # noqa: F401  (shared App helpers)
from test_runtime import runtime  # noqa: F401  (the live-Duck Catchment fixture)

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


# ─── Ponds reading Records and Media ──────────────────────────────────────────────


def test_a_pond_reads_records_and_media_with_what_changed_since_its_last_run(tmp_path):
    from datetime import datetime, timedelta, timezone

    import duckdb
    import psycopg

    from duckstring.app_data import set_locations
    from duckstring.catchment.db import connect, migrate
    from duckstring.catchment.media import Media
    from duckstring.catchment.records import Records
    from duckstring.core import Pond

    db = connect(tmp_path / "duck.db")
    migrate(db)
    records, media = Records(tmp_path, db), Media(tmp_path, db)
    try:
        records.provision("feedback", 1)
        with psycopg.connect(records.line_url("feedback", 1)) as con:
            con.execute("CREATE TABLE ratings (id int PRIMARY KEY, score int)")
            con.execute("INSERT INTO ratings VALUES (1, 5), (2, 3)")
        media.ensure_line("feedback", 1)
        import io
        media.write("feedback", 1, "uploads/a.txt", io.BytesIO(b"first"))
        set_locations(records.reader_url(), str(tmp_path / "media"))

        registry = duckdb.connect(str(tmp_path / "registry.duckdb"))
        t0 = datetime(2026, 11, 24, tzinfo=timezone.utc)

        def run(f, previous_f):
            return Pond("reader", "1.0.0", registry.cursor(), tmp_path, f=f, previous_f=previous_f)

        from duckstring.engine.core import NEVER

        pond = run(t0, NEVER)
        assert pond.read_records("feedback@1.ratings").aggregate("sum(score)").fetchall()[0][0] == 8
        first = pond.media("feedback@1", "uploads")
        assert [e["path"] for e in first.added()] == ["uploads/a.txt"] and first.changed
        assert first.open("uploads/a.txt").read() == b"first"
        with pytest.raises(TypeError):
            first.open("uploads/a.txt", "wb")

        media.write("feedback", 1, "uploads/b.txt", io.BytesIO(b"second"))
        second = run(t0 + timedelta(hours=1), t0).media("feedback@1", "uploads")
        assert [e["path"] for e in second.added()] == ["uploads/b.txt"]
        assert second.removed() == []
        media.delete("feedback", 1, "uploads/a.txt")
        third = run(t0 + timedelta(hours=2), t0 + timedelta(hours=1)).media("feedback@1", "uploads")
        assert third.added() == [] and third.removed() == ["uploads/a.txt"]
        unchanged = run(t0 + timedelta(hours=3), t0 + timedelta(hours=2)).media("feedback@1", "uploads")
        assert not unchanged.changed

        with pytest.raises(ValueError):
            pond.read_records("feedback.ratings")  # the major is always named
    finally:
        set_locations(None, None)
        records.stop()


_LOOP_APP_PY = '''from fastapi import FastAPI

from duckstring import app as ds

api = FastAPI()
pond = ds.pond()


@api.get("/healthz")
def healthz():
    return {"ok": True}


@api.post("/rate/{score}")
def rate(score: int):
    with ds.records() as con:
        con.execute("CREATE TABLE IF NOT EXISTS ratings (id serial PRIMARY KEY, score int)")
        con.execute("INSERT INTO ratings (score) VALUES (%s)", (score,))
    return {"ok": True}


@api.get("/summary")
def summary():
    try:
        rows = pond.read_table("tally.summary").fetchall()
    except Exception as e:
        return {"error": str(e)}
    return {"n": rows[0][0], "total": rows[0][1]}
'''

_TALLY_PY = '''from duckstring import ripple


@ripple
def summary(pond):
    rel = pond.read_records("feedback@1.ratings").aggregate("count(*) AS n, sum(score) AS total")
    pond.write_table("summary", rel)
'''


@pytest.mark.timeout(120)
def test_an_app_writes_records_a_pond_reads_them_and_the_app_reads_its_output(runtime):  # noqa: F811
    import httpx
    from test_runtime import _pond_status, _wait

    url, root = runtime

    def deploy(name, files, kind="pond"):
        r = httpx.post(f"{url}/api/deploy", files={"pond": ("pond.zip", _zip(files), "application/zip")},
                       data={"name": name, "version": "1.0.0", "type": kind}, timeout=30.0)
        assert r.status_code == 200, r.text

    app_toml = _toml("feedback", extra="records = true\n").replace('type = "app"\n',
                                                                      'type = "app"\n\n[sources]\ntally = "1.0.0"\n')
    deploy("tally", {"pond.toml": '[pond]\nname = "tally"\nversion = "1.0.0"\n', "src/pond.py": _TALLY_PY})
    deploy("feedback", {"pond.toml": app_toml, "main.py": _LOOP_APP_PY})

    for score in (4, 5, 2):
        assert httpx.post(f"{url}/apps/feedback/v1/rate/{score}", timeout=60.0).json() == {"ok": True}
    httpx.post(f"{url}/api/ponds/tally/refresh", timeout=5.0)
    assert _wait(lambda: (_pond_status(url, "tally") or {}).get("runs_completed", 0) >= 1, timeout=60), \
        _pond_status(url, "tally")
    assert _wait(lambda: httpx.get(f"{url}/apps/feedback/v1/summary", timeout=30.0).json().get("n") == 3,
                 timeout=30), httpx.get(f"{url}/apps/feedback/v1/summary", timeout=30.0).json()
    assert httpx.get(f"{url}/apps/feedback/v1/summary", timeout=30.0).json() == {"n": 3, "total": 11}


def test_the_viewer_lists_lines_queries_records_and_manages_media(catchment_client):
    client = catchment_client
    r = _deploy(client, "feedback", {"pond.toml": _toml("feedback"), "main.py": _APP_PY})
    assert r.status_code == 200, r.text
    assert client.post("/apps/feedback/v1/rate/4").json() == {"ok": True}

    listed = client.get("/api/app-data").json()
    (line,) = listed["lines"]
    assert (line["name"], line["major"], line["schema"], line["state"]) == ("feedback", 1, "feedback_v1", "live")
    assert [t["table"] for t in line["tables"]] == ["ratings"]
    assert line["usage"] == {"files": 1, "bytes": len("rated 4")}

    # Records are the catalog `records`, read-only, beside Pond tables.
    got = client.post("/api/serve/query", json={"sql": "SELECT sum(score) AS s FROM records.feedback_v1.ratings"})
    assert got.status_code == 200, got.text
    assert got.json()["columns"] == ["s"] and got.json()["rows"] == [[4]]
    refused = client.post("/api/serve/query", json={"sql": "INSERT INTO records.feedback_v1.ratings VALUES (1)"})
    assert refused.status_code >= 400

    # Media: list, download, upload (a multipart form, as the viewer sends), delete.
    assert [f["path"] for f in client.get("/api/app-data/feedback/1/media").json()["files"]] == ["uploads/note.txt"]
    down = client.get("/api/app-data/feedback/1/media/file", params={"path": "uploads/note.txt"})
    assert down.content == b"rated 4" and "note.txt" in down.headers["content-disposition"]
    up = client.post("/api/app-data/feedback/1/media/file", params={"path": "docs/guide.pdf"},
                     files={"file": ("guide.pdf", b"%PDF-1.7 tiny", "application/pdf")})
    assert up.json() == {"path": "docs/guide.pdf", "size": 13}
    assert client.get("/apps/feedback/v1/total").json()["files"] == ["docs/guide.pdf", "uploads/note.txt"]
    raw = client.post("/api/app-data/feedback/1/media/file", params={"path": "raw.bin"}, content=b"\x00\x01")
    assert raw.json()["size"] == 2
    assert client.delete("/api/app-data/feedback/1/media/file", params={"path": "raw.bin"}).status_code == 200
    assert client.delete("/api/app-data/feedback/1/media/file", params={"path": "raw.bin"}).status_code == 404
    bad = client.post("/api/app-data/feedback/1/media/file", params={"path": "../escape"}, content=b"x")
    assert bad.status_code == 422
    assert client.get("/api/app-data/nothing/1/media").status_code == 404


def test_media_uploads_need_manage_access(tmp_path):
    from fastapi.testclient import TestClient

    from duckstring.catchment.app import create_app
    from duckstring.catchment.auth import generate

    app = create_app(tmp_path)
    with TestClient(app) as client:
        keys = generate(app.state.driver.db)
        full = {"Authorization": f"Bearer {keys['full']}"}
        read = {"Authorization": f"Bearer {keys['read']}"}
        r = _deploy(client, "feedback", {"pond.toml": _toml("feedback", extra="media = true\n"), "main.py": _APP_PY},
                    headers=full)
        assert r.status_code == 200, r.text
        client.app.state.driver.note_app_data("feedback", 1, media=True)
        client.app.state.driver.media.ensure_line("feedback", 1)
        assert client.get("/api/app-data/feedback/1/media", headers=read).status_code == 200
        assert client.post("/api/app-data/feedback/1/media/file", params={"path": "a.txt"}, content=b"x",
                           headers=read).status_code == 403
        assert client.post("/api/app-data/feedback/1/media/file", params={"path": "a.txt"}, content=b"x",
                           headers=full).status_code == 200


# ─── Majors: trial, live, retired (app_lifecycle.py) ─────────────────────────────

_MIGRATE_PY = '''import os

import psycopg

with psycopg.connect(os.environ["DUCKSTRING_RECORDS_URL"]) as con:
    con.execute("ALTER TABLE ratings ADD COLUMN IF NOT EXISTS note text DEFAULT 'migrated'")
with open(os.path.join(os.environ["DUCKSTRING_MEDIA_URL"], "MIGRATED"), "w") as f:
    f.write(os.environ["DUCKSTRING_PREVIOUS_MAJOR"])
'''

_V2_PY = _APP_PY + '''

@api.get("/notes")
def notes():
    with ds.records() as con:
        return {"notes": [r[0] for r in con.execute("SELECT note FROM ratings ORDER BY score").fetchall()]}
'''


def _v2_toml() -> str:
    return _toml("feedback", extra='records = true\nmedia = true\nmigrate = "python migrate.py"\n').replace(
        'version = "1.0.0"', 'version = "2.0.0"')


def test_a_new_major_is_a_trial_until_promoted_then_the_old_one_retires(catchment_client):
    client = catchment_client
    driver = client.app.state.driver
    r = _deploy(client, "feedback", {"pond.toml": _toml("feedback"), "main.py": _APP_PY})
    assert r.status_code == 200, r.text
    client.post("/apps/feedback/v1/rate/4")
    r = _deploy(client, "feedback", {"pond.toml": _v2_toml(), "main.py": _V2_PY, "migrate.py": _MIGRATE_PY},
                version="2.0.0")
    assert r.status_code == 200, r.text

    # The bare address stays on the live line; the trial starts with a migrated copy.
    assert client.get("/apps/feedback/", follow_redirects=False).headers["location"].startswith("/apps/feedback/v1/")
    assert client.get("/apps/feedback/v2/notes").json() == {"notes": ["migrated"]}
    assert (client.app.state.root / "media" / "feedback" / "m2" / "MIGRATED").read_text() == "1"
    assert driver.lifecycle.state("feedback", 2) == "trial"
    client.post("/apps/feedback/v2/rate/1")  # written to the trial: discarded by the next migration

    # The live line keeps taking writes; the promotion carries them over and migrates them.
    client.post("/apps/feedback/v1/rate/5")
    got = client.post("/api/ponds/feedback/serve/promote", json={"major": 2})
    assert got.status_code == 200, got.text
    assert got.json()["migrated"] and got.json()["retired"] == 1
    assert client.get("/apps/feedback/v2/total").json()["total"] == 9  # 4 + 5; the trial's 1 is gone
    assert client.get("/apps/feedback/v2/notes").json() == {"notes": ["migrated", "migrated"]}

    # The retired line redirects to the live one, never starts, and its role can't sign in.
    moved = client.get("/apps/feedback/v1/total", follow_redirects=False)
    assert moved.status_code == 307 and moved.headers["location"] == "/apps/feedback/v2/total"
    assert driver.lifecycle.state("feedback", 1) == "retired"
    assert "not permitted to log in" in subprocess_psql(driver.records.line_url("feedback", 1))
    assert client.post("/api/app-data/feedback/1/media/file", params={"path": "x"}, content=b"x").status_code == 409
    assert client.get("/apps/feedback/", follow_redirects=False).headers["location"].startswith("/apps/feedback/v2/")

    # Purge: a live line can't be, a retired one can.
    identity = client.get("/api/catchment/identity").json()
    name = identity["name"] or identity["id"]
    assert client.post("/api/app-data/feedback/2/purge", json={"confirm": name}).status_code == 422
    assert client.post("/api/app-data/feedback/1/purge", json={"confirm": "wrong"}).status_code == 422
    assert client.post("/api/app-data/feedback/1/purge", json={"confirm": name}).status_code == 200
    assert [d["major"] for d in driver.app_data()] == [2]
    assert not (client.app.state.root / "media" / "feedback" / "m1").exists()
    assert driver.records.psql("SELECT count(*) FROM pg_roles WHERE rolname = 'feedback_v1'").strip() == "0"


def subprocess_psql(url: str) -> str:
    import subprocess

    return subprocess.run([str(find_postgres()[0] / "psql"), url, "-Atc", "SELECT 1"],
                          capture_output=True, text=True).stderr


def test_a_failed_migration_leaves_the_live_line_as_it_was(catchment_client):
    client = catchment_client
    driver = client.app.state.driver
    _deploy(client, "feedback", {"pond.toml": _toml("feedback"), "main.py": _APP_PY})
    client.post("/apps/feedback/v1/rate/4")
    broken = _v2_toml().replace("python migrate.py", "python -c 'raise SystemExit(3)'")
    _deploy(client, "feedback", {"pond.toml": broken, "main.py": _V2_PY}, version="2.0.0")
    got = client.post("/api/ponds/feedback/serve/promote", json={"major": 2})
    assert got.status_code == 422 and "exited with code 3" in got.json()["detail"]
    assert driver.lifecycle.state("feedback", 1) == "live"
    assert client.get("/apps/feedback/v1/total").json()["total"] == 4  # signs in and serves again
    assert client.post("/apps/feedback/v1/rate/1").json() == {"ok": True}


def test_a_trial_is_for_manage_access_only(tmp_path):
    from fastapi.testclient import TestClient

    from duckstring.catchment.app import create_app
    from duckstring.catchment.auth import generate

    app = create_app(tmp_path)
    with TestClient(app) as client:
        keys = generate(app.state.driver.db)
        full = {"Authorization": f"Bearer {keys['full']}"}
        read = {"Authorization": f"Bearer {keys['read']}"}
        _deploy(client, "feedback", {"pond.toml": _toml("feedback"), "main.py": _APP_PY}, headers=full)
        assert client.post("/apps/feedback/v1/rate/3", headers=read).status_code == 200
        assert client.get("/apps/feedback/v1/total", headers=read).status_code == 200
        _deploy(client, "feedback", {"pond.toml": _v2_toml(), "main.py": _V2_PY, "migrate.py": _MIGRATE_PY},
                version="2.0.0", headers=full)
        assert client.get("/apps/feedback/v2/notes", headers=full).status_code == 200  # starts the trial
        assert client.get("/apps/feedback/v2/notes", headers=read).status_code == 403
        assert client.get("/apps/feedback/v1/total", headers=read).status_code == 200


def test_a_pond_declares_its_external_sources_for_the_canvas(catchment_client):
    client = catchment_client
    toml = ('[pond]\nname = "tally"\nversion = "1.0.0"\n'
            'external = ["app:feedback@1", "GDELT events", "GDELT events"]\n')
    r = _deploy(client, "tally", {"pond.toml": toml, "src/pond.py": _TALLY_PY})
    assert r.status_code == 200, r.text
    tally = next(p for p in client.get("/api/status").json()["ponds"] if p["name"] == "tally")
    assert tally["external"] == ["app:feedback@1", "GDELT events"]
    bad = toml.replace("app:feedback@1", "app:Feedback")
    assert _deploy(client, "tally", {"pond.toml": bad, "src/pond.py": _TALLY_PY}, version="1.0.1").status_code == 422

    r = _deploy(client, "feedback", {"pond.toml": _toml("feedback"), "main.py": _APP_PY})
    assert r.status_code == 200, r.text
    app = lambda: next(p for p in client.get("/api/status").json()["ponds"] if p["name"] == "feedback")["app"]  # noqa: E731
    assert (app()["records"], app()["media"], app()["data_state"]) == (True, True, None)
    client.get("/apps/feedback/v1/healthz")
    assert app()["data_state"] == "live"
