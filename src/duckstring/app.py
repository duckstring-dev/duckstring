"""Apps (plans/apps.md): an Outlet that serves HTTP instead of running Ripples.

An App is declared in ``pond.toml`` with ``[pond] type = "app"`` and an ``[app]`` section, and deployed like
any Pond. The Catchment starts its ``command`` on the first request, forwards requests to it under
``/apps/{name}/v{major}/``, and stops it after ``idle_timeout`` without a request.

Inside the App's process, :func:`pond` gives a read-only handle on its Sources::

    from duckstring import app

    pond = app.pond()
    rel = pond.read_table("reports.monthly_summary")

The process gets ``PORT`` (where to listen), ``DUCKSTRING_APP_PATH`` (the base path it serves under),
``DUCKSTRING_APP_STATE`` (a durable directory, with ``state = "local"``), ``DUCKSTRING_RECORDS_URL`` and
``DUCKSTRING_MEDIA_URL`` (its Records and Media, with ``records``/``media = true``; :func:`records_url`,
:func:`media`) and ``DUCKSTRING_APP`` (JSON describing the App, read by :func:`pond`). Requests arrive with the
base path removed and named in ``X-Forwarded-Prefix`` (the convention ``uvicorn --root-path`` and WSGI servers
follow), or, with ``strip_path = false``, with the full path (what Streamlit's ``baseUrlPath`` and Dash's path
prefixes expect).
"""

from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from itertools import count
from pathlib import Path

ENV = "DUCKSTRING_APP"  # the JSON the runtime hands an App's process, read by pond()
RECORDS_ENV = "DUCKSTRING_RECORDS_URL"  # the line's Records: a libpq URL signing its role in
MEDIA_ENV = "DUCKSTRING_MEDIA_URL"  # the line's Media: a local directory or an object-store URI
# An App with Records names a PostgreSQL schema and role ``{name}_v{major}``: a lower-case identifier that never
# needs quoting, well inside PostgreSQL's 63-byte limit.
RECORDS_NAME = re.compile(r"[a-z_][a-z0-9_]{0,47}")

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
                         '\'uvicorn main:app --port $PORT --root-path "$DUCKSTRING_APP_PATH"\'), static, or both')
    command = section.get("command")
    if command is not None and (not isinstance(command, str) or not command.strip()):
        raise ValueError("pond.toml [app] command: expected a command line")
    unknown = set(section) - {"command", "static", "idle_timeout", "state", "health", "strip_path", "download",
                              "records", "media", "migrate"}
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
    download = section.get("download")
    if download is not None:
        if not isinstance(download, str) or not download.strip():
            raise ValueError("pond.toml [app] download: expected a command line")
        if state == "none":
            raise ValueError('pond.toml [app] download: an App with state = "none" has no state to download')
    strip_path = section.get("strip_path", True)
    if not isinstance(strip_path, bool):
        raise ValueError("pond.toml [app] strip_path: expected true or false")
    # Records and Media (plans/records-and-media.md): the line's tables in the Catchment's PostgreSQL and its
    # files, which Ponds and other Apps can read.
    records, media = section.get("records", False), section.get("media", False)
    for key, value in (("records", records), ("media", media)):
        if not isinstance(value, bool):
            raise ValueError(f"pond.toml [app] {key}: expected true or false")
    if records:
        if pond.get("name") is not None and not RECORDS_NAME.fullmatch(str(pond["name"])):
            raise ValueError("pond.toml [app] records: an App with Records needs a name of lower-case letters, digits "
                             "and underscores, starting with a letter or underscore, at most 48 characters")
    migrate = section.get("migrate")
    if migrate is not None:
        if not isinstance(migrate, str) or not migrate.strip():
            raise ValueError("pond.toml [app] migrate: expected a command line")
        if not (records or media):
            raise ValueError("pond.toml [app] migrate: only an App with Records or Media is migrated")
    health = section.get("health")
    if health is not None and (not isinstance(health, str) or not health.startswith("/")):
        raise ValueError('pond.toml [app] health: expected a path starting with "/"')
    from .core import parse_size

    try:
        memory = parse_size(pond.get("memory"))  # the whole App's, DuckDB included
    except ValueError as exc:
        raise ValueError(f"pond.toml [pond] memory: {exc}") from exc
    return {
        "command": command,
        "static": static,
        "memory": memory,
        "idle_timeout_s": _duration_s(section.get("idle_timeout", DEFAULT_IDLE_TIMEOUT), "[app] idle_timeout"),
        "state": state,
        "health": health,
        "strip_path": strip_path,
        "download": download,
        "records": records,
        "media": media,
        "migrate": migrate,
    }


