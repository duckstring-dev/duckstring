"""Apps (plans/apps.md): an Outlet that serves HTTP instead of running Ripples.

An App is declared in ``pond.toml`` with ``[pond] type = "app"`` and an ``[app]`` section, and deployed like
any Pond. The Catchment starts its ``command`` on the first request, forwards requests to it under
``/apps/{name}/v{major}/``, and stops it after ``idle_timeout`` without a request.

Inside the App's process, :func:`pond` gives a read-only handle on its Sources::

    from duckstring import app

    pond = app.pond()
    rel = pond.read_table("reports.monthly_summary")

The process gets ``PORT`` (where to listen), ``DUCKSTRING_APP_PATH`` (the base path it serves under),
``DUCKSTRING_APP_STATE`` (a durable directory, with ``state = "local"``) and ``DUCKSTRING_APP`` (JSON describing
the App, read by :func:`pond`). Requests arrive with the base path removed and named in ``X-Forwarded-Prefix``
(the convention ``uvicorn --root-path`` and WSGI servers follow), or, with ``strip_path = false``, with the full
path (what Streamlit's ``baseUrlPath`` and Dash's path prefixes expect).
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

ENV = "DUCKSTRING_APP"  # the JSON the runtime hands an App's process, read by pond()

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
DEFAULT_IDLE_TIMEOUT = "30m"


def _duration_s(text, label: str) -> int:
    """Seconds from ``"30m"``, ``"1h30m"`` or ``"0"``."""
    s = str(text).strip().lower()
    if s == "0":
        return 0
    parts = re.findall(r"(\d+)([smhdw])", s)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != s:
        raise ValueError(f"pond.toml {label}: invalid duration {text!r} (use e.g. 30m, 1h, 1h30m, or 0)")
    return sum(int(n) * _UNITS[u] for n, u in parts)


def app_config(info: dict, source_dir: Path | None = None) -> dict | None:
    """The validated ``[app]`` section of a parsed ``pond.toml``, or ``None`` when the Pond isn't an App.
    Raises :class:`ValueError` on a bad declaration. With ``source_dir``, also checks the declared ``static``
    directory exists."""
    pond = info.get("pond", {})
    section = info.get("app")
    if pond.get("type") != "app":
        if section is not None:
            raise ValueError('pond.toml has an [app] section but [pond] type isn\'t "app"')
        return None
    if not isinstance(section, dict) or not (section.get("command") or section.get("static")):
        raise ValueError('pond.toml: an App needs [app] command (for example '
                         '"uvicorn main:app --port $PORT --root-path $DUCKSTRING_APP_PATH"), static, or both')
    command = section.get("command")
    if command is not None and (not isinstance(command, str) or not command.strip()):
        raise ValueError("pond.toml [app] command: expected a command line")
    unknown = set(section) - {"command", "static", "idle_timeout", "state", "health", "strip_path"}
    if unknown:
        raise ValueError(f"pond.toml [app]: unknown {', '.join(sorted(unknown))}")
    static = section.get("static")
    if static is not None:
        if not isinstance(static, str) or Path(static).is_absolute() or ".." in Path(static).parts:
            raise ValueError("pond.toml [app] static: expected a directory inside the App")
        if source_dir is not None and not (Path(source_dir) / static).is_dir():
            raise ValueError(f"pond.toml [app] static: no directory {static!r} in the App")
    state = section.get("state", "local")
    if state not in ("local", "none"):
        raise ValueError(f'pond.toml [app] state: expected "local" or "none", got {state!r}')
    strip_path = section.get("strip_path", True)
    if not isinstance(strip_path, bool):
        raise ValueError("pond.toml [app] strip_path: expected true or false")
    health = section.get("health")
    if health is not None and (not isinstance(health, str) or not health.startswith("/")):
        raise ValueError('pond.toml [app] health: expected a path starting with "/"')
    return {
        "command": command,
        "static": static,
        "idle_timeout_s": _duration_s(section.get("idle_timeout", DEFAULT_IDLE_TIMEOUT), "[app] idle_timeout"),
        "state": state,
        "health": health,
        "strip_path": strip_path,
    }


def base_path(name: str, major: int) -> str:
    """The path an App's major line serves under, without a trailing slash."""
    return f"/apps/{name}/v{major}"


