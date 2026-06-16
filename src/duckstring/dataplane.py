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
# leaving room for siblings (``_duckstring_f`` for freshness, ``_duckstring_op`` for merge, …).
RESERVED_PREFIX = "_duckstring_"

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

    def read_select(self, data_dir: Path, table: str) -> str:
        """A DuckDB ``SELECT`` over a published Source ``table``, for registering as a view or relation.
        Raises :class:`FileNotFoundError` when the Source has not published that table yet."""
        raise NotImplementedError

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


class ParquetDataPlane(DataPlane):
    """The zero-dependency default: each table is one ``{table}.parquet`` file, written atomically
    (tmp + replace) and overwritten wholesale per run."""

    def export(self, con, data_dir: Path, *, mode: str = "overwrite", f=None) -> None:
        from .core import retry_on_lock

        _check_mode(mode)
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)

        def _export() -> None:
            for (table,) in con.execute("SHOW TABLES").fetchall():
                reserved = _reserved_columns(con, table)
                if reserved:
                    raise ReservedColumnError(
                        f"table '{table}' has column(s) {', '.join(reserved)} in the reserved "
                        f"'{RESERVED_PREFIX}*' namespace — these names are framework-owned; rename them"
                    )
                dest = data_dir / f"{table}.parquet"
                tmp = data_dir / f"{table}.parquet.tmp"
                con.execute(f'COPY "{table}" TO \'{tmp}\' (FORMAT PARQUET)')
                tmp.replace(dest)

        retry_on_lock(_export)

    def read_select(self, data_dir: Path, table: str) -> str:
        pq = self.table_path(data_dir, table)
        if pq is None or not pq.exists():
            raise FileNotFoundError(str(Path(data_dir) / f"{table}.parquet"))
        return f"SELECT * FROM read_parquet('{str(pq).replace(chr(39), chr(39) * 2)}')"

    def list_tables(self, data_dir: Path) -> list[str]:
        data_dir = Path(data_dir)
        if not data_dir.exists():
            return []
        return sorted(pq.stem for pq in data_dir.glob("*.parquet"))

    def table_path(self, data_dir: Path, table: str) -> Path | None:
        return Path(data_dir) / f"{table}.parquet"


def get_data_plane() -> DataPlane:
    """The active data-plane backend. The selection seam for the future Iceberg backend: today only
    the Parquet default exists; ``DUCKSTRING_DATA_PLANE`` is honoured so an Iceberg backend can be
    opted into without changing call sites."""
    import os

    backend = os.environ.get("DUCKSTRING_DATA_PLANE", "parquet").lower()
    if backend == "parquet":
        return ParquetDataPlane()
    raise NotImplementedError(
        f"data plane {backend!r} is not available — the Iceberg backend is not implemented yet "
        f"(see plans/data-plane-iceberg.md); unset DUCKSTRING_DATA_PLANE or set it to 'parquet'"
    )
