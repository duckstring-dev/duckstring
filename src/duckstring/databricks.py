"""Reading Databricks data into a Pond: a Unity Catalog table (:func:`table`), or the files a Databricks job wrote
to a Volume (:func:`files`). Each returns an :class:`Input`: the data as a DuckDB relation, whether it changed
since the Ripple's previous run, and the rows added since then.

    from duckstring import databricks

    @ripple
    def orders(pond):
        src = databricks.table(pond, "main.sales.orders")
        if not src.changed:
            return pond.skip()
        pond.merge_table("orders", src.read(), pk="order_id")

A table is read from its storage with credentials Unity Catalog vends for it, so no warehouse runs (the metastore
must allow external data access, and the principal needs ``EXTERNAL USE SCHEMA`` on the schema); where Unity
Catalog refuses, it is read through a SQL warehouse. Its version comes from Unity Catalog's table info either way
(about 50 ms, no warehouse). Files are read where they are when DuckDB can reach them (a mounted Volume, a local
path, an object store), else fetched through the Files API into a local cache that keeps what it already holds.
A directory is a Delta table if it has a ``_delta_log``, else Parquet: Spark's output is read only once committed
(its ``_committed_*`` markers), and a file still uploading (empty) is left for the next run.

What a Ripple read is recorded per run (``core.INPUTS_TABLE``) and published in the run stamp, so ``changed`` and
:meth:`Input.added` compare against the Ripple's previous completed run, also after a Duck rebuilt its registry.
A Rebuild starts over: everything reads as changed and added. See plans/databricks-bigquery.md, "Reading from
Databricks".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from . import databricks_rest as dbx
from . import delta_log
from .trickle_io import first_row

log = logging.getLogger(__name__)

_FETCH_THREADS = 8
_TID = re.compile(r"-tid-(\d+)-")
_clients: dict[str, dbx.Client] = {}
_clients_lock = threading.Lock()
_cache_locks: dict[str, threading.Lock] = {}
_cache_locks_guard = threading.Lock()


class NotAppendOnly(ValueError):
    """:meth:`Input.added` found rows changed or removed since the previous run, so the input isn't append-only;
    read it whole with :meth:`Input.read` (and ``merge_table`` it) instead."""


class Input:
    """An external input as one Ripple run sees it.

    Attributes:
        ref: What was read: the table's full name, or the files' path.
        version: The input's version now: a Delta version, or a digest of the committed files. None for a
            view, which has no version (it always reads as changed).
        changed: Whether the version differs from the one the Ripple's previous completed run read (always
            true on a first run or after a Rebuild).
        via: How the data was reached, once read: ``"storage"`` (vended credentials), ``"warehouse"``,
            ``"files"`` (in place) or ``"cache"`` (fetched through the Files API).
    """

    def __init__(self, pond, ref: str, state: dict):
        self.ref = ref
        self.version = state.get("version")
        self._pond = pond
        self._state = state
        self._previous = pond._input_state(ref)
        prior = (self._previous or {}).get("version")
        self.changed = self.version is None or prior is None or prior != self.version
        self.via: str | None = None
        pond._record_input(ref, state)

    def read(self):
        """The input's current data, as a relation on the Pond's connection."""
        raise NotImplementedError

    def added(self):
        """The rows added since the Ripple's previous completed run: everything on a first run (or after a
        Rebuild), nothing when unchanged. Raises :class:`NotAppendOnly` when rows were changed or removed since."""
        raise NotImplementedError

    def _previous_version(self):
        return (self._previous or {}).get("version")


def table(pond, name: str, *, warehouse: str | None = None, env=None) -> Input:
    """A Unity Catalog table (``catalog.schema.table``) as an :class:`Input`.

    Read with credentials Unity Catalog vends for the table's storage when it allows them, else on the SQL
    warehouse ``warehouse`` (default ``DUCKSTRING_DATABRICKS_WAREHOUSE``, else ``DATABRICKS_WAREHOUSE_ID``).
    The workspace is ``DATABRICKS_HOST``, signed in as :mod:`duckstring.databricks_rest` describes. ``env``
    replaces the process environment for both."""
    env = os.environ if env is None else env
    client = _client(env)
    info = client.request("GET", f"/api/2.1/unity-catalog/tables/{quote(name, safe='.')}").json()
    props = info.get("properties") or {}
    version = props.get("delta.lastUpdateVersion") if info.get("data_source_format") == "DELTA" else None
    wh = warehouse or env.get("DUCKSTRING_DATABRICKS_WAREHOUSE") or env.get("DATABRICKS_WAREHOUSE_ID")
    return _Table(pond, info.get("full_name") or name, {"kind": "table", "version": version}, client, info, wh)


def files(pond, path: str, *, format: str | None = None) -> Input:
    """The files at ``path`` as an :class:`Input`: a Delta table, or a directory of Parquet files (Spark's output
    read once committed; ``k=v`` directories as Hive partitions). ``format`` (``"delta"`` or ``"parquet"``)
    overrides the detection. ``path`` is a ``/Volumes/...`` path, a local path or an object-store URI."""
    from .storage import get_storage

    store = get_storage(path)
    kind = format or ("delta" if delta_log.is_table(store) else "parquet")
    if kind == "delta":
        names = delta_log.log_names(store)
        version = delta_log.latest_version(names)
        if version is None:
            raise FileNotFoundError(f"{path} has a _delta_log with no commits")
        return _DeltaFiles(pond, str(path), {"kind": "delta", "version": str(version)}, store, names)
    if kind != "parquet":
        raise ValueError(f"format must be 'delta' or 'parquet', not {format!r}")
    live = _committed_parquet(store)
    if not live and not store.is_dir():
        raise FileNotFoundError(f"{path} doesn't exist")
    digest = hashlib.sha256(json.dumps(sorted(live.items())).encode()).hexdigest()[:32]
    return _ParquetFiles(pond, str(path), {"kind": "parquet", "version": digest, "files": sorted(live)}, store, live)


# ─── Unity Catalog tables ──────────────────────────────────────────────────────────────────────────────────


class _Table(Input):
    def __init__(self, pond, ref, state, client, info, warehouse):
        self._client, self._info, self._warehouse = client, info, warehouse
        self._route = None
        super().__init__(pond, ref, state)
        _clear_results(_cache_dir(pond, ref))  # an earlier run's warehouse results

    def read(self):
        route = self._reach()
        v = self.version
        if route == "storage":
            return self._scan(v)
        return self._on_warehouse(f"SELECT * FROM {self._ident()}" + (f" VERSION AS OF {int(v)}" if v else ""))

    def added(self):
        prior = self._previous_version()
        if prior is None or self.version is None:
            return self.read()
        if prior == self.version:
            return self.read().limit(0)
        if self._reach() == "storage":
            return _delta_added(self._pond, self._store, int(prior), int(self.version), self._scan)
        t, now, then = self._ident(), int(self.version), int(prior)
        gone = self._client.rows(self._client.execute(self._warehouse, (
            f"SELECT count(*) FROM (SELECT * FROM {t} VERSION AS OF {then} EXCEPT ALL "
            f"SELECT * FROM {t} VERSION AS OF {now})")))
        if int(gone[0][0]):
            raise NotAppendOnly(f"{self.ref}: {gone[0][0]} rows changed or removed between versions {then} and {now}")
        return self._on_warehouse(f"SELECT * FROM {t} VERSION AS OF {now} EXCEPT ALL SELECT * FROM {t} VERSION AS OF {then}")

    def _ident(self) -> str:
        return ".".join(dbx.ident(p) for p in self.ref.split("."))

    def _reach(self) -> str:
        """``storage`` when Unity Catalog vends credentials for the table, else ``warehouse`` (or raise)."""
        if self._route is not None:
            return self._route
        why = self._vend()
        if why is None:
            self._route = self.via = "storage"
            return self._route
        if not self._warehouse:
            raise dbx.DatabricksError(f"{self.ref} can't be read from its storage ({why}), and no SQL warehouse is "
                                      "set to read it through: pass warehouse=, or set DUCKSTRING_DATABRICKS_WAREHOUSE")
        log.info("reading %s through warehouse %s: %s", self.ref, self._warehouse, why)
        self._route = self.via = "warehouse"
        return self._route

    def _vend(self) -> str | None:
        """Vend read credentials and configure the Pond's connection with them; None, or why not."""
        from .storage import get_storage

        info, props = self._info, self._info.get("properties") or {}
        if info.get("data_source_format") != "DELTA" or not info.get("storage_location"):
            return f"it is a {info.get('table_type', 'non-Delta').lower()} with no Delta storage"
        if any(k.startswith("delta.feature.catalog") for k in props):
            return "its commits are managed by the catalog, which DuckDB's Delta reader doesn't follow"
        try:
            creds = self._client.request("POST", "/api/2.1/unity-catalog/temporary-table-credentials",
                                         json={"table_id": info["table_id"], "operation": "READ"}).json()
        except dbx.DatabricksError as exc:
            return str(exc)
        location = info["storage_location"]
        if creds.get("azure_user_delegation_sas"):
            params = {"sas_token": creds["azure_user_delegation_sas"]["sas_token"]}
        elif creds.get("aws_temp_credentials"):
            aws = creds["aws_temp_credentials"]
            params = {"key_id": aws["access_key_id"], "secret": aws["secret_access_key"], "token": aws["session_token"]}
        else:
            return "Unity Catalog vended a credential kind this reader doesn't use yet (" + \
                ", ".join(k for k in creds if k not in ("expiration_time", "url")) + ")"
        self._store = get_storage(location, params)
        _vended_secret(self._pond.con, location, params, self._client)
        return None

    def _scan(self, version, *, filename: bool = False) -> object:
        return _delta_scan(self._pond.con, self._store.uri(), version, filename=filename)

    def _on_warehouse(self, sql: str):
        st = self._client.execute(self._warehouse, sql, arrow=True)
        out = _cache_dir(self._pond, self.ref) / f"wh-{uuid.uuid4().hex[:12]}"
        self._client.save_result(st, out)
        return self._pond.con.read_parquet(str(out / "*.parquet"))