def app_stop_message(reason: str | None, exit_code: int | None) -> str:
    """A person-readable reason an App's instance ended, from its ``app_instance`` row."""
    code = f" (exit code {exit_code})" if exit_code is not None else ""
    if reason == "start_failed":
        return f"the App didn't become ready{code}; see `duckstring app logs`"
    if reason == "exited":
        return f"the App's process exited{code}; see `duckstring app logs`"
    return f"the App stopped: {reason}{code}"


# ─── the handle ────────────────────────────────────────────────────────────────


def _pond_class():
    from .core import Pond

    class AppPond(Pond):
        """A read-only Pond handle for an App: it reads its Sources' newest published data, and gives each
        thread its own DuckDB cursor (web servers serve requests on several threads)."""

        def __init__(self, *args, runtime: Path | None = None, **kwargs):
            self._local = threading.local()
            self._runtime = runtime
            super().__init__(*args, **kwargs)

        @property
        def con(self):
            cur = getattr(self._local, "con", None)
            if cur is None:
                cur = self._local.con = self._base_con.cursor()
            return cur

        @con.setter
        def con(self, value):
            self._base_con = value

        def _resolve_ref(self, ref: str, what: str = "table"):
            source, name = super()._resolve_ref(ref, what)
            if source is None:
                raise ValueError(f"an App reads only its Sources' {what}s: name the Source, as in 'source.{name}'")
            return source, name

        def freshness(self) -> dict:
            """The App's freshness and each Source's, as the Catchment last reported them:
            ``{"freshness": datetime | None, "sources": {name: datetime}}``. Empty outside a Catchment."""
            from datetime import datetime

            if self._runtime is None:
                return {"freshness": None, "sources": {}}
            try:
                raw = json.loads((self._runtime / "freshness.json").read_text())
            except (OSError, ValueError):
                return {"freshness": None, "sources": {}}

            def when(iso):
                return datetime.fromisoformat(iso) if iso else None

            return {"freshness": when(raw.get("freshness")),
                    "sources": {k: when(v) for k, v in raw.get("sources", {}).items()}}

        def _read_only(self, *_a, **_k):
            raise TypeError("an App can't publish tables or Objects; write to its state directory or a database")

        write_table = append_table = merge_table = apply_zset = write_object = _read_only

        def read_delta(self, ref: str):
            raise TypeError("an App reads current data; read_delta needs a Pond Run's window")

        def trickle(self, spine_ref: str, *, p: float = 0.3):
            raise TypeError("an App reads current data; the Trickle builder needs a Pond Run's window")

        def skip(self) -> None:
            raise TypeError("an App has no Pond Run to skip")

    return AppPond


_handle = None
_handle_lock = threading.Lock()


def pond():
    """The App's read-only Pond handle: :meth:`~duckstring.Pond.read_table`, ``count_table``, ``read_object``,
    ``object_path``, ``source_columns`` and ``con`` over its declared Sources, at their newest published data,
    plus ``freshness()``. One handle per process; each thread gets its own cursor."""
    global _handle
    with _handle_lock:
        if _handle is None:
            _handle = _make_handle()
        return _handle


def _make_handle():
    import duckdb

    raw = os.environ.get(ENV)
    if not raw:
        raise RuntimeError(f"{ENV} isn't set: run the App through the Catchment or `duckstring pond run`")
    cfg = json.loads(raw)
    runtime = Path(cfg["runtime"]) if cfg.get("runtime") else None
    return _pond_class()(
        cfg["name"], cfg.get("version", "0.0.0"), duckdb.connect(), cfg["root"],
        source_majors=cfg.get("source_majors") or None, data_root=cfg.get("data_root"),
        sources=list(cfg.get("sources", [])), runtime=runtime,
    )
