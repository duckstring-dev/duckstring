"""Moving Records and Media (catchment/moves.py): the copy, the window, the switch and the old store deleted, or on a
failure, nothing changed. Records against real PostgreSQL servers (skipped without one); Media against moto's S3."""

from __future__ import annotations

import os
import subprocess

import pytest
from test_app import _deploy
from test_app_data import _APP_PY, _toml

from duckstring.install import find_postgres

pytestmark = [pytest.mark.timeout(180),
              pytest.mark.skipif(find_postgres() is None, reason="no PostgreSQL here (duckstring install postgres)")]


@pytest.fixture
def external(tmp_path):
    """Another PostgreSQL server, standing in for an external one (RDS, Lakebase): a superuser and a database."""
    from duckstring.catchment.records import ManagedPostgres, _free_port

    pg = ManagedPostgres(tmp_path / "external-pg")
    pg.initdb("outside-pw")
    port = _free_port()
    pg.start(port, "127.0.0.1", 128 * 2**20)
    psql = str(pg.bin_dir / "psql")
    env = {**os.environ, "PGPASSWORD": "outside-pw"}

    def sql(query: str, db: str = "apps") -> str:
        out = subprocess.run([psql, "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-h", "127.0.0.1", "-p", str(port),
                              "-U", "duckstring", "-d", db, "-c", query], capture_output=True, text=True, env=env)
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    sql("CREATE DATABASE apps", db="postgres")
    yield f"postgres://duckstring:outside-pw@127.0.0.1:{port}/apps", sql
    pg.stop()


def _confirm(client) -> str:
    ident = client.get("/api/catchment/identity").json()
    return ident["name"] or ident["id"]


def _move(client, kind: str, target: str, **extra) -> dict:
    r = client.post(f"/api/catchment/{kind}/move", json={"target": target, "confirm": _confirm(client), **extra})
    assert r.status_code == 200, r.text
    return client.app.state.driver.moves.wait(120)


def _seed(client):
    r = _deploy(client, "feedback", {"pond.toml": _toml("feedback"), "main.py": _APP_PY})
    assert r.status_code == 200, r.text
    client.post("/apps/feedback/v1/rate/4")
    client.post("/apps/feedback/v1/rate/5")
    records = client.app.state.driver.records
    records.spout_url("catalog_v1")  # a Spout's schema moves too
    records.psql("SET ROLE duckstring_spouts; CREATE TABLE catalog_v1.prices (id int);"
                 "INSERT INTO catalog_v1.prices VALUES (1), (2), (3);")


def test_records_move_to_an_external_postgres_and_back(catchment_client, external):
    client = catchment_client
    uri, outside = external
    root = client.app.state.root
    records = client.app.state.driver.records
    _seed(client)

    done = _move(client, "records", uri)
    assert done["state"] == "done", done
    assert records.target == uri and not (root / "records" / "pg").exists()  # the old cluster is gone
    assert outside("SELECT count(*) FROM feedback_v1.ratings") == "2"
    assert outside("SELECT count(*) FROM catalog_v1.prices") == "3"
    assert client.get("/apps/feedback/v1/total").json()["total"] == 9  # the App, on the new store
    assert client.post("/apps/feedback/v1/rate/1").json() == {"ok": True}

    done = _move(client, "records", "managed")
    assert done["state"] == "done", done
    assert records.managed and (root / "records" / "pg" / "PG_VERSION").is_file()
    assert client.get("/apps/feedback/v1/total").json()["total"] == 10
    # This Catchment's schemas and roles are gone from the external server; the server itself is untouched.
    assert outside("SELECT count(*) FROM pg_namespace WHERE nspname IN ('feedback_v1', 'catalog_v1')") == "0"
    assert outside("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'feedback_%' OR rolname LIKE 'duckstring_%'") == "0"
    assert records.psql("SELECT count(*) FROM catalog_v1.prices").strip() == "3"


def test_a_managed_postgres_moves_through_a_new_cluster(catchment_client):
    client = catchment_client
    root = client.app.state.root
    driver = client.app.state.driver
    _seed(client)
    major = int((root / "records" / "pg" / "PG_VERSION").read_text())
    driver.moves._move_records("managed", major)  # the version move's path, at the same major
    assert sorted(p.name for p in (root / "records").iterdir() if p.name.startswith("pg")) == ["pg"]
    assert client.get("/apps/feedback/v1/total").json()["total"] == 9
    assert driver.records.psql("SELECT count(*) FROM catalog_v1.prices").strip() == "3"