def base_path(name: str, major: int) -> str:
    """The path an App's major line serves under, without a trailing slash."""
    return f"/apps/{name}/v{major}"


def static_file(static_dir: Path, path: str) -> Path | None:
    """The file under an App's ``static`` directory that ``path`` (below the App's base path) names, its
    ``index.html`` for a directory. ``None`` when there is none, or the path leads out of the directory. Used by the
    Catchment's forwarding and by ``pond run``, so a path serves the same file in both."""
    base = Path(static_dir).resolve()
    target = (base / path).resolve()
    if target != base and base not in target.parents:
        return None
    if target.is_dir():
        target = target / "index.html"
    return target if target.is_file() else None


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
            """The App's freshness and each Source's, as the Catchment last reported them, and when each Source's
            data last changed: ``{"freshness": datetime | None, "sources": {name: datetime}, "changed": {name:
            datetime}}``. A Source's freshness moves on every Run, including Runs that changed nothing; ``changed``
            moves only when its data did. Empty outside a Catchment."""
            from datetime import datetime

            empty = {"freshness": None, "sources": {}, "changed": {}}
            if self._runtime is None:
                return empty
            try:
                raw = json.loads((self._runtime / "freshness.json").read_text())
            except (OSError, ValueError):
                return empty

            def when(iso):
                return datetime.fromisoformat(iso) if iso else None

            return {"freshness": when(raw.get("freshness")),
                    "sources": {k: when(v) for k, v in raw.get("sources", {}).items()},
                    "changed": {k: when(v) for k, v in raw.get("changed", {}).items()}}

        @contextmanager
        def hold(self):
            """Keep the Source versions current now while the block runs, for reads outside a request (a
            background thread, a cache warmed at startup). A read inside a request needs none: the Catchment
            keeps what a request in flight may read until its response ends. A no-op outside a Catchment."""
            if self._runtime is None:
                yield self
                return
            holds = self._runtime / "holds"
            holds.mkdir(parents=True, exist_ok=True)
            path = holds / f"{os.getpid()}-{threading.get_ident()}-{next(_hold_ids)}.json"
            tmp = path.with_name(path.name + ".tmp")
            try:
                # Written, then checked: a Source that published meanwhile is held at its newer version, since a
                # read in the block may resolve to it.
                for _ in range(10):
                    stamps = self._stamps()
                    tmp.write_text(json.dumps({"sources": stamps}))
                    tmp.replace(path)
                    if self._stamps() == stamps:
                        break
                yield self
            finally:
                path.unlink(missing_ok=True)

        def _stamps(self) -> dict:
            from .catchment.registry import local_publish_f

            out = {}
            for source in self._declared_sources or ():
                f = local_publish_f(self._source_data_dir(source))
                if f:
                    out[source] = f
            return out

        def _read_only(self, *_a, **_k):
            raise TypeError("an App can't publish tables or Objects; write to its state directory or a database")

        write_table = append_table = merge_table = apply_zset = write_object = _read_only

        def read_delta(self, ref: str):
            raise TypeError("an App reads current data; read_delta needs a Pond Run's window")

        def trickle(self, spine_ref: str, *, p: float = 0.3):
            raise TypeError("an App reads current data; the Trickle builder needs a Pond Run's window")

        def skip(self) -> None:
            raise TypeError("an App has no Pond Run to skip")

        def report(self, reports: dict) -> None:
            raise TypeError("an App has no Pond Run to report on")

    return AppPond


_handle = None
_handle_lock = threading.Lock()
_hold_ids = count(1)


def pond():
    """The App's read-only Pond handle: :meth:`~duckstring.Pond.read_table`, ``count_table``, ``read_object``,
    ``object_path``, ``reports`` and ``con`` over its declared Sources, at their newest published data,
    plus ``freshness()`` and ``hold()``. One handle per process; each thread gets its own cursor."""
    global _handle
    with _handle_lock:
        if _handle is None:
            _handle = _make_handle()
        return _handle


