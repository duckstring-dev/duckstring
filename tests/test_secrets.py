"""The write-only Catchment secret store: the store, the full-gated API (write-only — no read-back),
${secret:NAME} resolution at egress time, and archive exclusion (secrets never travel in a bundle)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from duckstring.catchment.app import create_app
from duckstring.catchment.secrets import SecretStore
from duckstring.egress import credentials

pytestmark = pytest.mark.timeout(10)


@pytest.fixture(autouse=True)
def _reset_secret_provider():
    """create_app injects a process-wide provider; restore it around each test so they don't leak."""
    saved = credentials._secret_provider
    yield
    credentials.set_secret_provider(saved)


# ─── The store ────────────────────────────────────────────────────────────────


def test_store_set_get_names_remove(tmp_path):
    s = SecretStore(tmp_path)
    s.set("PGPASS", "hunter2")
    assert s.get("PGPASS") == "hunter2"
    assert [n["name"] for n in s.names()] == ["PGPASS"]
    assert s.names()[0]["set_at"]  # a timestamp, never the value
    assert "value" not in s.names()[0]
    s.set("AWS_KEY", "AKIA")  # overwrite-or-add; sorted
    assert [n["name"] for n in s.names()] == ["AWS_KEY", "PGPASS"]
    assert s.remove("PGPASS") is True and s.get("PGPASS") is None
    assert s.remove("PGPASS") is False


def test_store_file_is_private_and_rejects_bad_names(tmp_path):
    s = SecretStore(tmp_path)
    s.set("OK_1", "v")
    assert (s.path.stat().st_mode & 0o777) == 0o600
    for bad in ("has space", "1leading", "dash-no", "${env:x}", ""):
        with pytest.raises(ValueError, match="invalid secret name"):
            s.set(bad, "v")


# ─── Resolution ───────────────────────────────────────────────────────────────


def test_secret_resolves_with_provider():
    store = {"PGPASS": "shh"}
    out = credentials.resolve("postgres://u:${secret:PGPASS}@h/db", secret=store.get)
    assert out == "postgres://u:shh@h/db"


def test_unset_secret_surfaces_clearly():
    with pytest.raises(credentials.CredentialError, match="NOPE"):
        credentials.resolve("${secret:NOPE}", secret={}.get)  # provider present, no such key


def test_no_provider_means_unset():
    credentials.set_secret_provider(None)
    with pytest.raises(credentials.CredentialError, match="not set"):
        credentials.resolve("${secret:X}")  # no store attached → not set (never a crash / value leak)


# ─── API (write-only, full-gated) ─────────────────────────────────────────────


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as c:  # open mode → full access
        yield c


def test_api_set_list_remove_never_returns_value(client):
    assert client.post("/api/secrets", json={"name": "PGPASS", "value": "hunter2"}).status_code == 200
    listed = client.get("/api/secrets").json()["secrets"]
    assert [s["name"] for s in listed] == ["PGPASS"]
    # The value is nowhere in the API surface — write-only.
    assert "hunter2" not in client.get("/api/secrets").text
    assert client.delete("/api/secrets/PGPASS").status_code == 200
    assert client.get("/api/secrets").json()["secrets"] == []
    assert client.delete("/api/secrets/PGPASS").status_code == 404


def test_api_set_rejects_bad_name(client):
    assert client.post("/api/secrets", json={"name": "bad name", "value": "v"}).status_code == 422


def test_api_secrets_are_full_gated(tmp_path):
    from duckstring.catchment import auth

    app = create_app(tmp_path)
    keys = auth.generate(app.state.db)
    with TestClient(app) as c:
        for level in ("read", "demand"):
            h = {"Authorization": f"Bearer {keys[level]}"}
            assert c.get("/api/secrets", headers=h).status_code == 403
            assert c.post("/api/secrets", headers=h, json={"name": "X", "value": "v"}).status_code == 403
        full = {"Authorization": f"Bearer {keys['full']}"}
        assert c.post("/api/secrets", headers=full, json={"name": "X", "value": "v"}).status_code == 200


def test_set_secret_resolves_in_a_destination_e2e(client):
    # Set a secret via the API, then resolve a destination that references it (the egress-time path).
    client.post("/api/secrets", json={"name": "AWS_SECRET", "value": "topsecret"})
    out = credentials.resolve("s3://b/p?secret=${secret:AWS_SECRET}",
                              secret=client.app.state.secret_store.get)
    assert out == "s3://b/p?secret=topsecret"


# ─── Archive exclusion (secrets never travel in a `catchment download` bundle) ──


def test_secrets_excluded_from_archive(tmp_path):
    from duckstring.catchment.routes.catchment import _root_files

    app = create_app(tmp_path)
    app.state.secret_store.set("PGPASS", "hunter2")
    assert (tmp_path / "secrets.json").exists()
    names = [arc for _, arc in _root_files(tmp_path)]
    assert "secrets.json" not in names  # excluded from the archive walk
