"""The Iceberg data-plane backend — the default (``DUCKSTRING_DATA_PLANE`` unset or ``iceberg``).

An Apache Iceberg base layer over the Parquet files we already write: it adds **snapshots** (one
overwrite commit per Pond Run, stamped with the run's freshness ``f``) and **schema metadata**, the
substrate Phase 2 contracts and the later Trickle work build on. The data files stay Parquet — this is
a metadata/catalog layer, not a file-format swap.

Layout (per ``name@major`` line, so the major-line isolation ``ponds/{name}/m{major}/`` already gives
is preserved physically, not just by namespace — and there is no shared catalog for concurrent Ducks
to contend on):

- a :class:`~duckstring.iceberg_catalog.FileCatalog` (a JSON-pointer pyiceberg catalog, **no
  SQLAlchemy**) at ``{data_dir}/catalog.json``, warehouse rooted at ``data_dir``;
- one namespace, ``pond`` — the catalog is already isolated to one line, so the table is ``pond.{table}``.

Writes go through pyiceberg (Arrow ``overwrite``); reads go through DuckDB's ``iceberg`` extension
(``iceberg_scan`` on the snapshot's metadata file). A **flat ``{table}.parquet`` copy is written
alongside** each commit: it keeps the unchanged consumers working behaviour-neutrally — the duct/draw
file transfer, the direct file-serve, and the transitional read of a Source that hasn't re-exported to
Iceberg yet. The ``catalog.json`` and the Iceberg metadata/data under ``data_dir`` are included in
``catchment archive`` by the existing root walk (download while quiescent).
"""

from __future__ import annotations

from pathlib import Path

from .dataplane import DataPlane, ParquetDataPlane

_NAMESPACE = "pond"  # the single namespace within each per-line catalog
F_PROP = "duckstring.f"  # snapshot summary property carrying the Pond Run's freshness
# Table properties for Trickle append/changelog tables (survive a retention delete-snapshot, unlike a
# snapshot summary): the append cursor (max committed _duckstring_f) and the retained-history floor.
LAST_F_PROP = "duckstring.last_f"
FLOOR_PROP = "duckstring.floor"