def _vended_secret(con, location: str, params: dict, client) -> None:
    """A DuckDB secret for ``location`` (scoped to it) from vended credentials."""
    from . import storage

    if "://" not in location:
        return  # a local table (tests): nothing to authenticate
    name = "duckstring_in_" + hashlib.sha256(location.encode()).hexdigest()[:16]
    scheme = location.split("://", 1)[0]
    if scheme in ("abfs", "abfss", "az"):
        extension, sql = storage.duckdb_secret_sql(location, params, name=name)
        storage.load_extension(con, extension)
    else:  # S3: the data plane's builder has no session token and one shared name, so build it here
        region = _metastore_region(client)
        bits = ["TYPE S3", f"KEY_ID {storage._q(params['key_id'])}", f"SECRET {storage._q(params['secret'])}",
                f"SESSION_TOKEN {storage._q(params['token'])}", f"SCOPE {storage._q(location.rstrip('/'))}"]
        if region:
            bits.append(f"REGION {storage._q(region)}")
        storage.load_extension(con, "httpfs")
        sql = f"CREATE OR REPLACE SECRET {name} ({', '.join(bits)})"
    try:
        con.execute(sql)
    except Exception:  # never echo a credential
        raise RuntimeError(f"failed to configure DuckDB credentials for {location}") from None


