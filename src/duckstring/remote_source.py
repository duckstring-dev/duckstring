"""A Source read through another Catchment's query API, at a position: how a Puddle Source reads production
(plans/showcase-fixes.md item 10). Nothing is read in place: each read is ``POST /api/query`` with ``at``, and each
window of changes the same query with ``since`` too, so a filter runs on the Source's Catchment and only the rows it
keeps travel. Nothing is written on the reading side but the temporary tables a Run reads.

Each of the Source's tables is read through a query: by default the whole table, or a Puddle definition's query
(:func:`compile_definitions`) when the Pond has one."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime

STRIP = "SELECT COLUMNS(lambda c: NOT starts_with(c, '_duckstring_')) FROM ({sql})"


class RemoteQueryError(RuntimeError):
    """The Source's Catchment refused a query (``status`` is its HTTP status)."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"the Source's Catchment refused the query ({status}): {detail}")
        self.status = status
        self.detail = detail


class SourceTrimmed(RuntimeError):
    """A table holds nothing from before the point asked for (created later, or trimmed since)."""


class HttpTransport:
    """Queries a Catchment over HTTP with a registered Catchment's address and credentials."""

    def __init__(self, url: str, headers: dict | None = None, timeout: float = 300.0):
        self.url = url.rstrip("/")
        self.headers = dict(headers or {})
        self.timeout = timeout

    @classmethod
    def registered(cls, catchment: str | None) -> HttpTransport:
        from .core import resolve_catchment_auth

        url, headers = resolve_catchment_auth(catchment)
        return cls(url, headers)

    def _check(self, resp):
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:  # noqa: BLE001
                detail = resp.text[:300]
            raise RemoteQueryError(resp.status_code, str(detail))
        return resp

    def query(self, payload: dict) -> tuple[bytes, str | None]:
        import httpx

        resp = self._check(httpx.post(f"{self.url}/api/query", json=payload, headers=self.headers,
                                      timeout=httpx.Timeout(self.timeout, connect=10.0)))
        return resp.content, resp.headers.get("x-duckstring-warning")

    def get(self, path: str, params: dict | None = None):
        import httpx

        resp = self._check(httpx.get(f"{self.url}{path}", params=params, headers=self.headers,
                                     timeout=httpx.Timeout(30.0, connect=10.0)))
        return resp.json()


def _iso(f: datetime) -> str:
    return f.isoformat()


