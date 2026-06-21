"""The **data plane** — how a Pond *publishes* its tables for, and *reads* them from, other Ponds.

This is the cross-Pond interchange layer, distinct from the DuckDB registry where Ripples compute.
Today it is whole-table Parquet replace (overwrite-per-run); the :class:`DataPlane` interface is the
seam an Iceberg snapshot/catalog backend slots into later (see ``plans/data-plane-iceberg.md``)
*without touching call sites*. It already carries the shape that work needs:

- a write ``mode`` — ``"overwrite"`` now; ``"append"`` / ``"merge"`` are **reserved** for Trickle and
  raise until implemented, so call sites route a mode rather than baking overwrite in;
- a per-run freshness stamp ``f`` — a no-op against plain Parquet (no snapshot metadata), but the hook
  an Iceberg backend records on each snapshot so a run is resolvable from its freshness;
- the reserved ``_duckstring_*`` system-column namespace, rejected at write so future framework columns
  (``_duckstring_f`` and siblings) can be claimed without a later breaking rename.
"""

from __future__ import annotations

from pathlib import Path

# System columns are framework-owned and persisted; the WHOLE prefix is reserved (not a single name),
# leaving room for siblings (``_duckstring_f`` for freshness, ``_duckstring_d`` for the Z-set weight, …).
# The Trickle subpackage owns this namespace (its system columns live in it); re-exported here so the data
# plane and Trickle share a single source of truth (see duckstring/trickle/context.py).
from .trickle.context import SYSTEM_PREFIX as RESERVED_PREFIX  # noqa: E402

# Write modes the interface can express. Only ``overwrite`` is implemented in Phase 1; the others are
# the history-preserving Trickle write paths, reserved here so call sites don't hard-code a mode.
WRITE_MODES = ("overwrite", "append", "merge")


class ReservedColumnError(ValueError):
    """A published table carries a column in the reserved ``_duckstring_*`` namespace."""


class DataPlane:
    """The cross-Pond data interchange contract. Backends implement publish (``export``) and consume
    (``read_select`` / ``list_tables`` / ``table_path``)."""

    def export(self, con, data_dir: Path, *, mode: str = "overwrite", f=None) -> None:
        """Publish every table in ``con``'s registry to ``data_dir`` for cross-Pond consumption.

        ``mode`` selects the write semantic (only ``"overwrite"`` in Phase 1). ``f`` is the run's
        freshness, recorded by backends that snapshot. Rejects any table carrying a reserved
        (``_duckstring_*``) column."""
        raise NotImplementedError

    def prepare(self, con) -> None:
        """Make ``con`` able to read this backend's published tables (e.g. load a DuckDB extension).
        Idempotent; a no-op for the Parquet backend. Call once before using ``read_select`` on ``con``."""

    def read_select(self, data_dir: Path, table: str, *, as_of=None) -> str:
        """A DuckDB ``SELECT`` over a published Source ``table``, for registering as a view or relation.
        ``as_of`` (a freshness) is the **as-of read seam**: the Source snapshot whose ``f <= as_of``;
        ``None`` reads the latest. Raises :class:`FileNotFoundError` when the Source has not published it yet.

        A **merge Trickle main** is log-structured (a base + the ``__changelog``), so it is *reconstructed*
        here (latest-per-PK over the base ⊎ the changelog newer than the fold watermark ``f_base``); every
        other table is a direct physical read (:meth:`_raw_read_select`)."""
        from .trickle.io import load_sidecar

        meta = load_sidecar(data_dir).get(table, {})
        if meta.get("mode") == "merge":
            return self._reconstruct_select(data_dir, table, meta, as_of)
        return self._raw_read_select(data_dir, table, as_of=as_of)

    def _raw_read_select(self, data_dir: Path, table: str, *, as_of=None) -> str:
        """A direct physical ``SELECT`` over a published table (no reconstruction) — the backend's storage
        read. Used as-is for append/overwrite tables and as the base/changelog operands of a merge read."""
        raise NotImplementedError

    def _reconstruct_select(self, data_dir: Path, table: str, meta: dict, as_of=None) -> str:
        """Reconstruct a merge main's current state from its base + ``__changelog`` (both read via
        :meth:`_raw_read_select`), per :func:`duckstring.trickle.io.reconstruct_sql`."""
        from datetime import datetime

        from .trickle.io import changelog_name, reconstruct_sql

        try:
            clog_sql = self._raw_read_select(data_dir, changelog_name(table), as_of=as_of)
        except FileNotFoundError:
            clog_sql = None
        try:
            base_sql = self._raw_read_select(data_dir, table, as_of=as_of)
        except FileNotFoundError:
            base_sql = None
        if clog_sql is None:
            if base_sql is None:
                raise FileNotFoundError(str(Path(data_dir) / table))
            return base_sql
        f_base = datetime.fromisoformat(meta["f_base"]) if meta.get("f_base") else None
        return reconstruct_sql(base_sql, clog_sql, f_base, tuple(meta.get("pk", ())), upper=as_of)

    def list_tables(self, data_dir: Path) -> list[str]:
        """The names of the tables a Pond has published into ``data_dir``."""
        raise NotImplementedError

    def table_path(self, data_dir: Path, table: str) -> Path | None:
        """The single on-disk artifact for ``table``, for backends that have one (Parquet) — used to
        serve a file directly. ``None`` for backends without one (a query path must be used instead)."""
        return None