def _metastore_region(client) -> str | None:
    try:
        return client.request("GET", "/api/2.1/unity-catalog/metastore_summary").json().get("region")
    except dbx.DatabricksError:
        return None


# ─── files: Delta ──────────────────────────────────────────────────────────────────────────────────────────


class _DeltaFiles(Input):
    def __init__(self, pond, ref, state, store, names):
        self._store, self._names = store, names
        self._needed: set[str] = set()
        super().__init__(pond, ref, state)

    def read(self):
        return self._scan(int(self.version))

    def added(self):
        prior = self._previous_version()
        if prior is None:
            return self.read()
        if prior == self.version:
            return self.read().limit(0)
        log_store = self._log_store(int(prior))
        if not self._store.duckdb_readable:  # hold both versions' files before either is scanned
            self._local([int(prior), int(self.version)])
        return _delta_added(self._pond, log_store, int(prior), int(self.version), self._scan)

    def _scan(self, version, *, filename: bool = False):
        return _delta_scan(self._pond.con, self._local([version]), version, filename=filename)

    def _local(self, versions) -> str:
        """A URI DuckDB can read the table at: in place, or a local mirror holding what ``versions`` read."""
        if self._store.duckdb_readable:
            self._store.duckdb_setup(self._pond.con)
            self.via = "files"
            return self._store.uri()
        self.via = "cache"
        cache = _cache_dir(self._pond, self.ref)
        log_store = self._log_store(min(versions), versions)
        import duckdb

        con = duckdb.connect()
        want = set()
        for v in versions:
            data, dvs = delta_log.files_at(con, log_store, v)
            want |= data | dvs
        self._needed |= want
        _mirror(self._store, want, cache, keep=self._needed)
        return str(cache)

    def _log_store(self, prior: int, versions=None):
        """The log, where DuckDB can read it: in place, or the files ``versions`` need mirrored locally."""
        from .storage import LocalStorage

        if self._store.duckdb_readable:
            return self._store
        versions = versions or [prior, int(self.version)]
        cache = _cache_dir(self._pond, self.ref)
        names = delta_log.log_files_for(self._names, versions)
        need = {f"{delta_log.LOG}/{n}" for n in names}
        _mirror(self._store, need, cache, keep=self._needed, prune=False, refresh={f"{delta_log.LOG}/_last_checkpoint"})
        local = LocalStorage(cache)
        import duckdb

        side = {f"{delta_log.LOG}/{s}" for s in delta_log.sidecars(duckdb.connect(), local, names)}
        if side:
            _mirror(self._store, side, cache, keep=self._needed, prune=False)
        self._needed |= need | side  # the log files kept; a later prune drops the rest
        return local