class IcebergDataPlane(DataPlane):
    def __init__(self) -> None:
        # The flat-Parquet sidecar: the compat copy for draws, direct-serve, and the legacy fallback.
        self._parquet = ParquetDataPlane()

    # ─── catalog ──────────────────────────────────────────────────────────────

    def _catalog(self, data_dir: Path):
        from .iceberg_catalog import FileCatalog

        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        cat = FileCatalog(
            "duckstring",
            catalog_path=data_dir / "catalog.json",
            warehouse=data_dir.as_uri(),
        )
        cat.create_namespace_if_not_exists(_NAMESPACE)
        return cat

    def _load(self, data_dir: Path, table: str):
        """The Iceberg table, or ``None`` if this line has no such table yet (pre-Iceberg Source, or a
        table never written)."""
        from pyiceberg.exceptions import NoSuchTableError

        if not (Path(data_dir) / "catalog.json").exists():
            return None
        cat = self._catalog(data_dir)
        try:
            return cat.load_table(f"{_NAMESPACE}.{table}")
        except NoSuchTableError:
            return None

    # ─── write ──────────────────────────────────────────────────────────────────

    def export(self, con, data_dir: Path, *, mode: str = "overwrite", f=None) -> None:
        from . import trickle_io
        from .dataplane import _check_mode, publish_plan

        _check_mode(mode)
        data_dir = Path(data_dir)
        # Validates the publish set (Trickle tables exempt from the reserved-column check), writes the
        # Trickle mode/PK sidecar, and returns the tables to commit — all before any write.
        tables = publish_plan(con, data_dir)
        # Flat-Parquet sidecar first (also the consistent fallback if the Iceberg commit fails).
        self._parquet.export(con, data_dir, mode=mode, f=f)
        # Stamp _duckstring_f (a TIMESTAMPTZ) as UTC for Arrow: pyiceberg accepts only UTC-tz timestamps,
        # and a registry written under a local session tz would otherwise fetch as e.g. tz=Australia/…
        con.execute("SET TimeZone='UTC'")
        cat = self._catalog(data_dir)
        meta = trickle_io.read_meta(con)
        for table in tables:
            if self._is_incremental(table, meta):
                # A Trickle history/changelog grows by **append** — commit only this run's new rows as one
                # `_duckstring_f`-homogeneous data file, so the window read prunes by manifest stats. The
                # clean merge *main* and plain Ripple output stay overwrite (see _commit).
                self._append_commit(cat, table, con, f)
            else:
                arrow = con.execute(f'SELECT * FROM "{table}"').fetch_arrow_table()
                self._commit(cat, table, arrow, f)

    @staticmethod
    def _is_incremental(table: str, meta: dict) -> bool:
        """A Trickle append history, or any merge Trickle's ``__changelog`` companion — the append-only
        tables. A merge *main* (``meta[table]['mode'] == 'merge'``) is overwrite, not incremental."""
        from .trickle_io import CHANGELOG_SUFFIX

        if meta.get(table, {}).get("mode") == "append":
            return True
        return table.endswith(CHANGELOG_SUFFIX) and table[: -len(CHANGELOG_SUFFIX)] in meta

    def _append_commit(self, cat, table: str, con, f) -> None:
        from pyiceberg.exceptions import NoSuchTableError

        from .trickle_io import F_COL

        ident = f"{_NAMESPACE}.{table}"
        try:
            tbl = cat.load_table(ident)
        except NoSuchTableError:
            tbl = None

        if tbl is None:
            arrow = con.execute(f'SELECT * FROM "{table}"').fetch_arrow_table()
            tbl = cat.create_table(ident, schema=arrow.schema)
            if arrow.num_rows:
                tbl.append(arrow, snapshot_properties=self._props(f))
                self._set_props(tbl, **{LAST_F_PROP: f.isoformat()})
            self._sync_retention(cat.load_table(ident), table, con)
            return
        # Only the rows newer than the last committed run — its LAST_F_PROP cursor (a *table* property, not
        # the snapshot summary, so a retention delete-snapshot below can't clobber it). For append/changelog
        # that cursor equals the max _duckstring_f committed, so a replay at the same f appends nothing.
        last = tbl.properties.get(LAST_F_PROP)
        where = f"WHERE {F_COL} > TIMESTAMPTZ '{last}'" if last else ""
        arrow = con.execute(f'SELECT * FROM "{table}" {where}').fetch_arrow_table()
        if arrow.num_rows:
            tbl.append(arrow, snapshot_properties=self._props(f))
            self._set_props(tbl, **{LAST_F_PROP: f.isoformat()})
        self._sync_retention(cat.load_table(ident), table, con)

    def _sync_retention(self, tbl, table: str, con) -> None:
        """Mirror the registry's retention into the Iceberg history: the registry table was already
        trimmed (``trickle_io._apply_retention``), so anything below its ``min(_duckstring_f)`` is expired
        here too. Files are ``_duckstring_f``-homogeneous, so the delete drops whole files (metadata-only,
        no Iceberg delete-files). A ``duckstring.floor`` property gates it so a no-retention run is a
        cheap no-op. Space-only — correctness rides the consumer's window read + full-read fallback."""
        from datetime import datetime

        from .trickle_io import F_COL

        reg_floor = con.execute(f'SELECT min({F_COL}) FROM "{table}"').fetchone()[0]
        if reg_floor is None:
            return
        stored = tbl.properties.get(FLOOR_PROP)
        if stored is None:  # first observation — record the floor, nothing to drop yet
            self._set_props(tbl, **{FLOOR_PROP: reg_floor.isoformat()})
            return
        if reg_floor <= datetime.fromisoformat(stored):
            return  # retention hasn't advanced the floor → nothing newly expired
        tbl.delete(f"{F_COL} < '{reg_floor.isoformat()}'")  # drops whole expired files (metadata-only)
        self._set_props(tbl, **{FLOOR_PROP: reg_floor.isoformat()})

    @staticmethod
    def _set_props(tbl, **props) -> None:
        with tbl.transaction() as txn:
            txn.set_properties(**props)

    @staticmethod
    def _props(f) -> dict:
        return {F_PROP: f.isoformat()} if f is not None else {}

    def _commit(self, cat, table: str, arrow, f) -> None:
        import warnings

        from pyiceberg.exceptions import NoSuchTableError

        ident = f"{_NAMESPACE}.{table}"
        props = {F_PROP: f.isoformat()} if f is not None else {}

        def _create():
            cat.create_namespace_if_not_exists(_NAMESPACE)
            return cat.create_table(ident, schema=arrow.schema)

        def _overwrite(tbl):
            with warnings.catch_warnings():
                # Overwriting a fresh/empty table warns "Delete operation did not match any records" —
                # expected on every first write of an overwrite Ripple; suppress the noise.
                warnings.filterwarnings("ignore", message="Delete operation did not match any records")
                tbl.overwrite(arrow, snapshot_properties=props)

        try:
            tbl = cat.load_table(ident)
        except NoSuchTableError:
            tbl = _create()

        try:
            _overwrite(tbl)
        except Exception:
            # A Ripple is overwrite-per-run; if the output schema changed since the table was created,
            # overwrite can't reconcile it. Recreate the table at the new schema (snapshot history is a
            # Phase-2/Trickle concern; an overwrite Ripple keeps no history anyway).
            cat.drop_table(ident)
            _overwrite(_create())

    # ─── read ──────────────────────────────────────────────────────────────────

    def prepare(self, con) -> None:
        try:
            con.execute("LOAD iceberg")
        except Exception:
            con.execute("INSTALL iceberg")
            con.execute("LOAD iceberg")

    def read_select(self, data_dir: Path, table: str, *, as_of=None) -> str:
        tbl = self._load(data_dir, table)
        if tbl is None:
            # Transitional: a Source that hasn't re-exported to Iceberg → read its legacy flat Parquet.
            return self._parquet.read_select(data_dir, table, as_of=as_of)
        ml = tbl.metadata_location.replace("'", "''")
        snap = self._snapshot_for(tbl, as_of) if as_of is not None else None
        if snap is not None:
            return f"SELECT * FROM iceberg_scan('{ml}', snapshot_from_id => {snap})"
        return f"SELECT * FROM iceberg_scan('{ml}')"

    def _snapshot_for(self, tbl, as_of):
        """The id of the latest snapshot whose stamped ``f`` is ``<= as_of`` — the as-of read seam. None
        when no snapshot is eligible (consumer's freshness predates the Source's first run)."""
        from datetime import datetime

        eligible = []
        for s in tbl.snapshots():
            stamp = s.summary.additional_properties.get(F_PROP) if s.summary else None
            if stamp and datetime.fromisoformat(stamp) <= as_of:
                eligible.append((stamp, s.snapshot_id))
        if not eligible:
            return None
        return max(eligible)[1]

    def list_tables(self, data_dir: Path) -> list[str]:
        # The flat sidecar is written for every published table, so its listing is the publish set.
        return self._parquet.list_tables(data_dir)

    def table_path(self, data_dir: Path, table: str) -> Path | None:
        return self._parquet.table_path(data_dir, table)
