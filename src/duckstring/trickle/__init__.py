"""Trickle — a self-contained DBSP-style incremental engine over DuckDB.

Z-sets, an epoch-stamped changelog, incremental joins (a DAG of binary affected-key recomputes), and
incremental aggregation — see ``plans/trickle-dbsp.md`` and ``plans/trickle-dag.md`` for the design, and
``docs/docs/incremental-theory.md`` for the underlying theory.

This subpackage depends on **nothing else in Duckstring**; its only host seam is :class:`.context.Context`
(a DuckDB connection + a stable epoch + source reads). It is kept this way so it can be lifted into its own
distribution at any time — see the note in the repo ``CLAUDE.md``. The legacy module paths
``duckstring.trickle_io`` / ``duckstring.trickle_builder`` / ``duckstring.agg`` remain as thin compatibility
aliases that forward here.
"""

from __future__ import annotations

from . import agg, builder, context, io
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
    table_parts,
    unique_name,
    write_sidecar,
)

__all__ = [
    # host seam
    "Context", "NEVER", "SYSTEM_PREFIX",
    # builder
    "TrickleBuilder", "BuildError", "agg",
    # io: classes + the write/read API
    "Delta", "DeltaError",
    "append_table", "append_zset", "apply_zset", "merge_table", "apply_aggregate",
    "checkpoint", "reconstruct_current", "reconstruct_sql", "current_state",
    "read_delta", "read_registry_delta", "read_meta", "normalize_pk", "changelog_name", "unique_name",
    # io: sidecar / cross-catchment draw (per-run parts)
    "write_sidecar", "load_sidecar", "landed_after", "incremental_tables",
    "part_name", "part_f", "table_parts", "part_tables",
    # io: system-column / table-name constants
    "F_COL", "D_COL", "CHANGELOG_SUFFIX", "DROPLOG_SUFFIX", "AGG_STATE_PREFIX", "META_TABLE", "SIDECAR",
    # submodules
    "io", "builder", "context",
]
