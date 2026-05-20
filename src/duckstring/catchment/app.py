import os
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

app = FastAPI()

_NEXT_DEV_URL = os.getenv("NEXT_DEV_URL")

if _NEXT_DEV_URL:
    import httpx

    _HOP_BY_HOP = frozenset(["transfer-encoding", "connection", "keep-alive", "upgrade"])
    _proxy_client = httpx.AsyncClient(base_url=_NEXT_DEV_URL, follow_redirects=True)

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
    async def _proxy_frontend(request: Request, path: str):
        url = httpx.URL(path=f"/{path}", query=request.url.query.encode())
        req_headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
        resp = await _proxy_client.request(request.method, url, headers=req_headers, content=await request.body())
        resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}
        return Response(content=resp.content, status_code=resp.status_code, headers=resp_headers)

else:
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")