def _delta_scan(con, uri: str, version, *, filename: bool = False):
    from .storage import load_extension

    load_extension(con, "delta")
    extra = ", filename = true" if filename else ""
    pinned = f", version => {int(version)}" if version is not None else ""
    return con.sql(f"SELECT * FROM delta_scan('{uri}'{pinned}{extra})")


def _delta_added(pond, log_store, prior: int, now: int, scan):
    """Rows in version ``now`` and not ``prior``: only the files added between them when the commits since
    only added files, else the whole difference (and :class:`NotAppendOnly` if anything left)."""
    try:
        appended = delta_log.appended_files(log_store, prior, now)
    except delta_log.DeltaLogError:
        appended = None
    if appended is not None:
        rel = scan(now, filename=True)
        if not appended:
            return rel.limit(0).select("* EXCLUDE (filename)")
        names = ", ".join("'" + n.rsplit("/", 1)[-1].replace("'", "''") + "'" for n in appended)
        return rel.filter(f"parse_filename(filename) IN ({names})").select("* EXCLUDE (filename)")
    current, previous = scan(now).sql_query(), scan(prior).sql_query()
    gone = first_row(pond.con.execute(f"SELECT count(*) FROM (({previous}) EXCEPT ALL ({current}))"))[0]
    if gone:
        raise NotAppendOnly(f"{gone} rows changed or removed between versions {prior} and {now}")
    return pond.con.sql(f"({current}) EXCEPT ALL ({previous})")


# ─── files: Parquet ────────────────────────────────────────────────────────────────────────────────────────


class _ParquetFiles(Input):
    def __init__(self, pond, ref, state, store, live: dict[str, int]):
        self._store, self._live = store, live
        super().__init__(pond, ref, state)

    def read(self):
        return self._read(sorted(self._live))

    def added(self):
        prior = (self._previous or {}).get("files")
        if prior is None:
            return self.read()
        gone = set(prior) - set(self._live)
        if gone:
            raise NotAppendOnly(f"{self.ref}: {len(gone)} files were replaced or removed since the previous run "
                                f"(for example {sorted(gone)[0]})")
        new = sorted(set(self._live) - set(prior))
        if not new:
            return self.read().limit(0)
        return self._read(new)

    def _read(self, rels: list[str]):
        if not rels:
            raise FileNotFoundError(f"{self.ref} holds no committed Parquet files yet")
        if self._store.duckdb_readable:
            self._store.duckdb_setup(self._pond.con)
            self.via = "files"
            uris = [self._store.uri(*r.split("/")).replace("''", "'") for r in rels]
        else:
            self.via = "cache"
            cache = _cache_dir(self._pond, self.ref)
            _mirror(self._store, set(self._live), cache, keep=set(self._live))
            uris = [str(cache / r) for r in rels]
        hive = any("=" in part for r in rels for part in r.split("/")[:-1])
        return self._pond.con.read_parquet(uris, union_by_name=True, hive_partitioning=hive)