def _check_mode(mode: str) -> None:
    if mode == "overwrite":
        return
    if mode in WRITE_MODES:
        raise NotImplementedError(
            f"write mode {mode!r} is reserved for Trickle (history-preserving append/merge) and is not "
            f"implemented yet — Ripples write 'overwrite'"
        )
    raise ValueError(f"unknown write mode {mode!r} (expected one of {', '.join(WRITE_MODES)})")


def _reserved_columns(con, table: str) -> list[str]:
    # DESCRIBE's first column is the column name; flag any in the reserved namespace.
    return [
        row[0] for row in con.execute(f'DESCRIBE "{table}"').fetchall()
        if str(row[0]).startswith(RESERVED_PREFIX)
    ]


def registry_tables(con) -> list[str]:
    """The table names a Pond has written into ``con``'s registry — the publish set. Tables in the
    reserved ``_duckstring_*`` namespace (Trickle's mode/PK meta) are framework-internal, never published.

    Only **base tables** count: a Pond's real output is always a table (``write_table`` create+rename, or
    a Trickle main/changelog), never a view. ``read_table("source.table")`` registers each foreign Source
    as a same-named *view* so SQL can ``FROM table`` it — those must NOT be published (``SHOW TABLES``
    lists views too, so this filtered ``duckdb_tables()`` query is what stops a Pond from re-exporting a
    full copy of every Source it reads)."""
    return [
        t for (t,) in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main' ORDER BY table_name"
        ).fetchall() if not t.startswith(RESERVED_PREFIX)
    ]


def validate_publish(con, table: str) -> None:
    """Reject a table carrying a column in the reserved ``_duckstring_*`` namespace (framework-owned)."""
    reserved = _reserved_columns(con, table)
    if reserved:
        raise ReservedColumnError(
            f"table '{table}' has column(s) {', '.join(reserved)} in the reserved "
            f"'{RESERVED_PREFIX}*' namespace — these names are framework-owned; rename them"
        )


def publish_plan(con, data_dir: Path, f=None) -> list[str]:
    """Validate the publish set, write the ``_trickle.json`` sidecar, and return the tables to publish.

    Trickle tables (a clean *main* + its ``__changelog`` Z-set companion) legitimately carry
    ``_duckstring_*`` system columns, so they are exempt from the reserved-column check that guards plain
    overwrite output. The sidecar carries one entry per published *base* table — ``{mode, pk, floor}`` for
    a Trickle, ``{mode: "overwrite"}`` for plain output — each stamped with this run's freshness ``f`` so a
    cross-Pond reader can resolve a Trickle's coverage *and* detect whether an overwrite source advanced
    (its ``f`` vs the consumer's ``previous_f``). Call this *before* writing anything so a reserved-column
    violation aborts the whole publish (last-good left intact)."""
    from datetime import timezone

    from . import trickle_io as trickle

    meta = trickle.read_meta(con)
    changelogs = {trickle.changelog_name(t) for t in meta}
    droplogs = {f"{t}{trickle.DROPLOG_SUFFIX}" for t in meta}
    tables = registry_tables(con)
    f_iso = f.astimezone(timezone.utc).isoformat() if f is not None else None
    payload: dict[str, dict] = {}
    for table in tables:
        if table in meta or table in changelogs or table in droplogs:
            continue  # Trickle base/companion — base added below; the __changelog/__droplog companions are
            #            exported as files (they carry reserved system columns) but take no sidecar entry.
        validate_publish(con, table)
        payload[table] = {"mode": "overwrite", "f": f_iso}
    for base, m in meta.items():
        entry = {"mode": m["mode"], "pk": list(m["pk"]), "floor": m.get("floor"), "f": f_iso}
        if m["mode"] == "merge":
            entry["f_base"] = m.get("f_base")  # fold watermark — the read reconstructs base ⊎ changelog>f_base
        payload[base] = entry
    trickle.write_sidecar(data_dir, payload)
    return tables