def records_url() -> str:
    """The App line's Records as a standard PostgreSQL URL: its own role and password on the Catchment's database,
    searching its own schema first, so unqualified table names are its own. Every App line's tables are readable as
    ``{name}_v{major}.{table}``; only its own are writable. Use it with any driver (psycopg, SQLAlchemy with
    ``postgresql+psycopg://``, asyncpg). Needs ``[app] records = true``."""
    url = os.environ.get(RECORDS_ENV)
    if not url:
        raise RuntimeError(f"{RECORDS_ENV} isn't set: declare [app] records = true in pond.toml")
    return url


def records(**kwargs):
    """A psycopg connection to the App line's Records (:func:`records_url`). Needs psycopg in the App's
    environment (``uv add 'psycopg[binary]'``); any other driver can use :func:`records_url` instead."""
    try:
        import psycopg
    except ImportError:
        raise RuntimeError("duckstring.app.records() needs psycopg in the App's environment: "
                           "uv add 'psycopg[binary]' (or connect to records_url() with another driver)") from None
    return psycopg.connect(records_url(), **kwargs)


def media_url() -> str:
    """Where the App line's Media lives: a local directory, or an object-store URI when the Catchment keeps Media
    there. Needs ``[app] media = true``."""
    url = os.environ.get(MEDIA_ENV)
    if not url:
        raise RuntimeError(f"{MEDIA_ENV} isn't set: declare [app] media = true in pond.toml")
    return url


def media_path() -> Path:
    """The App line's Media as a local directory (when the Catchment keeps Media on its own disk)."""
    url = media_url()
    if "://" in url:
        raise RuntimeError(f"this Catchment keeps Media in an object store ({url.split('://')[0]}://): use media()")
    return Path(url)


def media():
    """An fsspec filesystem rooted at the App line's Media, the same wherever the Catchment keeps it: paths are
    relative (``fs.open("uploads/a.png", "wb")``, ``fs.ls("uploads")``). Needs fsspec in the App's environment,
    and the object store's own package (s3fs, gcsfs, adlfs) when Media is in one."""
    try:
        import fsspec
        from fsspec.implementations.dirfs import DirFileSystem
    except ImportError:
        raise RuntimeError("duckstring.app.media() needs fsspec in the App's environment: uv add fsspec "
                           "(or use media_path() for files on the Catchment's disk)") from None
    url = media_url()
    if "://" in url:
        fs, path = fsspec.core.url_to_fs(url)
    else:
        fs, path = fsspec.filesystem("file", auto_mkdir=True), url  # parents made, as an object store needs none
    return DirFileSystem(path, fs)


def _make_handle():
    import duckdb

    raw = os.environ.get(ENV)
    if not raw:
        raise RuntimeError(f"{ENV} isn't set: run the App through the Catchment or `duckstring pond run`")
    cfg = json.loads(raw)
    runtime = Path(cfg["runtime"]) if cfg.get("runtime") else None
    con = duckdb.connect()
    if cfg.get("memory_limit"):  # the App's [pond] memory (at least 256MB), set aside while it runs
        con.execute(f"SET memory_limit = '{cfg['memory_limit']}'")
    source_dirs = None
    if cfg.get("source_dirs"):  # a local serve's Puddle working copies (duckstring.local.app)
        from .storage import get_storage

        source_dirs = {s: get_storage(uri) for s, uri in cfg["source_dirs"].items()}
    remote_sources = None
    if cfg.get("remote_sources"):  # a local serve's live Puddles, read through their Catchment's query API
        from .remote_source import HttpTransport, RemoteSource

        remote_sources = {
            s: RemoteSource(s, r["major"], HttpTransport.registered(r.get("catchment")), queries=r.get("queries"),
                            kinds={t: (m, tuple(pk)) for t, (m, pk) in (r.get("kinds") or {}).items()})
            for s, r in cfg["remote_sources"].items()}
    return _pond_class()(
        cfg["name"], cfg.get("version", "0.0.0"), con, cfg["root"],
        source_majors=cfg.get("source_majors") or None, data_root=cfg.get("data_root"),
        sources=list(cfg.get("sources", [])), runtime=runtime, source_dirs=source_dirs,
        remote_sources=remote_sources,
    )
