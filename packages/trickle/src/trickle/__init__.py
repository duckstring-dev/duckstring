"""Trickle: incremental joins and aggregates for DuckDB (the ``duckstring-trickle`` distribution).

A DBSP-style incremental engine: tables carry their changes as Z-sets (rows weighted +1 or -1) in an
epoch-stamped changelog, and the builder maintains joins (a DAG of binary joins recomputed over the changed
keys), aggregates and ordered scans from those changes. The design notes are in Duckstring's repository
(``plans/trickle-dbsp.md``, ``plans/trickle-dag.md``) and the user documentation at docs.duckstring.com.

The engine reaches its host only through :class:`.context.Context`. Two hosts exist: :func:`run` /
:func:`at` here (one DuckDB database; :mod:`.session`) and Duckstring's ``Pond``, which reads Sources from
published Parquet. Nothing here imports Duckstring; Duckstring imports this (``duckstring.trickle`` is an
alias of this package).
"""

from __future__ import annotations

from . import acc, agg, builder, context, io, session
from .builder import BuildError, TrickleBuilder
from .context import NEVER, SYSTEM_PREFIX, Context
from .io import (
    AGG_STATE_PREFIX,
    CHANGELOG_SUFFIX,
    D_COL,
    DROPLOG_SUFFIX,
    F_COL,
    META_TABLE,
    SIDECAR,
    Delta,
    DeltaError,
    append_table,
    append_zset,
    apply_aggregate,
    apply_zset,
    changelog_name,
    checkpoint,
    current_state,
    incremental_tables,
    landed_after,
    load_sidecar,
    merge_table,
    normalize_pk,
    part_f,
    part_name,
    part_tables,
    read_delta,
    read_meta,
    read_registry_delta,
    reconstruct_current,
    reconstruct_sql,
    sidecar_tables,
    table_parts,
    unique_name,
    write_sidecar,
)
from .session import Epoch, Run, at, run

__all__ = [
    # host seam
    "Context", "NEVER", "SYSTEM_PREFIX",
    # builder
    "TrickleBuilder", "BuildError", "agg", "acc",
    # the standalone host
    "run", "Run", "at", "Epoch",
    # io: classes + the write/read API
    "Delta", "DeltaError",
    "append_table", "append_zset", "apply_zset", "merge_table", "apply_aggregate",
    "checkpoint", "reconstruct_current", "reconstruct_sql", "current_state",
    "read_delta", "read_registry_delta", "read_meta", "normalize_pk", "changelog_name", "unique_name",
    # io: sidecar / cross-catchment draw (per-run parts)
    "write_sidecar", "load_sidecar", "sidecar_tables", "landed_after", "incremental_tables",
    "part_name", "part_f", "table_parts", "part_tables",
    # io: system-column / table-name constants
    "F_COL", "D_COL", "CHANGELOG_SUFFIX", "DROPLOG_SUFFIX", "AGG_STATE_PREFIX", "META_TABLE", "SIDECAR",
    # submodules
    "io", "builder", "context", "session",
]
