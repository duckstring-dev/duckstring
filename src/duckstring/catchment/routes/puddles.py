"""Puddles on a Catchment (catchment/puddles.py): deploying them, and Refreshes through them.

``GET  /api/puddles``                       the Puddles on this Catchment (read)
``POST /api/puddles``                       deploy Puddles from a Pond's bundle and its definitions (full)
``POST /api/ponds/{name}/puddles/refresh``  move the Puddles a Refresh reaches to a target and refresh at it (demand)
``POST /api/ponds/{name}/puddles/hydrate``  build them again with their definitions and run from the new baseline
                                            (full: it can store production's credentials, and resets the Ponds it
                                            reaches)
``POST /api/ponds/{name}/puddles/rewind``   back to the baseline and run from it (full: it resets them too)
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from .. import auth
from .orchestrate import _driver, _resolve

router = APIRouter()


def _point(value: str | None, label: str) -> datetime | None:
    if value is None:
        return None
    try:
        f = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{label}: not an ISO time ({value!r})") from exc
    return f if f.tzinfo is not None else f.replace(tzinfo=timezone.utc)


async def _call(fn, *args, **kwargs):
    from ...remote_source import RemoteQueryError

    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RemoteQueryError as exc:
        raise HTTPException(status_code=502, detail=_origin_error(exc)) from exc


def _origin_error(exc) -> str:
    if exc.status in (401, 403):
        return ("the Catchment the Puddle reads refused the credentials stored here: refresh with --hydrate "
                "--from CATCHMENT to store new ones")
    return f"the Catchment the Puddle reads answered {exc.status}: {exc.detail}"


@router.get("/puddles", dependencies=[auth.read])
def list_puddles(request: Request):
    return {"puddles": _driver(request).list_puddles()}


class _Origin(BaseModel):
    name: str | None = None  # the CLI's name for it, shown only
    url: str
    headers: dict[str, str] = {}


class _DeploySpec(BaseModel):
    sources: list[str] | None = None  # none: each of the Pond's Sources not yet on this Catchment
    set: str = "default"
    live: bool = False
    as_at: str | None = None
    origin: _Origin | None = None     # the Catchment to read the Sources from; none: the definitions alone
    force: bool = False               # replace Puddles already here


@router.post("/puddles", dependencies=[auth.full])
async def deploy_puddles(request: Request):
    """Deploy Puddles from the uploaded bundle (a Pond project, as ``pond deploy`` uploads it): its environment is
    built, then each Puddle's definitions run in it (:mod:`duckstring.puddle_build`), and only once every one has
    built are the Puddles made the Source lines here. 409 (``detail.replace``) for a Puddle already here, unless
    ``force``."""
    import asyncio

    from ...core import read_pond_toml
    from ...environments import EnvError, ensure_env
    from ...keys import spec_major
    from ...puddle_build import PuddleBuildError, run_build
    from ..puddles import PuddleConflict

    driver = _driver(request)
    form = await request.form()
    try:
        spec = _DeploySpec(**json.loads(form.get("spec") or "{}"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"spec: {exc}") from exc
    as_at = _point(spec.as_at, "as_at")
    if spec.live and spec.origin is None:
        raise HTTPException(status_code=422, detail="a live Puddle reads production: give --from CATCHMENT")
    archive = await form["bundle"].read()
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=driver._puddles_root()))
    try:
        code = staging / "code"
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as zf:
                zf.extractall(code)
        except zipfile.BadZipFile as exc:
            raise HTTPException(status_code=422, detail="the bundle isn't a zip archive") from exc
        info = read_pond_toml(code)
        owner = (info.get("pond") or {}).get("name")
        if not owner:
            raise HTTPException(status_code=422, detail="the bundle has no pond.toml naming its Pond")
        declared = {s: spec_major(str(v)) for s, v in (info.get("sources") or {}).items()}
        try:
            version = str((info.get("pond") or {}).get("version") or "")
            own_major = spec_major(version) if version else None
            build, skipped = driver.puddle_plan(owner, declared, spec.sources, force=spec.force, own_major=own_major)
        except PuddleConflict as exc:
            raise HTTPException(status_code=409, detail={"message": str(exc), "replace": exc.replace}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not build:
            return {"deployed": [], "skipped": skipped, "warnings": []}
        try:
            python = await asyncio.to_thread(ensure_env, Path(driver.root), code)
        except EnvError as exc:
            raise HTTPException(status_code=422, detail=f"Building the Pond's environment failed: {exc}") from exc
        origin = spec.origin.model_dump() if spec.origin is not None else None
        built, warnings = [], []
        for source, major in build:
            out = staging / "out" / source
            try:
                result = await asyncio.to_thread(run_build, python, {
                    "op": "build", "project_dir": str(code), "source": source, "set": spec.set, "live": spec.live,
                    "as_at": as_at.isoformat() if as_at else None, "out": str(out),
                    "origin": {"url": origin["url"], "headers": origin["headers"]} if origin else None})
            except PuddleBuildError as exc:
                detail = f"The Puddle of '{source}' couldn't be built: {exc}"
                raise HTTPException(status_code=422, detail=f"{detail}\n\n{exc.detail}" if exc.detail else detail) \
                    from exc
            warnings += result.get("warnings") or []
            built.append({"source": source, "major": major, "set": spec.set, "live": spec.live,
                          "as_at": as_at.isoformat() if as_at else None, "out": out, "result": result})
        try:
            deployed = await run_in_threadpool(driver.install_puddles, owner, code, built, origin)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"deployed": deployed, "skipped": skipped, "warnings": warnings}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


class _TargetBody(BaseModel):
    target: str | None = None  # next-change | next-run | most-recent
    to: str | None = None      # ISO freshness


@router.post("/ponds/{name}/puddles/refresh", dependencies=[auth.demand])
async def refresh_puddles(name: str, body: _TargetBody, request: Request, major: int | None = None,
                          version: str | None = None):
    if (body.target is None) == (body.to is None):
        raise HTTPException(status_code=422, detail="give one of target or to")
    key = _resolve(request, name, major, version)
    asked = await _call(_driver(request).refresh_puddles, key, target=body.target, to=_point(body.to, "to"))
    return {"ok": True, "requested": asked.isoformat()}


class _HydrateBody(BaseModel):
    origin: _Origin | None = None  # read this Catchment instead of the one each Puddle was built from
    as_at: str | None = None


@router.post("/ponds/{name}/puddles/hydrate", dependencies=[auth.full])
async def hydrate_puddles(name: str, body: _HydrateBody, request: Request, major: int | None = None,
                          version: str | None = None):
    key = _resolve(request, name, major, version)
    asked = await _call(_driver(request).refresh_puddles, key, hydrate=True, as_at=_point(body.as_at, "as_at"),
                        origin=body.origin.model_dump() if body.origin is not None else None)
    return {"ok": True, "requested": asked.isoformat()}


@router.post("/ponds/{name}/puddles/rewind", dependencies=[auth.full])
async def rewind_puddles(name: str, request: Request, major: int | None = None, version: str | None = None):
    key = _resolve(request, name, major, version)
    asked = await _call(_driver(request).refresh_puddles, key, rewind=True)
    return {"ok": True, "requested": asked.isoformat()}
