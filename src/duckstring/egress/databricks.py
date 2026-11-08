"""Databricks egress driver: a Pond's output delivered as Unity Catalog tables (plans/databricks-bigquery.md, part 4).

The destination names the workspace, the target schema, a SQL warehouse and a Volume to stage through::

    databricks://adb-123.4.azuredatabricks.net/main.sales?warehouse=1a2b3c&volume=/Volumes/main/sales/staging
    databricks:///main.sales?warehouse=1a2b3c&volume=/Volumes/main/sales/staging   (host from DATABRICKS_HOST)

Credentials: ``token`` (a personal access token), or ``client_id`` and ``client_secret`` (a Databricks service
principal), as query parameters holding ``${secret:}``/``${env:}`` references; else the Catchment's own
``DATABRICKS_TOKEN`` / ``DATABRICKS_CLIENT_ID`` and ``DATABRICKS_CLIENT_SECRET`` (what a Databricks App is
given), else on Azure an Entra token (:mod:`duckstring.databricks_rest`).

It needs no external location or storage credential: each delivery is written as Parquet on the Catchment,
uploaded to the Volume through the Files API, and applied by the warehouse in one statement reading it with
``read_files``, after which the staged file is removed. Every source works:

- a **merge Trickle**'s window is one ``MERGE`` by its key (changed rows replaced, removed keys deleted), so a
  replay of the same window gives the same table;
- an **append Trickle**'s window ``(w, f]`` is ``INSERT INTO ... REPLACE WHERE _duckstring_f > w``: the destination
  keeps each delivery's freshness (an ISO string) in a ``_duckstring_f`` column, so a replay replaces every row
  delivered after the recorded watermark ``w`` rather than adding them twice;
- a plain table is replaced whole (``INSERT OVERWRITE``) when its version advances.

A Delta statement commits one table, so the watermark (``_duckstring_egress`` in the target schema) is written
after the data: a crash between the two replays the window, which each form above makes harmless. A source's new
column becomes ``ALTER TABLE ... ADD COLUMNS``, typed as the warehouse reads the staged Parquet.

Each statement costs the warehouse's round trip (0.4 to 0.9 s, measured) and each commit 1.5 to 3 s, so a run is
kept to the fewest (``batch``): the watermarks of every table are read in one query and written in one ``MERGE``
when the run ends; a target known (per process) to exist with the staged columns isn't checked again; and the
run's tables are delivered in parallel (``parallel_tables``).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import parse_qsl, urlsplit

from .. import databricks_rest as dbx
from . import credentials
from .base import Capabilities
from .destination import Destination, DestinationError

WATERMARK = "_duckstring_egress"
STAMP = "_duckstring_f"  # an append table's delivery freshness, kept in the destination
_OP = "__ds_op"
_CRED_KEYS = {"token": "DATABRICKS_TOKEN", "client_id": "DATABRICKS_CLIENT_ID",
              "client_secret": "DATABRICKS_CLIENT_SECRET"}
_STAGE_FILE_BYTES = 1 << 30
# What this process knows exists, so a run doesn't check it again: the watermark tables, and each target table's
# columns (lower-cased), keyed by (host, quoted target). Dropped for a target the warehouse no longer finds.
_KNOWN_LOCK = threading.Lock()
_KNOWN_WATERMARKS: set[tuple[str, str]] = set()
_KNOWN_COLUMNS: dict[tuple[str, str], frozenset[str]] = {}


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


class DatabricksEgressDriver:
    SCHEMES = ("databricks",)

    parallel_tables = True  # a run's tables are delivered concurrently (each its own statements)

    def __init__(self, dest: Destination):
        self.dest = dest
        self._client: dbx.Client | None = None
        self._config: dict | None = None
        self.transport = None  # an httpx transport, for tests
        self._lock = threading.Lock()
        self._batch: dict | None = None  # within batch(): {"seen": {table: f}, "pending": {table: f}}
        self._previous: dict[str, datetime | None] = {}  # the watermark each table's delivery started from

    def capabilities(self) -> Capabilities:
        return Capabilities(supports_delta=True, supports_delete=True, transactional=True, requires_pk=False)

    # ─── the destination ─────────────────────────────────────────────────────

    def config(self) -> dict:
        """``{host, catalog, schema, warehouse, volume, env}`` from the destination, credentials resolved."""
        if self._config is None:
            raw = credentials.resolve(self.dest.raw) if self.dest.scheme is None else self.dest.raw
            parts = urlsplit(raw)
            options = {k.lower(): v for k, v in parse_qsl(parts.query, keep_blank_values=True)}  # before resolving
            target = parts.path.strip("/")
            if target.count(".") != 1:
                raise DestinationError("a databricks destination names its schema as catalog.schema, as in "
                                       "databricks://HOST/main.sales?warehouse=ID&volume=/Volumes/main/sales/staging")
            catalog, schema = target.split(".")
            env = {k: v for k, v in os.environ.items() if k.startswith("DATABRICKS_") or k.startswith("AZURE_")}
            for key, name in _CRED_KEYS.items():
                if options.get(key):
                    env[name] = credentials.resolve(options[key])
            host = credentials.resolve(parts.netloc) if parts.netloc else env.get("DATABRICKS_HOST")
            warehouse = credentials.resolve(options.get("warehouse", "")) or env.get("DATABRICKS_WAREHOUSE_ID")
            volume = credentials.resolve(options.get("volume", "")).rstrip("/")
            missing = [n for n, v in (("a host", host), ("warehouse=", warehouse), ("volume=", volume)) if not v]
            if missing:
                raise DestinationError(f"the databricks destination needs {', '.join(missing)}")
            if not volume.startswith("/Volumes/"):
                raise DestinationError("volume= is a Unity Catalog Volume path, /Volumes/{catalog}/{schema}/{volume}")
            self._config = {"host": host, "catalog": catalog, "schema": schema, "warehouse": warehouse,
                            "volume": volume, "env": env}
        return self._config

    def client(self) -> dbx.Client:
        if self._client is None:
            cfg = self.config()
            self._client = dbx.Client(cfg["host"], cfg["env"], transport=self.transport)
        return self._client

    def _sql(self, statement: str) -> dict:
        cfg = self.config()
        return self.client().execute(cfg["warehouse"], statement, timeout_s=600)

    def _target(self, table: str) -> str:
        cfg = self.config()
        return f"{dbx.ident(cfg['catalog'])}.{dbx.ident(cfg['schema'])}.{dbx.ident(table)}"

    # ─── the driver protocol ─────────────────────────────────────────────────

    def ensure(self, *, table: str, schema: dict | None, pk: list[str] | None) -> None:
        pass  # created from the staged data on first delivery (_ensure_table)

    def test_connection(self, con) -> None:
        """Run ``SELECT 1`` on the warehouse, and write and remove a probe in the Volume. Nothing else is created."""
        self._sql("SELECT 1")
        probe = f"{self.config()['volume']}/.duckstring-probe-{uuid.uuid4().hex[:8]}"
        tmp = tempfile.NamedTemporaryFile(delete=False)
        try:
            tmp.write(b"duckstring")
            tmp.close()
            self.client().upload(tmp.name, probe)
            self.client().request("DELETE", f"/api/2.0/fs/files{dbx._path(probe)}", ok=(200, 204, 404))
        finally:
            os.unlink(tmp.name)

    @contextmanager
    def batch(self, con, *, pond: str, major: int, f: datetime):
        """One run's deliveries: every table's watermark read in one query on entry, and the watermarks of the
        tables delivered written in one ``MERGE`` on exit, also when a later table failed (the ones written stay
        written). A crash before that replays their windows, which every form of delivery makes harmless."""
        wm = self._watermark_table()
        rows = self.client().rows(self._sql(f"SELECT table_name, f FROM {wm}"))
        self._batch = {"seen": {r[0]: datetime.fromisoformat(r[1]) for r in rows if r and r[0] and r[1]},
                       "pending": {}}
        try:
            yield
        finally:
            pending, self._batch = self._batch["pending"], None
            if pending:
                self._write_watermarks(pending)

    def watermark(self, con, table: str) -> datetime | None:
        """The freshness the destination has fully applied for ``table``."""
        if self._batch is not None:
            got = self._batch["seen"].get(table)
        else:
            st = self._sql(f"SELECT f FROM {self._watermark_table()} WHERE table_name = {dbx.sql_string(table)}")
            rows = self.client().rows(st)
            got = datetime.fromisoformat(rows[0][0]) if rows and rows[0][0] else None
        with self._lock:
            self._previous[table] = got
        return got

    def write_full(self, con, relation, *, table: str, pk: list[str] | None, f: datetime) -> None:
        relation = _user_columns(relation)
        cols = ", ".join(dbx.ident(c) for c in relation.columns)
        with self._staged(con, relation, f, stamp=False) as (files, staged):
            self._apply(table, files, staged, lambda tgt: f"INSERT OVERWRITE {tgt} ({cols}) SELECT {cols} FROM {files}")
        self._set_watermark(table, f)

    def apply_delta(self, con, delta, *, table: str, pk: list[str] | None, f: datetime) -> None:
        names = list(delta.upserts.columns)
        cols = ", ".join(dbx.ident(c) for c in names)
        if pk:
            match = " AND ".join(f"t.{_q(c)} IS NOT DISTINCT FROM d.{_q(c)}" for c in pk)
            # The window's changed rows, plus its removed keys that weren't re-added (a MERGE source holds each
            # key once), told apart by an operation column.
            up, gone = delta.upserts.sql_query(), delta.deletes.sql_query()
            source = con.sql(
                f"SELECT {', '.join(_q(c) for c in names)}, 'u' AS {_OP} FROM ({up}) "
                f"UNION ALL BY NAME SELECT DISTINCT {', '.join('d.' + _q(c) for c in pk)}, 'd' AS {_OP} "
                f"FROM ({gone}) AS d ANTI JOIN ({up}) AS t ON {match}")
            if not source.limit(1).fetchall():
                return self._set_watermark(table, f)  # nothing changed: no warehouse work
            on = " AND ".join(f"t.{dbx.ident(c)} <=> s.{dbx.ident(c)}" for c in pk)
            sets = ", ".join(f"t.{dbx.ident(c)} = s.{dbx.ident(c)}" for c in names)
            vals = ", ".join(f"s.{dbx.ident(c)}" for c in names)
            with self._staged(con, source, f, stamp=False) as (files, staged):
                self._apply(table, files, staged, lambda tgt: (
                    f"MERGE INTO {tgt} AS t USING (SELECT * FROM {files}) AS s ON {on} "
                    f"WHEN MATCHED AND s.{_OP} = 'd' THEN DELETE "
                    f"WHEN MATCHED THEN UPDATE SET {sets} "
                    f"WHEN NOT MATCHED AND s.{_OP} = 'u' THEN INSERT ({cols}) VALUES ({vals})"))
        else:
            if delta.deletes.limit(1).fetchall():
                raise ValueError(f"table {table!r} retracted rows but has no primary key to find them by "
                                 "(put a merge Trickle, .merge(pk=…), before this Spout)")
            if not delta.upserts.limit(1).fetchall():
                return self._set_watermark(table, f)
            stamped = f"{cols}, {dbx.ident(STAMP)}"
            # Every row delivered after the recorded watermark is this window's (a replay can span a delivery whose
            # watermark wasn't written), so they are replaced together. Stamps are UTC ISO strings, ordered as text.
            with self._lock:
                previous = self._previous.get(table)
            after = dbx.sql_string(previous.isoformat()) if previous else "''"
            with self._staged(con, delta.upserts, f, stamp=True) as (files, staged):
                self._apply(table, files, staged, lambda tgt: (
                    f"INSERT INTO {tgt} ({stamped}) REPLACE WHERE {dbx.ident(STAMP)} > {after} "
                    f"SELECT {stamped} FROM {files}"))
        self._set_watermark(table, f)

    # ─── helpers ─────────────────────────────────────────────────────────────

    def _staged(self, con, relation, f: datetime, *, stamp: bool):
        """A context manager staging ``relation`` in the Volume (with ``stamp``, plus the delivery's freshness as
        an ISO string in ``_duckstring_f``); yields the ``read_files(...)`` to select from and the staged column
        names."""
        from ..flock.engines.sql_engine import stage_select

        @contextmanager
        def staged():
            local = tempfile.mkdtemp(prefix="duckstring-egress-databricks-")
            remote = f"{self.config()['volume']}/.duckstring-staging/{uuid.uuid4().hex}"
            try:
                select = con.sql(stage_select(relation)).sql_query()
                if stamp:
                    select = f"SELECT *, '{f.isoformat()}' AS {_q(STAMP)} FROM ({select})"
                rows = os.path.join(local, "rows")
                con.execute(f"COPY ({select}) TO '{rows.replace(chr(39), chr(39) * 2)}' (FORMAT PARQUET, "
                            f"FILE_SIZE_BYTES {_STAGE_FILE_BYTES}, OVERWRITE_OR_IGNORE)")
                for name in sorted(os.listdir(rows)):
                    self.client().upload(os.path.join(rows, name), f"{remote}/{name}")
                columns = [c for c in con.sql(select).columns]
                yield (f"read_files({dbx.sql_string(remote)}, format => 'parquet', schemaEvolutionMode => 'none')",
                       columns)
            finally:
                shutil.rmtree(local, ignore_errors=True)
                try:
                    self.client().remove_tree(remote)
                except Exception:  # noqa: BLE001 (best effort; a later delivery never reads it)
                    pass

        return staged()

    def _apply(self, table: str, files: str, staged: list[str], statement) -> None:
        """Run ``statement(target)`` against the target, made ready first unless this process already knows it has
        the staged columns. A target the warehouse no longer finds (dropped since) is made again and the statement
        run once more."""
        key = (self.config()["host"], self._target(table))
        wanted = {c.lower() for c in staged if c != _OP}
        with _KNOWN_LOCK:
            known = _KNOWN_COLUMNS.get(key)
        tgt = self._target(table) if known is not None and wanted <= known else self._ensure_table(table, files)
        try:
            self._sql(statement(tgt))
        except dbx.DatabricksError as exc:
            if "TABLE_OR_VIEW_NOT_FOUND" not in str(exc) and "cannot be found" not in str(exc):
                raise
            with _KNOWN_LOCK:
                _KNOWN_COLUMNS.pop(key, None)
            self._sql(statement(self._ensure_table(table, files)))

    def _ensure_table(self, table: str, files: str) -> str:
        """Create the target from the staged data's schema if absent, and add any column it has gained. The
        operation column of a merge's staging is left out. Returns the quoted target."""
        tgt = self._target(table)
        staged = [(r[0], r[1]) for r in self.client().rows(self._sql(f"DESCRIBE QUERY SELECT * FROM {files}"))]
        columns = [(n, t) for n, t in staged if n != _OP]
        select = ", ".join(dbx.ident(n) for n, _t in columns)
        self._sql(f"CREATE TABLE IF NOT EXISTS {tgt} AS SELECT {select} FROM {files} LIMIT 0")
        current = {r[0].lower() for r in self.client().rows(self._sql(f"DESCRIBE TABLE {tgt}"))
                   if r and r[0] and not r[0].startswith("#")}
        added = [(n, t) for n, t in columns if n.lower() not in current]
        if added:
            self._sql(f"ALTER TABLE {tgt} ADD COLUMNS ({', '.join(f'{dbx.ident(n)} {t}' for n, t in added)})")
        with _KNOWN_LOCK:
            _KNOWN_COLUMNS[(self.config()["host"], tgt)] = frozenset(current | {n.lower() for n, _t in added})
        return tgt

    def _watermark_table(self) -> str:
        wm = self._target(WATERMARK)
        key = (self.config()["host"], wm)
        with _KNOWN_LOCK:
            known = key in _KNOWN_WATERMARKS
        if not known:
            self._sql(f"CREATE TABLE IF NOT EXISTS {wm} (table_name STRING, f STRING)")
            with _KNOWN_LOCK:
                _KNOWN_WATERMARKS.add(key)
        return wm

    def _set_watermark(self, table: str, f: datetime) -> None:
        if self._batch is not None:
            with self._lock:
                self._batch["pending"][table] = f
            return
        self._write_watermarks({table: f})

    def _write_watermarks(self, marks: dict[str, datetime]) -> None:
        wm = self._watermark_table()
        rows = ", ".join(f"({dbx.sql_string(t)}, {dbx.sql_string(f.isoformat())})" for t, f in sorted(marks.items()))
        statement = (f"MERGE INTO {wm} AS t USING (SELECT * FROM VALUES {rows} AS v(table_name, f)) AS s "
                     "ON t.table_name = s.table_name WHEN MATCHED THEN UPDATE SET t.f = s.f "
                     "WHEN NOT MATCHED THEN INSERT (table_name, f) VALUES (s.table_name, s.f)")
        try:
            self._sql(statement)
        except dbx.DatabricksError as exc:
            if "TABLE_OR_VIEW_NOT_FOUND" not in str(exc) and "cannot be found" not in str(exc):
                raise
            with _KNOWN_LOCK:
                _KNOWN_WATERMARKS.discard((self.config()["host"], wm))
            self._watermark_table()
            self._sql(statement)


def _user_columns(relation):
    from .attached import _user_columns as strip

    return strip(relation)