class ParquetDataPlane(DataPlane):
    """The zero-dependency default. A plain overwrite output is one ``{table}.parquet`` file, overwritten
    per run. An **append-only** Trickle table (append history, ``__changelog``, ``__droplog``) is a
    *directory* of per-run parts ``{table}/{f}.parquet`` (O(change) per run). A **merge main** is
    log-structured: its ``__changelog`` publishes per run (parts) and its base ``{table}.parquet`` is
    rewritten only at a **checkpoint** (when the changelog since the fold watermark outgrows the base, past
    ``DUCKSTRING_COMPACT_THRESHOLD``); reads reconstruct base ⊎ changelog (see :meth:`DataPlane.read_select`)."""

    def export(self, con, data_dir: Path, *, mode: str = "overwrite", f=None) -> None:
        from . import trickle_io as trickle
        from .core import retry_on_lock

        _check_mode(mode)
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)

        tables = publish_plan(con, data_dir, f)
        meta = trickle.read_meta(con)
        incremental = trickle.incremental_tables(meta) if f is not None else set()
        merge_mains = {t for t, m in meta.items() if m.get("mode") == "merge"}

        def _export() -> None:
            for table in tables:
                if table in incremental:
                    _export_parts(con, data_dir, table, f)
                elif table in merge_mains:
                    continue  # the base is published only at a checkpoint (below), not per run
                else:
                    dest = data_dir / f"{table}.parquet"
                    tmp = data_dir / f"{table}.parquet.tmp"
                    con.execute(f'COPY "{table}" TO \'{tmp}\' (FORMAT PARQUET)')
                    tmp.replace(dest)
            for main in merge_mains:
                _checkpoint_and_publish_base(con, data_dir, main, f)
            if merge_mains:  # a checkpoint may have advanced f_base → refresh the sidecar
                publish_plan(con, data_dir, f)

        retry_on_lock(_export)

    def _raw_read_select(self, data_dir: Path, table: str, *, as_of=None) -> str:
        from . import trickle_io as trickle

        if trickle.table_parts(data_dir, table):  # append-only parts directory → union the parts
            glob = str(Path(data_dir) / table / "*.parquet").replace("'", "''")
            sel = f"SELECT * FROM read_parquet('{glob}')"
            # As-of read seam: keep only rows at/under the requested freshness. Parts are
            # `_duckstring_f`-homogeneous and stat-pruned by DuckDB, so this drops whole part files.
            if as_of is not None:
                sel += f' WHERE "{trickle.F_COL}" <= {trickle._ts(as_of)}'
            return sel
        pq = Path(data_dir) / f"{table}.parquet"
        if not pq.exists():
            raise FileNotFoundError(str(pq))
        return f"SELECT * FROM read_parquet('{str(pq).replace(chr(39), chr(39) * 2)}')"

    def list_tables(self, data_dir: Path) -> list[str]:
        from . import trickle_io as trickle

        data_dir = Path(data_dir)
        if not data_dir.exists():
            return []
        files = {pq.stem for pq in data_dir.glob("*.parquet")}
        parts = set(trickle.part_tables(data_dir))
        # A merge main is reconstructed from its changelog; it is a published table even before its base
        # exists (no checkpoint yet → no `{main}.parquet`), so surface it from the sidecar.
        mains = {t for t, m in trickle.load_sidecar(data_dir).items() if m.get("mode") == "merge"}
        return sorted(files | parts | mains)

    def table_path(self, data_dir: Path, table: str) -> Path | None:
        d = Path(data_dir) / table
        if d.is_dir():
            return d  # an append-only parts directory
        return Path(data_dir) / f"{table}.parquet"


