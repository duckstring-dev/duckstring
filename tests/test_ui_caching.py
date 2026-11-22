"""The web UI tells browsers how to cache it: without a Cache-Control header a phone's browser kept the old UI
after the Catchment's was rebuilt. The page revalidates on every load; the content-hashed build assets are kept."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from duckstring.catchment.app import _UIFiles


def test_the_page_revalidates_and_hashed_assets_are_kept(tmp_path):
    (tmp_path / "_next" / "static" / "chunks").mkdir(parents=True)
    (tmp_path / "index.html").write_text("<html>ui</html>")
    (tmp_path / "__next._full.txt").write_text("data")
    (tmp_path / "_next" / "static" / "chunks" / "a1b2.js").write_text("js")
    app = FastAPI()
    app.mount("/", _UIFiles(directory=tmp_path, html=True), name="frontend")
    client = TestClient(app)
    for path in ("/", "/index.html", "/__next._full.txt"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", path
    r = client.get("/_next/static/chunks/a1b2.js")
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    etag = client.get("/").headers["etag"]
    assert client.get("/", headers={"if-none-match": etag}).status_code == 304  # a reload is a cheap check