def _committed_parquet(store) -> dict[str, int]:
    """The committed Parquet files under ``store``: relative path → size. A file written by a Spark job is
    committed once a ``_committed_<id>`` marker of its job lists it, and stops counting when a later marker
    removes it (an overwrite); other files count unless empty (still uploading on a Volume). Directories
    starting with ``_`` or ``.`` (``_temporary``, ``_delta_log``) are skipped."""
    out: dict[str, int] = {}
    pending = [""]
    while pending:
        rel = pending.pop()
        here = store.child(*rel.split("/")) if rel else store
        entries = _entries(here)
        markers, removed = set(), set()
        for name, is_dir, size in entries:
            if name.startswith("_committed_") and not is_dir:
                text = here.read_text(name) if size else None
                if not text:
                    continue  # still uploading: its job doesn't count yet
                body = json.loads(text)
                markers.add(name[len("_committed_"):])
                removed |= set(body.get("removed") or ())
        for name, is_dir, size in entries:
            if name.startswith(("_", ".")):
                continue
            path = f"{rel}/{name}" if rel else name
            if is_dir:
                pending.append(path)
                continue
            tid = _TID.search(name)
            if size and name not in removed and (tid is None or tid.group(1) in markers) and name.endswith(".parquet"):
                out[path] = size
    return out


def _entries(store) -> list[tuple[str, bool, int]]:
    """``(name, is_directory, size)`` for each entry directly under ``store``."""
    if store.is_local:
        root = store._abs()
        if not root.is_dir():
            return []
        return [(e.name, e.is_dir(), 0 if e.is_dir() else e.stat().st_size) for e in os.scandir(root)]
    try:
        listing = store.fs.ls(store._key(), detail=True)
    except FileNotFoundError:
        return []
    return [(str(e["name"]).rstrip("/").rsplit("/", 1)[-1], e.get("type") == "directory", int(e.get("size") or 0))
            for e in listing]


# ─── the local cache ───────────────────────────────────────────────────────────────────────────────────────


def _cache_dir(pond, ref: str) -> Path:
    """Where this Pond keeps its copies of ``ref``'s files: under ``DUCKSTRING_INPUT_CACHE`` (default the
    system's temporary directory), keyed on the Pond's registry and the input."""
    row = first_row(pond.con.execute("SELECT path FROM duckdb_databases() WHERE database_name = current_database()"))
    key = hashlib.sha256(f"{row[0] if row else ''}\0{ref}".encode()).hexdigest()[:20]
    root = Path(os.environ.get("DUCKSTRING_INPUT_CACHE") or tempfile.gettempdir()) / "duckstring-inputs"
    path = root / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def _mirror(store, want: set[str], cache: Path, *, keep: set[str], prune: bool = True, refresh=()) -> None:
    """Fetch the files ``want`` (paths relative to ``store``) into ``cache`` that it doesn't hold (names are
    written once, so a held file is current; ``refresh`` names are fetched again). With ``prune``, files not in
    ``want | keep`` are removed."""
    with _cache_lock(cache):
        missing = sorted(r for r in want if r in refresh or not (cache / r).is_file())

        def fetch(rel: str) -> None:
            dest = cache / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:8]}.part")
            store.fs.get_file(store._key(*rel.split("/")), str(tmp))
            os.replace(tmp, dest)

        if missing:
            with ThreadPoolExecutor(max_workers=min(_FETCH_THREADS, len(missing))) as pool:
                list(pool.map(fetch, missing))
        if prune:
            held = want | keep
            for path in list(cache.rglob("*")):
                rel = path.relative_to(cache).as_posix()
                if path.is_file() and not rel.startswith(("wh-", ".")) and rel not in held:
                    path.unlink()


def _clear_results(cache: Path) -> None:
    """Drop the warehouse results (``wh-*``) an earlier run of an input left."""
    with _cache_lock(cache):
        for path in cache.glob("wh-*"):
            shutil.rmtree(path, ignore_errors=True)


def _cache_lock(cache: Path) -> threading.Lock:
    with _cache_locks_guard:
        return _cache_locks.setdefault(str(cache), threading.Lock())


def _client(env) -> dbx.Client:
    host = env.get("DATABRICKS_HOST")
    if not host:
        raise dbx.DatabricksError("reading from Databricks needs DATABRICKS_HOST (a Databricks App sets it)")
    with _clients_lock:
        key = dbx.normalise_host(host)
        if key not in _clients:
            _clients[key] = dbx.Client(host, env)
        return _clients[key]