def test_a_failed_records_move_leaves_everything_as_it_was(catchment_client, external, monkeypatch):
    from duckstring.catchment.moves import Moves

    client = catchment_client
    uri, outside = external
    records = client.app.state.driver.records
    _seed(client)
    # The server's own port differs between the two: the verification sees a difference.
    monkeypatch.setattr(Moves, "_counts_sql", staticmethod(lambda *a: "SELECT inet_server_port()"))
    done = _move(client, "records", uri)
    assert done["state"] == "failed" and "differ" in done["error"]
    assert records.managed
    assert client.get("/apps/feedback/v1/total").json()["total"] == 9  # signed in again, on the old store
    assert client.post("/apps/feedback/v1/rate/1").json() == {"ok": True}
    assert outside("SELECT count(*) FROM pg_namespace WHERE nspname = 'feedback_v1'") == "0"  # undone


def test_a_move_is_refused_without_reason(catchment_client, external):
    client = catchment_client
    uri, outside = external
    _seed(client)
    r = client.post("/api/catchment/records/move", json={"target": uri, "confirm": "wrong"})
    assert r.status_code == 422
    r = client.post("/api/catchment/records/move", json={"target": "managed", "confirm": _confirm(client)})
    assert r.status_code == 422 and "already there" in r.json()["detail"]
    outside("CREATE SCHEMA feedback_v1")
    done = _move(client, "records", uri)
    assert done["state"] == "failed" and "already holds" in done["error"]
    assert client.get("/apps/feedback/v1/total").json()["total"] == 9


# ─── Media ───────────────────────────────────────────────────────────────────────


def test_media_moves_to_an_object_store_and_back(catchment_client, request):
    pytest.importorskip("moto.server")
    request.getfixturevalue("s3_env")
    client = catchment_client
    root = client.app.state.root
    _seed(client)
    target = "s3://duckstring-test/media-move"

    done = _move(client, "media", target)
    assert done["state"] == "done", done
    assert client.get("/api/catchment/media").json()["target"] == target
    assert not (root / "media" / "feedback").exists()
    files = client.get("/api/app-data/feedback/1/media").json()["files"]
    assert [f["path"] for f in files] == ["uploads/note.txt"]
    got = client.get("/api/app-data/feedback/1/media/file", params={"path": "uploads/note.txt"})
    assert got.content == b"rated 5"

    done = _move(client, "media", "local")
    assert done["state"] == "done", done
    assert (root / "media" / "feedback" / "m1" / "uploads" / "note.txt").read_text() == "rated 5"
    assert client.app.state.driver.media.target == "local"


from test_object_store import s3_env, s3_server  # noqa: E402,F401  (the S3 fixtures)


def _older_and_newer() -> tuple | None:
    found = [find_postgres(v) for v in range(30, 13, -1)]
    found = [f for f in found if f]
    return (found[-1], found[0]) if len(found) >= 2 else None


@pytest.mark.skipif(_older_and_newer() is None, reason="needs two PostgreSQL majors installed")
def test_a_managed_postgres_moves_to_a_newer_major(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from duckstring.catchment.app import create_app

    (old_bin, old_major), (_, new_major) = _older_and_newer()
    monkeypatch.setenv("DUCKSTRING_PG_BIN", str(old_bin))  # the cluster is made with the older major
    app = create_app(tmp_path)
    with TestClient(app) as client:
        _seed(client)
        root = client.app.state.root
        assert (root / "records" / "pg" / "PG_VERSION").read_text().strip() == str(old_major)
        monkeypatch.delenv("DUCKSTRING_PG_BIN")
        done = _move(client, "records", "managed", postgres_version=new_major)
        assert done["state"] == "done", done
        assert (root / "records" / "pg" / "PG_VERSION").read_text().strip() == str(new_major)
        assert client.get("/apps/feedback/v1/total").json()["total"] == 9
        assert client.post("/apps/feedback/v1/rate/1").json() == {"ok": True}
        records = client.app.state.driver.records
        assert records.psql("SHOW server_version_num").strip().startswith(str(new_major))
        assert records.psql("SELECT count(*) FROM catalog_v1.prices").strip() == "3"
        assert sorted(p.name for p in (root / "records").iterdir() if p.name.startswith("pg")) == ["pg"]
