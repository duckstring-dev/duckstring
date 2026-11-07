"""A Duck reaching a private Catchment on Cloud Run with its service account's identity token (duck/id_token)."""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest

from duckstring.duck.id_token import HEADER, IdTokenAuth, audience_for, catchment_auth

pytestmark = pytest.mark.timeout(5)


def _jwt(exp: float) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{claims}.s"


def test_the_audience_is_a_cloud_run_origin_or_set_explicitly(monkeypatch):
    monkeypatch.delenv("DUCKSTRING_CATCHMENT_AUDIENCE", raising=False)
    assert audience_for("https://ds-catchment-123.australia-southeast1.run.app/x") == \
        "https://ds-catchment-123.australia-southeast1.run.app"
    assert audience_for("http://10.0.0.5:7474") is None and catchment_auth("http://localhost:7474") is None
    monkeypatch.setenv("DUCKSTRING_CATCHMENT_AUDIENCE", "https://ducks.example.com")
    assert audience_for("https://ducks.example.com") == "https://ducks.example.com"


def test_the_token_is_cached_and_refetched_near_expiry():
    fetched = []

    def fetch(audience):
        fetched.append(audience)
        return _jwt(time.time() + (3600 if len(fetched) == 1 else 7200))

    auth = IdTokenAuth("https://c.run.app", fetch=fetch)
    seen = []
    transport = httpx.MockTransport(lambda req: seen.append(req.headers.get(HEADER)) or httpx.Response(200))
    with httpx.Client(auth=auth, transport=transport, headers={"X-Duck-Token": "t"}) as client:
        client.get("https://c.run.app/api/duck/p/1/jobs")
        client.get("https://c.run.app/api/duck/p/1/jobs")
        auth._expires = time.time() + 60  # under five minutes left
        client.get("https://c.run.app/api/duck/p/1/jobs")
    assert fetched == ["https://c.run.app"] * 2 and seen[0] == seen[1] != seen[2]
    assert seen[0].startswith("Bearer h.")


def test_no_credential_sends_no_header_and_does_not_raise():
    auth = IdTokenAuth("https://c.run.app", fetch=lambda a: (_ for _ in ()).throw(RuntimeError("no metadata server")))
    req = next(auth.auth_flow(httpx.Request("GET", "https://c.run.app/x")))
    assert HEADER not in req.headers