def _export_parts(con, data_dir: Path, table: str, f) -> None:
    """Publish an append-only ``table`` as a directory of per-run Parquet parts. Writes one
    ``_duckstring_f``-homogeneous file per registry freshness not already on disk (so a normal run writes
    just its new slice, and a rebuild/restore backfills any missing parts), and drops parts whose freshness
    is no longer in the registry (mirroring retention). Idempotent on replay.

    When the table is **empty** (a bootstrap-only changelog with no rows yet), a schema-only marker part is
    still written at the run's ``f`` so the table stays readable as an empty relation — a consumer covered
    by the floor then sees an *empty* delta, not a coverage-miss full read."""
    from . import trickle_io as trickle

    part_dir = Path(data_dir) / table
    part_dir.mkdir(parents=True, exist_ok=True)
    reg_fs = {r[0] for r in con.execute(f'SELECT DISTINCT "{trickle.F_COL}" FROM "{table}"').fetchall()
              if r[0] is not None}
    if not reg_fs and f is not None:
        reg_fs = {f}  # synthesize a 0-row marker part (the `WHERE = f` below selects nothing → empty part)
    existing = {trickle.part_f(p.name): p for p in part_dir.glob("*.parquet")}
    # Write the parts not yet on disk; *always* (re)write the current run's f, whose content may have changed
    # this run (a same-f re-merge, or a replay) — older f's are immutable history and are skipped if present.
    to_write = (reg_fs - set(existing)) | ({f} if (f is not None and f in reg_fs) else set())
    for fi in to_write:
        dest = part_dir / trickle.part_name(fi)
        tmp = part_dir / (dest.name + ".tmp")
        con.execute(
            f'COPY (SELECT * FROM "{table}" WHERE "{trickle.F_COL}" = {trickle._ts(fi)}) '
            f"TO '{str(tmp).replace(chr(39), chr(39) * 2)}' (FORMAT PARQUET)"
        )
        tmp.replace(dest)
    for fi, p in existing.items():  # drop parts the registry no longer retains (or the superseded marker)
        if fi not in reg_fs:
            p.unlink()


def _compact_threshold() -> int:
    """The catchment-level checkpoint floor / target base-chunk size in bytes (``DUCKSTRING_COMPACT_THRESHOLD``,
    default 256 MiB). A merge main checkpoints when its changelog-since-the-fold-watermark outgrows
    ``max(base size, this)`` — so it never checkpoints below this, and otherwise at ~k=1 (changelog ≥ base)."""
    import os

    return int(os.environ.get("DUCKSTRING_COMPACT_THRESHOLD", str(256 * 1024 * 1024)))


def _checkpoint_and_publish_base(con, data_dir: Path, main: str, f) -> None:
    """For a merge main: trigger a checkpoint when the published changelog parts newer than the fold
    watermark outgrow the base (past the floor), then — only if a checkpoint folded — republish the base
    ``{main}.parquet`` from the registry and re-prune the changelog parts (retention may have trimmed)."""
    from . import trickle_io as trickle

    clog = trickle.changelog_name(main)
    f_base = trickle._f_base(con, main)
    clog_bytes = sum(
        p.stat().st_size for p in trickle.table_parts(data_dir, clog)
        if f_base is None or trickle.part_f(p.name) > f_base
    )
    base_pq = data_dir / f"{main}.parquet"
    base_bytes = base_pq.stat().st_size if base_pq.exists() else 0
    if clog_bytes < max(base_bytes, _compact_threshold()):
        return
    trickle.checkpoint(con, main, f)  # registry fold: rewrites the base, advances f_base, applies retention
    if trickle._table_exists(con, main):
        tmp = base_pq.with_suffix(".parquet.tmp")
        con.execute(f'COPY "{main}" TO \'{tmp}\' (FORMAT PARQUET)')
        tmp.replace(base_pq)
    _export_parts(con, data_dir, clog, f)  # re-sync the changelog parts after any retention trim


def get_data_plane() -> DataPlane:
    """The active data-plane backend, selected by ``DUCKSTRING_DATA_PLANE``:

    - ``iceberg`` (default) — the Apache Iceberg base layer (snapshots + schema metadata over the
      Parquet data files); its deps are in core, so it's available out of the box;
    - ``parquet`` — the whole-table Parquet plane, the opt-out for the lightest footprint or for an
      offline Catchment that can't fetch DuckDB's iceberg extension.

    Iceberg is the default because the version-contract (schema) and incremental work build on its
    metadata; ``parquet`` stays a first-class fallback."""
    import os

    backend = os.environ.get("DUCKSTRING_DATA_PLANE", "iceberg").lower()
    if backend == "parquet":
        return ParquetDataPlane()
    if backend == "iceberg":
        try:
            from .iceberg_plane import IcebergDataPlane
        except ImportError as exc:  # pragma: no cover - pyiceberg is a core dep, but guard a stripped install
            raise NotImplementedError(
                "the iceberg data plane needs pyiceberg (a core dependency) — reinstall duckstring, "
                "or set DUCKSTRING_DATA_PLANE=parquet for the lighter plane"
            ) from exc
        return IcebergDataPlane()
    raise ValueError(
        f"unknown DUCKSTRING_DATA_PLANE {backend!r} (expected 'iceberg' or 'parquet')"
    )
