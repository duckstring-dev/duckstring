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

# Snapshot/metadata retention. Iceberg accrues a new data file + manifests + snapshot + metadata.json per
# commit, and pyiceberg 0.11 expires snapshots from the *metadata* only (it leaves the files on disk), so
# without an explicit prune every Pond Run leaks an overwrite table's previous full copy plus a pile of
# manifest avros, forever. We keep the most-recent N snapshots (the as-of read seam can still reach that
# far back) and reclaim everything no surviving snapshot references. The metadata.json files are bounded
# by pyiceberg itself via the cleanup properties below, set at table creation.
_DEFAULT_KEEP_SNAPSHOTS = 5
_CLEANUP_PROPS = {  # let pyiceberg delete superseded metadata.json files itself
    "write.metadata.delete-after-commit.enabled": "true",
    "write.metadata.previous-versions-max": str(_DEFAULT_KEEP_SNAPSHOTS),
}


def _keep_snapshots() -> int:
    import os

    try:
        return max(1, int(os.environ.get("DUCKSTRING_ICEBERG_KEEP_SNAPSHOTS", _DEFAULT_KEEP_SNAPSHOTS)))
    except ValueError:
        return _DEFAULT_KEEP_SNAPSHOTS


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
        tables = publish_plan(con, data_dir, f)
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
        """A Trickle append history, or a Trickle's ``__changelog`` / ``__droplog`` companion — the
        append-only tables. A merge *main* (``meta[table]['mode'] == 'merge'``) is overwrite, not
        incremental."""
        from .trickle_io import CHANGELOG_SUFFIX, DROPLOG_SUFFIX

        if meta.get(table, {}).get("mode") == "append":
            return True
        for suffix in (CHANGELOG_SUFFIX, DROPLOG_SUFFIX):
            if table.endswith(suffix) and table[: -len(suffix)] in meta:
                return True
        return False

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
            tbl = self._create_table(cat, ident, arrow.schema)
            if arrow.num_rows:
                tbl.append(arrow, snapshot_properties=self._props(f))
                self._set_props(tbl, **{LAST_F_PROP: f.isoformat()})
            self._sync_retention(cat.load_table(ident), table, con)
            self._prune(cat, table)
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
        self._prune(cat, table)

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
            return self._create_table(cat, ident, arrow.schema)

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
        # Overwrite leaves the prior run's full data file referenced only by the now-superseded snapshot —
        # reclaim it (and the stale manifests) once that snapshot ages out.
        self._prune(cat, table)

    @staticmethod
    def _create_table(cat, ident: str, schema):
        """Create the table with pyiceberg's metadata-cleanup properties set, so superseded
        ``*.metadata.json`` files are deleted on each commit rather than accumulating forever."""
        return cat.create_table(ident, schema=schema, properties=dict(_CLEANUP_PROPS))

    # ─── prune (bound on-disk growth) ─────────────────────────────────────────────

    def _prune(self, cat, table: str) -> None:
        """Keep only the most-recent ``_keep_snapshots()`` snapshots and physically remove any data /
        manifest files no surviving snapshot references. Space-only — correctness rides the current
        snapshot (always retained) and the consumer's window read — so any failure here is swallowed: a
        Pond Run must never fail on housekeeping. An overwrite table sheds its old full data files; an
        append history / changelog keeps every data file its *current* snapshot still references (the row
        data is trimmed separately by :meth:`_sync_retention`), shedding only stale manifests/metadata."""
        try:
            ident = f"{_NAMESPACE}.{table}"
            tbl = cat.load_table(ident)
            keep = _keep_snapshots()
            snaps = sorted(tbl.snapshots(), key=lambda s: s.timestamp_ms)
            if len(snaps) > keep:
                # Drop the oldest; the current snapshot is the newest, so it's never in this set.
                expire = [s.snapshot_id for s in snaps[:-keep]]
                tbl.maintenance.expire_snapshots().by_ids(expire).commit()
                tbl = cat.load_table(ident)
            self._gc_orphan_files(tbl)
        except Exception:  # pragma: no cover - housekeeping must never break a run
            import logging

            logging.getLogger(__name__).debug("iceberg prune skipped for %s", table, exc_info=True)

    @staticmethod
    def _gc_orphan_files(tbl) -> None:
        """Delete files under the table's ``data/`` and ``metadata/`` dirs that no surviving snapshot (or
        the retained metadata log / current metadata pointer) references — the orphans left behind when a
        snapshot is expired (pyiceberg 0.11 expires metadata only, never the files)."""
        import os
        from urllib.parse import unquote, urlparse

        def _norm(p) -> str:
            u = urlparse(str(p))
            return os.path.abspath(unquote(u.path) if u.scheme in ("file", "") else str(p))

        io = tbl.io
        live: set[str] = {_norm(tbl.metadata_location)}
        for entry in tbl.metadata.metadata_log:
            live.add(_norm(entry.metadata_file))
        for snap in tbl.snapshots():
            if snap.manifest_list:
                live.add(_norm(snap.manifest_list))
            for mf in snap.manifests(io):
                live.add(_norm(mf.manifest_path))
                for e in mf.fetch_manifest_entry(io, discard_deleted=False):
                    live.add(_norm(e.data_file.file_path))

        table_dir = os.path.dirname(os.path.dirname(_norm(tbl.metadata_location)))  # .../{namespace}/{table}
        for sub in ("data", "metadata"):
            dpath = os.path.join(table_dir, sub)
            if not os.path.isdir(dpath):
                continue
            for fn in os.listdir(dpath):
                fp = os.path.abspath(os.path.join(dpath, fn))
                if os.path.isfile(fp) and fp not in live:
                    try:
                        os.remove(fp)
                    except OSError:
                        pass

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
        """The id of the last-committed snapshot whose stamped ``f`` is ``<= as_of`` — the as-of read seam.
        None when no snapshot is eligible (consumer's freshness predates the Source's first run).

        ``tbl.snapshots()`` yields in commit order. One overwrite commit produces TWO snapshots stamped with
        the *same* ``f`` — a DELETE then an APPEND — so ties must NOT break on ``snapshot_id`` (random): that
        can resolve to the empty DELETE. Pick the latest in commit order (``timestamp_ms`` then position) =
        the final committed state at that freshness."""
        from datetime import datetime

        eligible = []
        for i, s in enumerate(tbl.snapshots()):  # commit order
            stamp = s.summary.additional_properties.get(F_PROP) if s.summary else None
            if stamp and datetime.fromisoformat(stamp) <= as_of:
                eligible.append((s.timestamp_ms, i, s.snapshot_id))
        if not eligible:
            return None
        return max(eligible)[2]

    def list_tables(self, data_dir: Path) -> list[str]:
        # The flat sidecar is written for every published table, so its listing is the publish set.
        return self._parquet.list_tables(data_dir)

    def table_path(self, data_dir: Path, table: str) -> Path | None:
        return self._parquet.table_path(data_dir, table)