class RemoteSource:
    """One Source line (``source@major``) on another Catchment, read through ``transport``.

    ``queries`` maps a table to the query that stands for it (a Puddle definition's); every other table is the
    Source's own, whole."""

    def __init__(self, source: str, major: int, transport, queries: dict[str, str] | None = None,
                 kinds: dict[str, tuple] | None = None):
        self.source = source
        self.major = major
        self.transport = transport
        self.queries = dict(queries or {})
        # A query's table read as (mode, pk), where its definition said (p.append_table, p.merge_table) or it
        # names one of the Source's tables; every other table is read as the Source publishes it.
        self.kinds = dict(kinds or {})
        self.warnings: list[str] = []
        self._tables: dict | None = None
        self._reports: bool | None = None
        self._fetched: dict = {}

    # ── metadata ────────────────────────────────────────────────────────────

    def tables(self) -> dict[str, dict]:
        """``{table: {"trickle": "append"|"merge"|None, "pk": [...]}}`` for the Source's published tables."""
        if self._tables is None:
            body = self.transport.get(f"/api/ponds/{self.source}/tables", {"major": self.major})
            self._tables = {t["name"]: t for t in body.get("tables", [])}
        return self._tables

    def mode(self, table: str) -> str | None:
        from .reports import REPORTS_TABLE

        if table in self.kinds:
            return self.kinds[table][0]
        if table == REPORTS_TABLE:
            return "append"
        return (self.tables().get(table) or {}).get("trickle")

    def pk(self, table: str) -> tuple[str, ...]:
        from .reports import PK, REPORTS_TABLE

        if table in self.kinds:
            return tuple(self.kinds[table][1])
        if table == REPORTS_TABLE:
            return tuple(PK)
        return tuple((self.tables().get(table) or {}).get("pk") or ())

    def has_reports(self) -> bool:
        """Whether the Source publishes Run reports (a table read by name, never listed)."""
        from .reports import REPORTS_TABLE

        if self._reports is None:
            try:
                self.transport.query({"pond": self.source, "major": self.major, "format": "parquet",
                                      "sql": f'SELECT * FROM "{self.source}"."{REPORTS_TABLE}" LIMIT 0'})
                self._reports = True
            except RemoteQueryError as exc:
                if exc.status != 400:
                    raise
                self._reports = False
        return self._reports

    def published(self) -> list[str]:
        """The Source's own tables a whole copy takes: its outputs and its Run reports, not the companions a
        Trickle publishes beside a table."""
        from .reports import REPORTS_TABLE
        from .trickle_io import base_table_name

        tables = [t for t in self.tables() if base_table_name(t) == t and not t.startswith("_duckstring_")]
        return tables + ([REPORTS_TABLE] if self.has_reports() else [])

    def run_freshnesses(self, after: datetime | None = None,
                        upto: datetime | None = None) -> list[datetime] | None:
        """The freshnesses the line's Runs reached in ``(after, upto]``, passes included, oldest first; ``None``
        when no such line is deployed on the Catchment."""
        from .engine.core import NEVER

        params = {"pond": self.source, "major": self.major, "lineage": "false", "limit": 1000}
        out: set[datetime] = set()
        cursor = after if after is not None and after > NEVER else None
        while True:
            try:
                page = self.transport.get("/api/runs", {**params, **({"after": _iso(cursor)} if cursor else {})})
            except RemoteQueryError as exc:
                if exc.status != 404:
                    raise
                return None
            runs = page.get("runs", [])
            for r in runs:
                if r.get("status") == "success" and r.get("f"):
                    out.add(datetime.fromisoformat(r["f"]))
            if cursor is None or len(runs) < 1000:
                break
            cursor = max(datetime.fromisoformat(r["started_at"]) for r in runs if r.get("started_at"))
        return sorted(f for f in out if (after is None or f > after) and (upto is None or f <= upto))

    def runs(self, after: datetime | None = None, upto: datetime | None = None) -> list[datetime]:
        """The freshnesses the Source reached in ``(after, upto]``: its Runs', passes included, and the points its
        published tables changed at (a Source's freshness moves on every Run, which its changes alone wouldn't
        show; its changes alone stand in when no line is deployed, as for one removed since it published)."""
        out = set(self.run_freshnesses(after, upto) or ())
        out.update(self.changes(after, upto))
        return sorted(out)

    def changes(self, after: datetime | None = None, upto: datetime | None = None) -> list[datetime]:
        """The freshnesses at which the Source's published tables changed in ``(after, upto]``."""
        params = {"major": self.major}
        if after is not None:
            params["after"] = _iso(after)
        if upto is not None:
            params["upto"] = _iso(upto)
        body = self.transport.get(f"/api/ponds/{self.source}/changes", params)
        return [datetime.fromisoformat(c) for c in body.get("changes", [])]

    def latest(self, upto: datetime) -> datetime | None:
        """The Source's newest freshness at or before ``upto`` (its newest Run, else its newest change)."""
        points = self.runs(upto=upto)
        return points[-1] if points else None

    # ── reads ───────────────────────────────────────────────────────────────

    def sql(self, table: str) -> str:
        return self.queries.get(table) or f'SELECT * FROM "{self.source}"."{table}"'

    def _fetch(self, con, sql: str, at: datetime, since: datetime | None = None) -> str:
        """Run ``sql`` on the Source's Catchment at ``at`` (with ``since``: its change) into a temporary table on
        ``con``; returns its name. Fetched once per connection, point and query."""
        from .trickle_io import _table_exists, unique_name

        key = (id(con), sql, at, since)
        name = self._fetched.get(key)
        if name is not None and _table_exists(con, name):
            return name
        payload = {"pond": self.source, "major": self.major, "sql": STRIP.format(sql=sql), "format": "parquet"}
        if at is not None:  # None: the newest (an App's read)
            payload["at"] = _iso(at)
        if since is not None:
            payload["since"] = _iso(since)
        try:
            content, warning = self.transport.query(payload)
        except RemoteQueryError as exc:
            if exc.status == 422 and "holds nothing from before" in exc.detail:
                raise SourceTrimmed(exc.detail) from exc
            raise
        if warning and warning not in self.warnings:
            self.warnings.append(warning)
            import logging

            logging.getLogger(__name__).warning("%s: %s", self.source, warning)
        fd, path = tempfile.mkstemp(suffix=".parquet", prefix="duckstring-remote-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
            name = unique_name("_remote")
            escaped = path.replace("'", "''")
            con.execute(f"CREATE TEMP TABLE {name} AS SELECT * FROM read_parquet('{escaped}')")
        finally:
            os.unlink(path)
        self._fetched[key] = name
        return name

    def _missing(self, table: str, exc: RemoteQueryError):
        from .core import MissingSourceAsset

        if exc.status == 400 and table in exc.detail and "does not exist" in exc.detail:
            return MissingSourceAsset(self.source, table)
        return exc

    def relation(self, con, table: str, at: datetime):
        """The table (or its query's result) as it stood at ``at``, without system columns."""
        try:
            return con.table(self._fetch(con, self.sql(table), at))
        except RemoteQueryError as exc:
            raise self._missing(table, exc) from exc

    def columns(self, con, table: str, at: datetime) -> list[str]:
        return list(self.relation(con, table, at).columns)

    def count(self, con, table: str, at: datetime) -> int:
        from .trickle_io import first_row

        return int(first_row(self.relation(con, table, at).aggregate("count(*)"))[0])

    def delta(self, con, table: str, previous: datetime, at: datetime):
        """A ``Delta`` over ``(previous, at]``: the query's change between the two points, as the Source's own
        delta would be (an append table's added rows, a merge table's net change; a plain table, or a query over
        one, read whole when it changed). A first read, or a previous point the table no longer holds, is a full
        read."""
        from .engine.core import NEVER
        from .trickle_io import Delta, _as_zset, first_row

        pk = self.pk(table)
        if previous is None or previous <= NEVER or previous >= at:
            if previous is not None and previous > NEVER:  # nothing new: an empty window
                rel = self.relation(con, table, at)
                return Delta(con, pk, _as_zset(rel, 1).filter("false"), insert_only=True)
            return Delta(con, pk, _as_zset(self.relation(con, table, at), 1), is_full=True)
        try:
            zset = con.table(self._fetch(con, self.sql(table), at, since=previous))
        except SourceTrimmed:
            return Delta(con, pk, _as_zset(self.relation(con, table, at), 1), is_full=True)
        except RemoteQueryError as exc:
            raise self._missing(table, exc) from exc
        if self.mode(table) not in ("append", "merge"):
            if first_row(zset.aggregate("count(*)"))[0]:
                return Delta(con, pk, _as_zset(self.relation(con, table, at), 1), is_full=True)
            return Delta(con, pk, zset, insert_only=True)
        negative = first_row(zset.filter("_duckstring_d < 0").aggregate("count(*)"))[0]
        return Delta(con, pk, zset, insert_only=not negative)


# ── Puddle definitions as queries ───────────────────────────────────────────


class NotAQuery(ValueError):
    """A definition that reads its Source but whose result isn't a query over it alone."""


def query_references(con, sql: str) -> set[tuple[str | None, str]]:
    """The ``(schema, table)`` base-table references of ``sql`` (its CTEs and table functions aside)."""
    import json

    from .trickle_io import first_row

    tree = json.loads(first_row(con.execute("SELECT json_serialize_sql(?)", [sql]))[0])
    if tree.get("error"):
        raise NotAQuery(tree.get("error_message") or "the query couldn't be parsed")
    refs: set[tuple[str | None, str]] = set()
    ctes: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            cte_map = node.get("cte_map")
            if isinstance(cte_map, dict):
                for entry in cte_map.get("map", []) or []:
                    if isinstance(entry, dict) and entry.get("key"):
                        ctes.add(entry["key"])
            if node.get("type") == "BASE_TABLE":
                refs.add((node.get("schema_name") or None, node.get("table_name")))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(tree)
    return {(s, t) for s, t in refs if not (s is None and t in ctes)}
