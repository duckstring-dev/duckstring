"""The Iceberg data-plane backend (``DUCKSTRING_DATA_PLANE=iceberg``, ``duckstring[iceberg]`` extra).

An Apache Iceberg base layer over the Parquet files we already write: it adds **snapshots** (one
overwrite commit per Pond Run, stamped with the run's freshness ``f``) and **schema metadata**, the
substrate Phase 2 contracts and the later Trickle work build on. The data files stay Parquet — this is
a metadata/catalog layer, not a file-format swap.

Layout (per ``name@major`` line, so the major-line isolation ``ponds/{name}/m{major}/`` already gives
is preserved physically, not just by namespace — and there is no shared catalog for concurrent Ducks
to contend on):

- a pyiceberg ``SqlCatalog`` (SQLite) at ``{data_dir}/catalog.db``, warehouse rooted at ``data_dir``;
- one namespace, ``pond`` — the catalog is already isolated to one line, so the table is ``pond.{table}``.

Writes go through pyiceberg (Arrow ``overwrite``); reads go through DuckDB's ``iceberg`` extension
(``iceberg_scan`` on the snapshot's metadata file). A **flat ``{table}.parquet`` copy is written
alongside** each commit: it keeps the unchanged consumers working behaviour-neutrally — the duct/draw
file transfer, the direct file-serve, and the transitional read of a Source that hasn't re-exported to
Iceberg yet. The ``catalog.db`` (a ``*.db`` file) and the Iceberg metadata/data under ``data_dir`` are
included in ``catchment archive`` by the existing root walk (download while quiescent).
"""

from __future__ import annotations

import time
from pathlib import Path

from .dataplane import (
    DataPlane,
    ParquetDataPlane,
    registry_tables,
    validate_publish,
)

_NAMESPACE = "pond"  # the single namespace within each per-line catalog
F_PROP = "duckstring.f"  # snapshot summary property carrying the Pond Run's freshness


def _retry(fn, attempts: int = 12, base: float = 0.05):
    """Retry a catalog op on a transient SQLite lock (a sink reading a Source's catalog while its Duck
    commits) — queue and back off rather than fail. Re-raises anything that isn't a lock after the last
    attempt."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - narrow to the lock message below
            if "locked" not in str(exc).lower() or i == attempts - 1:
                raise
            time.sleep(min(base * (2**i), 0.5))


class IcebergDataPlane(DataPlane):
    def __init__(self) -> None:
        # The flat-Parquet sidecar: the compat copy for draws, direct-serve, and the legacy fallback.
        self._parquet = ParquetDataPlane()

    # ─── catalog ──────────────────────────────────────────────────────────────

    def _catalog(self, data_dir: Path):
        from pyiceberg.catalog.sql import SqlCatalog

        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        cat = SqlCatalog(
            "duckstring",
            uri=f"sqlite:///{data_dir / 'catalog.db'}",
            warehouse=data_dir.as_uri(),
        )
        cat.create_namespace_if_not_exists(_NAMESPACE)
        return cat

    def _load(self, data_dir: Path, table: str):
        """The Iceberg table, or ``None`` if this line has no such table yet (pre-Iceberg Source, or a
        table never written)."""
        from pyiceberg.exceptions import NoSuchTableError

        if not (Path(data_dir) / "catalog.db").exists():
            return None
        cat = self._catalog(data_dir)
        try:
            return _retry(lambda: cat.load_table(f"{_NAMESPACE}.{table}"))
        except NoSuchTableError:
            return None

    # ─── write ──────────────────────────────────────────────────────────────────

    def export(self, con, data_dir: Path, *, mode: str = "overwrite", f=None) -> None:
        from .dataplane import _check_mode

        _check_mode(mode)
        data_dir = Path(data_dir)
        tables = registry_tables(con)
        for table in tables:
            validate_publish(con, table)  # reject reserved _duckstring_* columns before any write
        # Flat-Parquet sidecar first (also the consistent fallback if the Iceberg commit fails).
        self._parquet.export(con, data_dir, mode=mode, f=f)
        cat = self._catalog(data_dir)
        for table in tables:
            arrow = con.execute(f'SELECT * FROM "{table}"').fetch_arrow_table()
            self._commit(cat, table, arrow, f)

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
                _retry(lambda: tbl.overwrite(arrow, snapshot_properties=props))

        try:
            tbl = _retry(lambda: cat.load_table(ident))
        except NoSuchTableError:
            tbl = _retry(_create)

        try:
            _overwrite(tbl)
        except Exception:
            # A Ripple is overwrite-per-run; if the output schema changed since the table was created,
            # overwrite can't reconcile it. Recreate the table at the new schema (snapshot history is a
            # Phase-2/Trickle concern; an overwrite Ripple keeps no history anyway).
            _retry(lambda: cat.drop_table(ident))
            _overwrite(_retry(_create))

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
