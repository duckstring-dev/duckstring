"""One Source's Puddle being written at one freshness: the connection a definition or step builds on and the published
layout it writes, as a Pond publishes, so a local run reads it through the data plane like any Source (deltas and
as-of reads included).

A definition that reads the real Source (``p.read_source``) is compiled, not run against data: the handle gives it a
stand-in table with the Source table's columns, and what it writes from it is recorded as a query
(:attr:`SourceSession.queries`). The query runs on the Source's Catchment (``/api/query`` with ``at``), either each
time a run reads the Source (a live Puddle) or once per point when the Puddle is copied
(:mod:`duckstring.remote_source`)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .project import Project


def _qi(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


class SourceSession:
    """Writes the Puddle of ``source`` into ``path`` at freshness ``f``.

    ``read_at`` is the freshness the stand-ins for ``p.read_source`` take the Source's columns at (``f`` unless
    given). ``remote`` is the Source on its Catchment (a :class:`~duckstring.remote_source.RemoteSource`; ``None``:
    ``p.read_source`` refuses). With existing published state in ``path`` (a step after the baseline, a copied
    window), the registry is rebuilt from it first, so a merge write is diffed against the state before and an
    append adds to its history."""

    def __init__(self, project: Project, source: str, path: Path, f: datetime, *, previous_f: datetime | None = None,
                 step: int = 0, remote=None, read_at: datetime | None = None, retain_from: datetime | None = None):
        from ..engine.core import NEVER

        self.project = project
        self.source = source
        self.path = Path(path)
        self.f = f
        self.previous_f = previous_f if previous_f is not None else NEVER
        self.step = step
        self.read_at = read_at or f
        self.retain_from = retain_from
        self.remote = remote
        self.used_source = False  # any definition read the Source
        self.reading = False  # the definition being run read the Source
        # Called at the first p.read_source() before anything is written: the Source's own freshness at the point,
        # which becomes the stamp (the Puddle is as fresh as the Source was).
        self.resolve_point = None
        self.written: set[str] = set()  # tables written here, as data
        self.queries: dict[str, str] = {}  # table → the query over the Source that stands for it
        self.kinds: dict[str, tuple] = {}  # table → (mode, pk) a query's table is written or read as
        self._stand_ins: set[tuple[str, str]] = set()
        self._con = None
        self._writer = None

    # ── connection and writer ───────────────────────────────────────────────

    @property
    def con(self):
        if self._con is None:
            import duckdb

            self._con = duckdb.connect()
            self._con.execute("SET TimeZone='UTC'")
            self.path.mkdir(parents=True, exist_ok=True)
            if (self.path / "_trickle.json").exists():
                from ..dataplane import hydrate_registry

                hydrate_registry(self._con, self.path)
        return self._con

    @property
    def writer(self):
        if self._writer is None:
            from ..core import Pond

            self._writer = Pond(self.source, "0.0.0", self.con, root=self.project.puddles_dir, f=self.f,
                                previous_f=self.previous_f, sources=[], own_data_dir=self.path,
                                staging_dir=self.path / ".staging", exact_ints=True)
        return self._writer

    def begin_definition(self) -> None:
        self.reading = False

    # ── the real Source ─────────────────────────────────────────────────────

    def source_relation(self, table: str):
        """A stand-in for the Source's ``table``: an empty table with its columns, named ``"{source}"."{table}"``,
        so a definition's relations over it compile to a query the Source's Catchment can run."""
        from ..remote_source import RemoteQueryError

        if self.remote is None:
            raise ValueError("p.read_source() needs a Catchment to read from: none is configured for this hydration "
                             "(a step writes generated rows; read the Source in the @puddle definition)")
        if not self.used_source and self.resolve_point is not None and not self.written:
            self.f = self.read_at = self.resolve_point()
            if self._writer is not None:
                self._writer.f = self.f
        self.used_source = self.reading = True
        key = (self.source, table)
        if key not in self._stand_ins:
            try:
                empty = self.remote._fetch(self.con, f"SELECT * FROM {_qi(self.source)}.{_qi(table)} LIMIT 0",
                                           self.read_at)
            except RemoteQueryError as exc:
                raise ValueError(f"couldn't read '{self.source}.{table}' from its Catchment: {exc.detail}") from None
            self.con.execute(f"CREATE SCHEMA IF NOT EXISTS {_qi(self.source)}")
            self.con.execute(f"CREATE TABLE {_qi(self.source)}.{_qi(table)} AS SELECT * FROM {empty}")
            self._stand_ins.add(key)
        return self.con.table(f"{_qi(self.source)}.{_qi(table)}")

    def _query_of(self, table: str, relation) -> str | None:
        """The query ``relation`` is over the Source's stand-ins, or ``None`` for data built here. Raises
        :class:`~duckstring.remote_source.NotAQuery` for a definition that read the Source but wrote something
        that isn't a query over it alone."""
        from ..remote_source import NotAQuery, query_references

        if not self._stand_ins:
            return None
        try:
            sql = relation.sql_query()
            refs = query_references(self.con, sql)
        except Exception:  # noqa: BLE001 - not expressible as SQL (a DataFrame, an Arrow table)
            sql, refs = None, set()
        ours = {r for r in refs if r in self._stand_ins}
        if sql is not None and refs and ours == refs:
            return sql
        if self.reading or ours:
            raise NotAQuery(
                f"'{self.source}.{table}': a definition that reads its Source with p.read_source() must write a query "
                "over the Source alone (DuckDB relations: filters, projections, joins, aggregates, samples by key "
                "hash), so it can run on the Source's Catchment. Rows fetched into Python, or other tables mixed in, "
                "can't be sent there")
        return None

    def _record_query(self, table: str, sql: str, mode: str | None, pk) -> None:
        self.queries[table] = sql
        self.kinds[table] = (mode, tuple(pk) if pk else ())

    # ── writes ──────────────────────────────────────────────────────────────

    def _relation(self, relation):
        if not hasattr(relation, "sql_query"):
            relation = self.con.from_df(relation)
        return relation

    def read_own(self, table: str):
        return self.writer.read_table(table)

    def _has_own(self, table: str) -> bool:
        from ..trickle.io import first_row

        return first_row(self.con.execute(
            "SELECT 1 FROM duckdb_tables() WHERE table_name = ? AND schema_name = 'main' "
            "AND database_name = current_database() "
            "UNION ALL SELECT 1 FROM duckdb_views() WHERE view_name = ? AND NOT temporary "
            "AND database_name = current_database()", [table, table])) is not None

    def write(self, table: str, relation):
        """``p.write_table``: a query over the Source is recorded, as the kind the Source publishes ``table`` as
        (so the Puddle's deltas are the Source's); data built here is a plain table."""
        relation = self._relation(relation)
        sql = self._query_of(table, relation)
        if sql is not None:
            remote = self.remote
            self._record_query(table, sql, remote.mode(table), remote.pk(table))
            return None
        self.written.add(table)
        self.writer.write_table(table, relation)
        return None

    def append(self, table: str, relation, *, pk=None, on_conflict: str = "skip_check", log_drops: bool = True):
        relation = self._relation(relation)
        sql = self._query_of(table, relation)
        if sql is not None:
            self._record_query(table, sql, "append", pk)
            return None
        self.written.add(table)
        return self.writer.append_table(table, relation, pk=pk, on_conflict=on_conflict, log_drops=log_drops)

    def merge(self, table: str, relation, *, pk):
        relation = self._relation(relation)
        sql = self._query_of(table, relation)
        if sql is not None:
            self._record_query(table, sql, "merge", pk)
            return None
        self.written.add(table)
        return self.writer.merge_table(table, relation, pk=pk)

    def write_data(self, table: str, relation, mode: str | None, pk) -> None:
        """Write fetched rows as ``mode`` (a copied query's table at one point): a merge table's complete state, an
        append table's rows the history doesn't hold yet, or a plain table's version."""
        self.written.add(table)
        if mode == "merge" and pk:
            self.writer.merge_table(table, relation, pk=pk)
        elif mode == "append":
            if self._has_own(table):
                relation = self._new_rows(table, relation)
            self.writer.append_table(table, relation, pk=pk or None)
        else:
            self.writer.write_table(table, relation)

    def apply_change(self, table: str, delta, mode: str | None, pk) -> None:
        """Apply a copied query's change at one point (a ``Delta`` from the Source's Catchment)."""
        from ..trickle_io import _strip_system

        if delta.is_full:
            self.write_data(table, delta.zset.filter("_duckstring_d > 0").project(
                ", ".join(f'"{c}"' for c in delta.zset.columns if c != "_duckstring_d")), mode, pk)
            return
        if delta.is_empty():
            return
        self.written.add(table)
        if mode == "merge" and pk:
            self.writer.apply_zset(table, delta.zset, pk=pk)
        elif mode == "append":
            self.writer.append_table(table, _strip_system(delta.zset.filter("_duckstring_d > 0")), pk=pk or None)

    def _new_rows(self, table: str, relation):
        """The rows of ``relation`` an append table's history doesn't hold yet, copy for copy."""
        from ..trickle_io import temp_view, unique_name

        new = unique_name("_puddle_new")
        cur = unique_name("_puddle_cur")
        temp_view(self.con, relation, new)
        temp_view(self.con, self.read_own(table), cur)
        cols = ", ".join(f'"{c}"' for c in relation.columns)
        return self.con.sql(f"SELECT {cols} FROM {new} EXCEPT ALL SELECT {cols} FROM {cur}")

    # ── publish ─────────────────────────────────────────────────────────────

    def publish(self) -> bool:
        """Publish what was written at ``f`` (nothing written publishes nothing, so a definition that wrote its
        files itself keeps them). Other tables a definition left on ``con`` are dropped first: only what it wrote
        through the handle belongs to the Puddle."""
        if self._con is None or not self.written:
            return False
        self._drop_untracked()
        from ..dataplane import KEEP_ALL, get_data_plane, registry_tables
        from ..trickle.io import read_meta

        # A plain table this write didn't touch keeps its published version: republished, it would read as changed.
        held = set(registry_tables(self.con)) - self.written - set(read_meta(self.con))
        retain = self.retain_from if self.retain_from is not None else KEEP_ALL
        get_data_plane().export(self.con, self.path, f=self.f, retain_from=retain, held=held, exact_ints=True)
        return True

    def drop_stand_ins(self) -> None:
        """Drop the stand-ins once the definitions have run: Trickle writes look tables up by bare name."""
        for source, _ in self._stand_ins:
            self.con.execute(f"DROP SCHEMA IF EXISTS {_qi(source)} CASCADE")
        self._stand_ins.clear()

    def _drop_untracked(self) -> None:
        from ..trickle.io import read_meta

        self.drop_stand_ins()
        mains = set(read_meta(self.con)) | self.written
        rows = self.con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = current_database() AND NOT temporary "
            "AND schema_name = 'main'"
        ).fetchall()
        for (name,) in rows:
            if name.startswith("_duckstring") or name in mains or any(name.startswith(f"{m}__") for m in mains):
                continue
            self.con.execute(f'DROP TABLE "{name}"')

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None
            self._writer = None
