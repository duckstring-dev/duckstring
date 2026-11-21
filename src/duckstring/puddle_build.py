"""Building a Puddle on a Catchment (catchment/puddles.py): its definitions run in the environment of the Pond that
deployed it, in a subprocess, as ``python -m duckstring.puddle_build IN OUT``. The Catchment never imports Pond code
itself (as for discovery, :mod:`duckstring.discover`).

``IN`` is a JSON spec:

- ``{"op": "build", "project_dir", "source", "set", "live", "as_at", "origin": {url, headers} | null, "out"}``: the
  Puddle's baseline, as ``pond hydrate`` builds one (:mod:`duckstring.local.hydrate`), written into ``out``. A live
  Puddle writes nothing and returns its definitions compiled to queries over the Source.
- ``{"op": "step", "project_dir", "source", "set", "out", "f", "previous_f", "step", "retain_from"}``: its
  ``@puddle.step`` definitions at ``f``, written into ``out`` over what it holds.

``OUT``: ``{"ok": true, ...}`` (a build adds ``state``, ``queries``, ``kinds`` and ``warnings``), or ``{"ok": false,
"error", "traceback"}``. Production's credentials travel in ``IN``, a file only the Catchment's user can read.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TIMEOUT_S = 1800


class PuddleBuildError(RuntimeError):
    """A Puddle's definitions failed, or its environment couldn't run them. ``detail`` holds the traceback."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


# ─── the Catchment side ────────────────────────────────────────────────────────


def run_build(python: str | Path, spec: dict, timeout: float | None = None) -> dict:
    """Run ``spec`` (see the module docstring) with the interpreter ``python``; raises :class:`PuddleBuildError`."""
    timeout = timeout or float(os.environ.get("DUCKSTRING_PUDDLE_BUILD_TIMEOUT", DEFAULT_TIMEOUT_S))
    with tempfile.TemporaryDirectory(prefix="duckstring-puddle-") as tmp:
        inp, out = Path(tmp) / "in.json", Path(tmp) / "out.json"
        inp.touch(mode=0o600)
        inp.write_text(json.dumps(spec))
        try:
            proc = subprocess.run([str(python), "-m", "duckstring.puddle_build", str(inp), str(out)],
                                  cwd=spec["project_dir"], capture_output=True, text=True, timeout=timeout,
                                  env={**os.environ, "PYTHONUNBUFFERED": "1", "DUCKSTRING_IN_POND_ENV": "1"})
        except subprocess.TimeoutExpired:
            raise PuddleBuildError(f"the Puddle's definitions took longer than {timeout:.0f}s "
                                   "(set DUCKSTRING_PUDDLE_BUILD_TIMEOUT to allow longer)") from None
        if not out.exists():
            raise PuddleBuildError("the Puddle's environment couldn't run its definitions",
                                   (proc.stderr or proc.stdout).strip()[-4000:])
        result = json.loads(out.read_text())
    if not result.get("ok"):
        raise PuddleBuildError(result.get("error", "the Puddle's build failed"), result.get("traceback", ""))
    return result


# ─── the subprocess side ───────────────────────────────────────────────────────


class _NoOrigin:
    """The transport of a Puddle deployed without ``--from``: its definitions write their own data."""

    def _refuse(self, *_args, **_kwargs):
        from .local.sources import SourceUnreachable

        raise SourceUnreachable("this Puddle reads no Catchment: deploy it with --from to read its Source")

    query = get = _refuse


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _failed(run) -> dict | None:
    bad = next((r for r in run.results if r.status == "failed"), None)
    if bad is None:
        return None
    return {"ok": False, "error": f"{bad.target} ({bad.name}): {bad.error}", "traceback": bad.traceback or ""}


def _build(spec: dict) -> dict:
    from .local import load_project
    from .local.hydrate import _Hydration, collect_definitions, select_definitions
    from .remote_source import HttpTransport

    project = load_project(Path(spec["project_dir"]))
    source, set_name = spec["source"], spec["set"]
    origin = spec.get("origin")
    transport = HttpTransport(origin["url"], origin.get("headers")) if origin else _NoOrigin()
    as_at = _dt(spec.get("as_at"))
    if origin and not spec.get("live") and as_at is None:
        as_at = datetime.now(timezone.utc)  # copied from production as of now
    definitions = collect_definitions(project)
    defs = select_definitions(definitions, source, set_name)
    steps = select_definitions(definitions, source, set_name, step=True)
    if not defs and not origin:
        return {"ok": False, "error": f"no Puddle definition for '{source}' in the set '{set_name}': define one, "
                                      "or deploy it with --from to read it from production"}
    run = _Hydration(project, None, live=bool(spec.get("live")), as_at=as_at, transport=transport)
    state = run.hydrate_source(source, defs, steps, set_name, False, path=Path(spec["out"]))
    failed = _failed(run)
    if failed is not None:
        return failed
    if state is None:
        return {"ok": False, "error": "; ".join(run.warnings) or f"'{source}' couldn't be built"}
    queries = kinds = None
    if state.mode == "live":
        remote = run.compile(source, defs, datetime.fromisoformat(state.f))
        queries = remote.queries
        kinds = {t: [m, list(pk)] for t, (m, pk) in remote.kinds.items()}
    notes = [r.note for r in run.results if r.note]
    return {"ok": True, "state": asdict(state), "queries": queries, "kinds": kinds, "warnings": run.warnings,
            "notes": notes}


def _step(spec: dict) -> dict:
    from .local import load_project
    from .local.hydrate import _Hydration, collect_definitions, select_definitions

    project = load_project(Path(spec["project_dir"]))
    steps = select_definitions(collect_definitions(project), spec["source"], spec["set"], step=True)
    run = _Hydration(project, None, live=False, as_at=None)
    f = datetime.fromisoformat(spec["f"])
    session = run.evaluate(spec["source"], steps, Path(spec["out"]), f, previous_f=_dt(spec.get("previous_f")),
                           read_at=f, step=int(spec.get("step") or 0), retain_from=_dt(spec.get("retain_from")),
                           record=False)
    if session is None:
        return _failed(run) or {"ok": False, "error": "the step failed"}
    return {"ok": True}


def run(spec: dict) -> dict:
    return _build(spec) if spec.get("op", "build") == "build" else _step(spec)


def main(argv: list[str]) -> int:
    inp, out = Path(argv[0]), Path(argv[1])
    try:
        result = run(json.loads(inp.read_text()))
    except Exception as exc:  # noqa: BLE001 - reported to the Catchment
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
    out.write_text(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
