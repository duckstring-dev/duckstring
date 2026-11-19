"""One Source's Puddle being written at one freshness: the connection a definition or step builds on, the reads of
the real Source (``p.read_source``) and the published layout it writes, as a Pond publishes, so a local run reads it
through the data plane like any Source (deltas and as-of reads included)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .project import Project


class SourceSession:
    """Writes the Puddle of ``source`` into ``path`` at freshness ``f``.

    ``read_at`` is the freshness ``p.read_source`` reads the real Source as of (``f`` unless given). ``locator`` finds
    the real Source (``None``: ``p.read_source`` refuses). With existing published state in ``path`` (a step after the
    baseline, a live definition's next window), the registry is rebuilt from it first, so a merge write is diffed
    against the state before and an append adds to its history."""

    def __init__(self, project: Project, source: str, path: Path, f: datetime, *, previous_f: datetime | None = None,
                 step: int = 0, locator=None, read_at: datetime | None = None, retain_from: datetime | None = None):
        from ..engine.core import NEVER

        self.project = project
        self.source = source
        self.path = Path(path)
        self.f = f
        self.previous_f = previous_f if previous_f is not None else NEVER
        self.step = step
        self.read_at = read_at or f
        self.retain_from = retain_from
        self.locator = locator
        self.used_source = False
        # Called at the first p.read_source() before anything is written: the Source's own freshness at the point,
        # which becomes the stamp and read position (the Puddle is as fresh as the Source was).
        self.resolve_point = None
        self.written: set[str] = set()
        self._con = None
        self._writer = None

    # ── connection and writer ───────────────────────────────────────────────

    @property
    def con(self):
        if self._con is None:
            import duckdb

            self._con = duckdb.connect()
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

    # ── the real Source ─────────────────────────────────────────────────────

    def source_store(self):
        if self.locator is None:
            raise ValueError("p.read_source() needs a Catchment to read from: none is configured for this hydration")
        return self.locator.storage(self.source)

    def source_entry(self, table: str) -> dict:
        from ..trickle.io import load_sidecar, sidecar_tables

        return sidecar_tables(load_sidecar(self.source_store())).get(table) or {}

    def source_relation(self, table: str):
        from ..dataplane import get_data_plane
        from ..trickle_io import _strip_system
        from .sources import earliest_readable

        store = self.source_store()
        if not self.used_source and self.resolve_point is not None and not self.written:
            self.f = self.read_at = self.resolve_point()
            if self._writer is not None:
                self._writer.f = self.f
        self.used_source = True
        earliest, exact = earliest_readable(store, table)
        if exact and earliest is not None and self.read_at < earliest:
            raise ValueError(
                f"'{self.source}.{table}' is kept from {earliest.isoformat()} on, so it can't be read as of "
                f"{self.read_at.isoformat()}: hydrate from {earliest.isoformat()} or later"
            )
        if not exact and earliest is not None and self.read_at < earliest:
            import logging

            logging.getLogger(__name__).warning(
                "'%s.%s' no longer keeps its version at %s; reading the oldest it keeps (%s)",
                self.source, table, self.read_at.isoformat(), earliest.isoformat())
        store.duckdb_setup(self.con)
        try:
            select = get_data_plane().read_select(store, table, as_of=self.read_at, pin=self.read_at, con=self.con)
        except FileNotFoundError:
            raise ValueError(f"'{self.source}' publishes no table '{table}'") from None
        return _strip_system(self.con.sql(select))

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
            "SELECT 1 FROM duckdb_tables() WHERE table_name = ? AND database_name = current_database() "
            "UNION ALL SELECT 1 FROM duckdb_views() WHERE view_name = ? AND NOT temporary "
            "AND database_name = current_database()", [table, table])) is not None

    def write(self, table: str, relation):
        """``p.write_table``: as the kind the real Source publishes ``table`` as, when the definition read the
        Source (so the Puddle's deltas are the Source's); a plain table otherwise."""
        relation = self._relation(relation)
        entry = self.source_entry(table) if self.used_source else {}
        mode, pk = entry.get("mode"), tuple(entry.get("pk") or ())
        if mode == "merge" and pk:
            return self.merge(table, relation, pk=pk)
        if mode == "append":
            if self._has_own(table):
                relation = self._new_rows(table, relation)
            return self.append(table, relation, pk=pk or None)
        self.written.add(table)
        self.writer.write_table(table, relation)
        return None

    def _new_rows(self, table: str, relation):
        """The rows of ``relation`` an append table's history doesn't hold yet, copy for copy."""
        from ..trickle_io import temp_view, unique_name

        new = unique_name("_puddle_new")
        cur = unique_name("_puddle_cur")
        temp_view(self.con, relation, new)
        temp_view(self.con, self.read_own(table), cur)
        cols = ", ".join(f'"{c}"' for c in relation.columns)
        return self.con.sql(f"SELECT {cols} FROM {new} EXCEPT ALL SELECT {cols} FROM {cur}")

    def append(self, table: str, relation, *, pk=None, on_conflict: str = "skip_check", log_drops: bool = True):
        self.written.add(table)
        return self.writer.append_table(table, self._relation(relation), pk=pk, on_conflict=on_conflict,
                                        log_drops=log_drops)

    def merge(self, table: str, relation, *, pk):
        self.written.add(table)
        return self.writer.merge_table(table, self._relation(relation), pk=pk)

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

    def _drop_untracked(self) -> None:
        from ..trickle.io import read_meta

        mains = set(read_meta(self.con)) | self.written
        rows = self.con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = current_database() AND NOT temporary"
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
