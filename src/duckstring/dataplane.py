"""The **data plane** — how a Pond *publishes* its tables for, and *reads* them from, other Ponds.

This is the cross-Pond interchange layer, distinct from the DuckDB registry where Ripples compute. Every
published table is Parquet under the line's data directory (local or an object store, via
:mod:`duckstring.storage`), with a ``_trickle.json`` sidecar describing each table:

- a plain **overwrite** table is a directory of immutable versions, ``{table}__v/{f}.parquet``, one per run,
  so a Sink run reads the version it is pinned to while the Source publishes the next
  (``plans/versioned-overwrite.md``; retention in :func:`prune_versions`);
- an **append-only** Trickle table (append history, ``__changelog``, ``__droplog``) is a directory of per-run
  parts, ``{table}/{f}.parquet``;
- a **merge** Trickle main is log-structured: hot changelog parts, warm ``__band/`` bands and a chunked cold
  ``__base/``, reconstructed on read.

The reserved ``_duckstring_*`` system-column namespace is rejected on plain output at publish.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .storage import LocalStorage, Storage, get_storage
from .trickle_io import first_row


def _as_storage(data_dir) -> Storage:
    """Coerce a ``data_dir`` to a :class:`~duckstring.storage.Storage`. Accepts a ``Storage`` (the
    runtime passes one, possibly object-store-backed) **or** a local ``Path``/str (tests and standalone
    callers), so every data-plane entry point works with either."""
    return data_dir if isinstance(data_dir, Storage) else get_storage(data_dir)

# System columns are framework-owned and persisted; the WHOLE prefix is reserved (not a single name),
# leaving room for siblings (``_duckstring_f`` for freshness, ``_duckstring_d`` for the Z-set weight, …).
# The Trickle subpackage owns this namespace (its system columns live in it); re-exported here so the data
# plane and Trickle share a single source of truth (see duckstring/trickle/context.py).
from .trickle.context import SYSTEM_PREFIX as RESERVED_PREFIX  # noqa: E402

# Write modes the interface can express. Only ``overwrite`` is implemented in Phase 1; the others are
# the history-preserving Trickle write paths, reserved here so call sites don't hard-code a mode.
WRITE_MODES = ("overwrite", "append", "merge")


# ``export(retain_from=KEEP_ALL)``: publish without pruning any overwrite version (tests, standalone
# callers). The runtime always passes a real ``retain_from`` (see :func:`prune_versions`).
KEEP_ALL = object()


class ReservedColumnError(ValueError):
    """A published table carries a column in the reserved ``_duckstring_*`` namespace."""


class DataPlane:
    """The cross-Pond data interchange contract. Backends implement publish (``export``) and consume
    (``read_select`` / ``list_tables`` / ``table_path``)."""

    def export(self, con, data_dir: Path, *, mode: str = "overwrite", f=None, retain_from=KEEP_ALL,
               held=frozenset()) -> None:
        """Publish every table in ``con``'s registry to ``data_dir`` for cross-Pond consumption.

        ``mode`` selects the write semantic (only ``"overwrite"`` in Phase 1). ``f`` is the run's
        freshness, which names the overwrite versions this publish writes. ``retain_from`` is the
        Catchment's retention bound for superseded versions (see :func:`prune_versions`). ``held`` names
        tables the run checked as unchanged, which keep their published copy. Rejects any table carrying a
        reserved (``_duckstring_*``) column."""
        raise NotImplementedError

    def read_select(self, data_dir: Path, table: str, *, as_of=None, pin=None, con=None) -> str:
        """A DuckDB ``SELECT`` over a published Source ``table``, for registering as a view or relation.
        ``as_of`` (a freshness) is the **as-of read seam**: the Source snapshot whose ``f <= as_of``;
        ``None`` reads the latest. ``pin`` overrides ``as_of`` for an **overwrite** table: it is the Source's
        published freshness when the reading run started, so the run reads the version it was scheduled on
        (a Trickle table stays bounded by ``as_of``, which its delta windows depend on). Raises
        :class:`FileNotFoundError` when the Source has not published it yet.

        A **merge Trickle main** is log-structured (a base + the ``__changelog``), so it is *reconstructed*
        here (latest-per-PK over the base ⊎ the changelog newer than the fold watermark ``f_base``); every
        other table is a direct physical read (:meth:`_raw_read_select`). Given ``con``, the connection that
        will run the query, the changelog's consolidation is written to a temporary table on it first, which
        is what a read that will be joined wants (:func:`~duckstring.trickle.io.materialise_reconstruct`)."""
        from .trickle.io import check_format_version, load_sidecar

        data_dir = _as_storage(data_dir)
        sidecar = load_sidecar(data_dir)
        meta = sidecar.get(table, {})
        check_format_version(meta.get("format_version"), table)
        mode = meta.get("mode")
        if mode == "merge":
            return self._reconstruct_select(data_dir, table, meta, as_of, con=con, sidecar=sidecar)
        if mode == "append":  # never versioned: skip that probe
            return self._flat_read_select(data_dir, table, as_of=as_of, sidecar=sidecar)
        return self._raw_read_select(data_dir, table, as_of=pin if pin is not None else as_of, sidecar=sidecar)

    def _raw_read_select(self, data_dir: Path, table: str, *, as_of=None, sidecar=None) -> str:
        """A direct physical ``SELECT`` over a published table (no reconstruction): an overwrite table's
        version, else :meth:`_flat_read_select`."""
        raise NotImplementedError

    def _flat_read_select(self, data_dir: Path, table: str, *, as_of=None, sidecar=None) -> str:
        """A physical read of an operand that is never versioned: a merge main's cold base and its
        ``__changelog`` / ``__band`` companions, and an append-only table. Skipping the versions probe saves
        a listing per operand, which is a round trip on an object store."""
        return self._raw_read_select(data_dir, table, as_of=as_of, sidecar=sidecar)

    def _reconstruct_select(self, data_dir: Path, table: str, meta: dict, as_of=None, con=None,
                            sidecar=None) -> str:
        """Reconstruct a merge main's current state as of ``as_of`` from its read snapshot
        (:func:`merge_snapshot`): the cold base it picks, its warm bands and its hot change log parts, per
        :func:`duckstring.trickle.io.reconstruct_sql`. The bands and the hot parts are disjoint in freshness,
        so their union is the change log above the base's watermark with no double-count."""
        from .trickle.io import load_sidecar, materialise_reconstruct, reconstruct_sql

        data_dir = _as_storage(data_dir)
        sidecar = load_sidecar(data_dir) if sidecar is None else sidecar
        snap = merge_snapshot(data_dir, table, sidecar, as_of)
        base_sql = _read_parquet_files(snap["base"], as_of) if snap["base"] else None
        clog = snap["bands"] + snap["hot"]
        if not clog:
            if base_sql is None:
                raise FileNotFoundError(data_dir.uri(table))
            return base_sql
        clog_sql = _read_parquet_files(clog, as_of)
        pk = tuple(meta.get("pk", ()))
        if con is not None:
            return materialise_reconstruct(con, base_sql, clog_sql, snap["f_base"], pk, upper=as_of)
        return reconstruct_sql(base_sql, clog_sql, snap["f_base"], pk, upper=as_of)

    def consolidated_count_select(self, data_dir: Path, table: str, meta: dict, as_of=None, pin=None) -> str:
        """A scalar ``SELECT`` for the merge main's **current-state row count**, computed *without scanning the
        base data*: ``count(cold base)`` (Parquet metadata, no scan) **+** the net Z-set weight
        ``sum(_duckstring_d)`` of the read snapshot's bands and hot parts. For a valid merge log each present
        row nets to weight ``+1``, an update to ``0`` and a delete to ``-1``, so this equals ``count(*)`` over
        :meth:`_reconstruct_select`, as metadata plus a small delta scan instead of a full base scan."""
        from .trickle.io import D_COL, F_COL, _ts, load_sidecar

        data_dir = _as_storage(data_dir)
        if meta.get("mode") != "merge":  # an overwrite version or an append parts dir: count the read itself
            return f"SELECT count(*) FROM ({self.read_select(data_dir, table, as_of=as_of, pin=pin)})"
        snap = merge_snapshot(data_dir, table, load_sidecar(data_dir), as_of)
        base_cnt = f"(SELECT count(*) FROM ({_read_parquet_files(snap['base'], as_of)}))" if snap["base"] else "0"
        clog = snap["bands"] + snap["hot"]
        if not clog:
            return f"SELECT {base_cnt}"
        lo = f' WHERE "{F_COL}" > {_ts(snap["f_base"])}' if snap["f_base"] is not None else ""
        delta = f'(SELECT coalesce(sum("{D_COL}"), 0) FROM ({_read_parquet_files(clog, as_of)}){lo})'
        return f"SELECT {base_cnt} + {delta}"

    def list_tables(self, data_dir) -> list[str]:
        """The names of the tables a Pond has published into ``data_dir``."""
        raise NotImplementedError

    def table_path(self, data_dir, table: str) -> Path | None:
        """The single on-disk artifact for ``table`` (a local ``Path``) — used to serve a file directly.
        ``None`` when the data plane is on an object store (no local path; use :meth:`files_for`)."""
        data_dir = _as_storage(data_dir)
        if not isinstance(data_dir, LocalStorage):
            return None
        from . import trickle_io as trickle

        versions = trickle.table_versions(data_dir, table)
        if versions:
            return data_dir.root / trickle.version_dir_name(table) / versions[-1]  # the newest version
        d = data_dir.root / table
        if d.is_dir():
            return d  # an append-only parts directory
        base = data_dir.root / trickle.base_dir_name(table)
        if base.is_dir():
            return base  # a log-structured merge-main base (chunk directory)
        return data_dir.root / f"{table}.parquet"

    def files_for(self, data_dir, table: str) -> list[tuple[tuple[str, ...], str]]:
        """The published files comprising ``table`` as ``(storage_parts, arcname)`` pairs — for serving the
        raw Parquet (an overwrite table's newest version as ``{table}.parquet``, an append-only parts
        directory, or a merge-main base chunk dir) over the duct/ripple routes, **independent of the storage
        backend**. The caller reads each file's bytes via ``data_dir.read_bytes(*storage_parts)`` and writes
        it into a zip under ``arcname``."""
        data_dir = _as_storage(data_dir)
        from . import trickle_io as trickle

        versions = trickle.table_versions(data_dir, table)
        if versions:
            return [((trickle.version_dir_name(table), versions[-1]), f"{table}.parquet")]
        parts = trickle.table_parts(data_dir, table)
        if parts or trickle.base_chunks(data_dir, table):
            sidecar = trickle.load_sidecar(data_dir)
        if parts:  # the committed snapshot's parts (a later Run's, or superseded ones, stay out)
            return [((table, n), f"{table}/{n}") for n in parts_snapshot(data_dir, table, sidecar)]
        if trickle.base_chunks(data_dir, table):
            bd = trickle.base_dir_name(table)
            return [((bd, n), f"{bd}/{n}") for n in merge_snapshot(data_dir, table, sidecar)["base_names"]]
        if data_dir.exists(f"{table}.parquet"):
            return [((f"{table}.parquet",), f"{table}.parquet")]
        return []


def _read_parquet_glob(glob: str, as_of=None) -> str:
    """A ``SELECT`` over a glob of Parquet parts/chunks, optionally clamped to the **as-of** freshness
    (``_duckstring_f <= as_of``) — a row-level predicate DuckDB stat-prunes to whole files where the parts
    are ``_duckstring_f``-homogeneous (the append/changelog case) and a partial scan otherwise (a base
    chunk spans a freshness range).

    ``union_by_name`` is **required**: the parts in a directory are written independently across runs (and
    across redeploys/rebuilds), so their column order can drift. Without it ``read_parquet`` aligns a glob
    **positionally** (first file's schema wins), which silently reads a user column's values into the
    ``_duckstring_d`` slot — a corruption that surfaces as an absurd ``SUM(_duckstring_d)`` (BIGINT overflow)
    on the reconstruct. By name, columns always bind correctly and a missing column reads as NULL."""
    from .trickle.io import F_COL, _ts

    sel = f"SELECT * FROM read_parquet('{glob}', union_by_name=true)"
    if as_of is not None:
        sel += f' WHERE "{F_COL}" <= {_ts(as_of)}'
    return sel


def _read_parquet_files(uris: list[str], as_of=None) -> str:
    """A ``SELECT`` over an explicit list of Parquet files, clamped to ``as_of`` like
    :func:`_read_parquet_glob`. A read names its files when it is planned, so a file a later publish adds
    (another Run's part, a new base) never joins it."""
    from .trickle.io import F_COL, _ts

    files = ", ".join(f"'{u}'" for u in uris)
    sel = f"SELECT * FROM read_parquet([{files}], union_by_name=true)"
    if as_of is not None:
        sel += f' WHERE "{F_COL}" <= {_ts(as_of)}'
    return sel


# ─── read snapshots (plans/trickle-insert-only.md, Stage 0) ──────────────────────
#
# Pond Runs pipeline, so a Source can publish its next Run (and fold or compact a merge main) while a Sink
# run at an earlier freshness is still reading it. Every read therefore resolves to a fixed set of immutable
# files when it's planned, chosen from the sidecar (the commit marker: written after the files it declares)
# and the read's freshness, and a superseded file is deleted only once no in-flight run can need it
# (:func:`gc_tiers`, bounded by the Catchment's ``retain_from``).
#
# For a merge main read as of ``a`` (clamped to the sidecar's run stamp): the newest base token at or below
# ``a``, the bands above that token up to ``a``, and the change log parts above the newest of those bands up
# to ``a``. Folded parts, bands and base chunks a later fold or compaction superseded are still on disk until
# the bound passes, so a reader behind those operations reads exactly its state.


def _run_upper(sidecar: dict, as_of=None):
    """The newest freshness a read may see: ``as_of`` clamped to the sidecar's run stamp (a part or base
    stamped later is a publish still in progress)."""
    from datetime import datetime

    from .trickle.io import RUN_KEY

    run = (sidecar.get(RUN_KEY) or {}).get("f")
    committed = datetime.fromisoformat(run) if run else None
    if as_of is None:
        return committed
    return as_of if committed is None else min(as_of, committed)


def _base_tokens(store, table: str) -> dict:
    """A merge main's published base chunks grouped by checkpoint token (its freshness)."""
    from .trickle.io import base_chunks, part_f

    tokens: dict = {}
    for n in base_chunks(store, table):
        try:
            tokens.setdefault(part_f(n), []).append(n)
        except ValueError:
            tokens.setdefault(None, []).append(n)  # unparseable: part of every snapshot, never collected
    return tokens


def _pick_token(tokens: dict, upper, f_base):
    """The base token a read at ``upper`` uses: the newest at or below it. With none (a read older than every
    retained base), the newest at or below the sidecar's ``f_base``, else none."""
    dated = sorted(t for t in tokens if t is not None)
    eligible = [t for t in dated if upper is None or t <= upper]
    if eligible:
        return eligible[-1]
    if f_base is not None:
        older = [t for t in dated if t <= f_base]
        if older:
            import logging

            logging.getLogger(__name__).warning(
                "no published base at or below %s; reading the base at %s", upper, older[-1])
            return older[-1]
    return None


def merge_snapshot(store, table: str, sidecar: dict, as_of=None) -> dict:
    """The files a read of merge main ``table`` as of ``as_of`` uses (see the section note): ``base`` (file
    URIs), ``f_base`` (the base's token, the reconstruct's fold watermark), ``bands`` and ``hot`` (change log
    part URIs, disjoint in freshness)."""
    from datetime import datetime

    from .trickle.io import base_dir_name, changelog_name, part_f, table_parts, warm_name

    store = _as_storage(store)
    entry = sidecar.get(table) or {}
    upper = _run_upper(sidecar, as_of)
    f_base_entry = datetime.fromisoformat(entry["f_base"]) if entry.get("f_base") else None
    tokens = _base_tokens(store, table)
    token = _pick_token(tokens, upper, f_base_entry)
    bdir = base_dir_name(table)
    base = [store.uri(bdir, n) for n in tokens.get(token, []) + tokens.get(None, [])] if token is not None else []
    if not base and not tokens and store.exists(f"{table}.parquet"):
        base = [store.uri(f"{table}.parquet")]  # a legacy single-file base
    if token is None and not base and tokens.get(None):
        base = [store.uri(bdir, n) for n in tokens[None]]

    def within(f, lo):
        return (lo is None or f > lo) and (upper is None or f <= upper)

    warm = warm_name(table)
    band_names = [n for n in table_parts(store, warm) if within(part_f(n), token)]
    f_warm = max((part_f(n) for n in band_names), default=token)
    clog = changelog_name(table)
    hot_names = [n for n in table_parts(store, clog) if within(part_f(n), f_warm)]
    hot = [store.uri(clog, n) for n in hot_names]
    bands = [store.uri(warm, n) for n in band_names]
    for name, uris in ((clog, hot), (warm, bands)):  # an unstamped publish writes a tier as one file
        if not table_parts(store, name) and store.exists(f"{name}.parquet"):
            uris.append(store.uri(f"{name}.parquet"))
    return {"base": base, "f_base": token, "bands": bands, "hot": hot, "f_warm": f_warm,
            "base_names": tokens.get(token, []) if token is not None else [],
            "band_names": band_names, "hot_names": hot_names}


def parts_snapshot(store, table: str, sidecar: dict, as_of=None) -> list[str]:
    """The part names a read of an append-only parts directory as of ``as_of`` uses: those at or below the
    read's freshness and, for a reader at or past the table's floor, at or above it (parts below the floor are
    retention's or a rebuild's drops, kept on disk only for readers still behind it)."""
    from datetime import datetime

    from .trickle.io import CHANGELOG_SUFFIX, WARM_SUFFIX, base_table_name, part_f, table_parts

    store = _as_storage(store)
    upper = _run_upper(sidecar, as_of)
    names = [n for n in table_parts(store, table) if upper is None or part_f(n) <= upper]
    if table.endswith((CHANGELOG_SUFFIX, WARM_SUFFIX)):
        return names  # a merge main's tiers: chosen by merge_snapshot, or windowed by a delta read
    floor_iso = (sidecar.get(base_table_name(table)) or {}).get("floor")
    floor = datetime.fromisoformat(floor_iso) if floor_iso else None
    if floor is not None and (upper is None or floor <= upper):
        names = [n for n in names if part_f(n) >= floor]
    return names


def gc_tiers(store, retain_from=KEEP_ALL) -> int:
    """Delete the published Trickle files no read at or after ``retain_from`` can use: base chunks of tokens
    older than the base a read at ``retain_from`` picks, bands at or below that token, change log parts at or
    below that read's newest band (and its floor), and append parts below the floor once ``retain_from``
    has passed it. ``KEEP_ALL`` (a standalone publish, with no in-flight readers to protect) and ``None`` (no
    reader bound) collect up to the newest publish. Run after the sidecar is committed. Returns the number
    of files removed."""
    from datetime import datetime

    from .trickle.io import (
        CHANGELOG_SUFFIX,
        DROPLOG_SUFFIX,
        base_dir_name,
        changelog_name,
        load_sidecar,
        part_f,
        sidecar_tables,
        table_parts,
        warm_name,
    )

    store = _as_storage(store)
    sidecar = load_sidecar(store)
    bound = _run_upper(sidecar, None if retain_from is KEEP_ALL else retain_from)
    removed = 0
    for table, entry in sidecar_tables(sidecar).items():
        mode = entry.get("mode")
        if mode == "merge":
            snap = merge_snapshot(store, table, sidecar, bound)
            token, f_warm = snap["f_base"], snap["f_warm"]
            if token is None:
                continue  # no base yet at the bound: nothing is superseded
            bdir = base_dir_name(table)
            for t, names in _base_tokens(store, table).items():
                if t is not None and t < token:
                    for n in names:
                        store.remove(bdir, n)
                        removed += 1
            if store.exists(f"{table}.parquet") and snap["base_names"]:
                store.remove(f"{table}.parquet")  # a legacy single-file base, superseded by chunks
                removed += 1
            warm = warm_name(table)
            bands = table_parts(store, warm)
            for n in bands:
                if part_f(n) <= token:
                    store.remove(warm, n)
                    removed += 1
            if bands and not table_parts(store, warm) and store.is_dir(warm):
                store.rmtree(warm)  # every band is in the base: no warm tier left
            floor_iso = entry.get("floor")
            floor = datetime.fromisoformat(floor_iso) if floor_iso else None
            clog = changelog_name(table)
            cut = f_warm if floor is None else min(f_warm, floor)
            newest = _run_upper(sidecar, None)
            for n in table_parts(store, clog):
                # Never the newest Run's part: at a bootstrap it is the empty marker at the floor, which keeps a
                # covered consumer's delta empty rather than a coverage miss (a fold never takes it either).
                if part_f(n) <= cut and part_f(n) != newest:
                    store.remove(clog, n)
                    removed += 1
        if mode in ("append", "merge"):
            floor_iso = entry.get("floor")
            floor = datetime.fromisoformat(floor_iso) if floor_iso else None
            if floor is None or (bound is not None and bound < floor):
                continue  # a reader may still be behind the floor
            dirs = [table] if mode == "append" else []
            dirs.append(f"{table}{DROPLOG_SUFFIX}")
            for d in dirs:
                if d.endswith(CHANGELOG_SUFFIX):
                    continue
                for n in table_parts(store, d):
                    if part_f(n) < floor:
                        store.remove(d, n)
                        removed += 1
    return removed


def select_version(names: list[str], at, *, table: str = "") -> str:
    """The version to read from an overwrite table's sorted version ``names``: the newest with
    ``f <= at``, or the newest overall when ``at`` is ``None``. When no version qualifies (the pinned one
    was pruned, or the Source's first version postdates ``at``), the newest is read and a warning logged:
    a run never fails for it."""
    from .trickle.io import part_f

    if at is None:
        return names[-1]
    eligible = [n for n in names if part_f(n) <= at]
    if eligible:
        return eligible[-1]
    import logging

    logging.getLogger(__name__).warning(
        "no published version of %r at or before %s; reading the newest (%s)", table, at.isoformat(), names[-1])
    return names[-1]


def retained_versions(names: list[str], retain_from) -> set[str]:
    """Which of an overwrite table's sorted version ``names`` retention keeps: the newest, every version
    newer than ``retain_from``, and the version ``retain_from`` itself resolves to (the newest with
    ``f <= retain_from``). ``retain_from=None`` keeps only the newest."""
    from .trickle.io import part_f

    if not names:
        return set()
    keep = {names[-1]}
    if retain_from is None:
        return keep
    at_or_below = [n for n in names if part_f(n) <= retain_from]
    if at_or_below:
        keep.add(at_or_below[-1])
    keep.update(n for n in names if part_f(n) > retain_from)
    return keep


def prune_versions(data_dir, retain_from, tables=None) -> int:
    """Delete the overwrite versions no reader can still need, and record each table's oldest retained
    version as its sidecar ``v_floor`` (the explicit watermark the Persist mirror prunes by). Returns the
    number of files removed.

    ``retain_from`` comes from the Catchment (``begin_run`` and ``shutdown`` jobs): the oldest Source pin
    held by an in-flight Sink run of this line, lowered to this line's own published freshness when the
    job was built. So a version is kept while a running Sink reads it, and a version a Sink could still be
    dispatched onto (the newest at job time, or one published by an overlapping run since) is never
    removed. ``None`` keeps only the newest. Housekeeping: an individual delete failure is skipped."""
    from datetime import timezone

    from . import trickle_io as trickle

    store = _as_storage(data_dir)
    sidecar = trickle.load_sidecar(store)
    removed = 0
    changed = False
    for table, entry in trickle.sidecar_tables(sidecar).items():
        if entry.get("mode", "overwrite") != "overwrite" or (tables is not None and table not in tables):
            continue
        names = trickle.table_versions(store, table)
        if not names:
            continue
        keep = retained_versions(names, retain_from)
        vstore = store.child(trickle.version_dir_name(table))
        for n in names:
            if n not in keep:
                try:
                    vstore.remove(n)
                    removed += 1
                except Exception:  # pragma: no cover - housekeeping never fails a run
                    pass
        floor = trickle.part_f(min(keep)).astimezone(timezone.utc).isoformat()
        if entry.get("v_floor") != floor:
            entry["v_floor"] = floor
            changed = True
    if changed:
        trickle.write_sidecar(store, sidecar)
    return removed


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
    full copy of every Source it reads). Only the registry's own database counts: a database a Ripple
    attaches to read from (another DuckDB file, SQLite, Postgres) is never republished."""
    return [
        t for (t,) in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main' "
            "AND database_name = current_database() ORDER BY table_name"
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


def publish_plan(con, data_dir: Path, f=None, held=frozenset(), *, write: bool = True) -> list[str]:
    """Validate the publish set, write the ``_trickle.json`` sidecar, and return the tables to publish.

    Trickle tables (a clean *main* + its ``__changelog`` Z-set companion) legitimately carry
    ``_duckstring_*`` system columns, so they are exempt from the reserved-column check that guards plain
    overwrite output. The sidecar carries one entry per published *base* table — ``{mode, pk, floor}`` for
    a Trickle, ``{mode: "overwrite"}`` for plain output — each stamped with this run's freshness ``f`` so a
    cross-Pond reader can resolve a Trickle's coverage *and* detect whether an overwrite source advanced
    (its ``f`` vs the consumer's ``previous_f``). Call this *before* writing anything so a reserved-column
    violation aborts the whole publish (last-good left intact).

    An overwrite table in ``held`` (checked unchanged this run, see plans/change-detection.md) keeps its
    prior entry, so its ``f`` stays at the run that last changed it; :func:`held_tables` says which can be.
    An overwrite entry carries the table's fingerprint (``fp``) when its last write recorded one. The
    sidecar's run stamp (:data:`~duckstring.trickle.io.RUN_KEY`) records this publish's ``f``, which a held
    entry no longer shows."""
    from datetime import timezone

    from . import trickle_io as trickle

    meta = trickle.read_meta(con)
    changelogs = {trickle.changelog_name(t) for t in meta}
    droplogs = {f"{t}{trickle.DROPLOG_SUFFIX}" for t in meta}
    warms = {trickle.warm_name(t) for t in meta}
    colds = {trickle.cold_base_name(t) for t, m in meta.items() if m.get("mode") == "merge"}
    tables = registry_tables(con)
    f_iso = f.astimezone(timezone.utc).isoformat() if f is not None else None
    existing = trickle.load_sidecar(data_dir)
    kept = held_tables(data_dir, held, existing)
    fps = registry_fingerprints(con)
    payload: dict[str, dict] = {}
    for table in tables:
        if table in meta or table in changelogs or table in droplogs or table in warms or table in colds:
            continue  # Trickle base/companion — base added below; the __changelog/__band/__droplog
            #            companions are exported as files (reserved system columns) but take no sidecar entry.
        validate_publish(con, table)
        if table in kept:
            payload[table] = dict(existing[table])  # unchanged: the published version and its stamp stand
            continue
        payload[table] = {"mode": "overwrite", "f": f_iso}
        if table in fps:
            payload[table]["fp"] = fps[table]
        prior = existing.get(table)
        if isinstance(prior, dict) and prior.get("v_floor"):
            payload[table]["v_floor"] = prior["v_floor"]  # the retention watermark (see prune_versions)
    for base, m in meta.items():
        entry = {"mode": m["mode"], "pk": list(m["pk"]), "floor": m.get("floor"), "f": f_iso,
                 "format_version": m["format_version"]}
        if m["mode"] == "merge":
            entry["f_base"] = m.get("f_base")  # fold watermark — the read reconstructs base ⊎ changelog>f_base
        payload[base] = entry
    # Objects persist until overwritten (not per-run declared), so carry their sidecar section forward —
    # this run's staged Object commits fold their fresh entries in afterwards (see objects.commit_objects).
    existing_objects = existing.get("objects")
    if existing_objects:
        payload["objects"] = existing_objects
    if existing.get("state"):  # the state companions' snapshots, until _export_companions records this run's
        payload["state"] = existing["state"]
    if f_iso is not None:
        payload[trickle.RUN_KEY] = {"f": f_iso}
    if write:  # the commit marker: an export validates first (write=False) and commits after its files
        trickle.write_sidecar(data_dir, payload)
    return tables


def held_tables(data_dir, held, sidecar=None) -> set[str]:
    """Which of ``held`` can be held at publish: overwrite tables with a published version to keep. A name
    without one (never published, or an Object) is published as usual."""
    from . import trickle_io as trickle

    if not held:
        return set()
    entries = trickle.sidecar_tables(trickle.load_sidecar(data_dir) if sidecar is None else sidecar)
    return {t for t in held if entries.get(t, {}).get("mode", "overwrite") == "overwrite" and t in entries
            and trickle.table_versions(data_dir, t)}


# ─── change detection for plain tables (plans/change-detection.md) ─────────────
#
# An overwrite table can carry a fingerprint: an order-independent digest of its content and schema. The
# published one (the sidecar entry's ``fp``) is the baseline a new value is compared with. The registry keeps
# its own copy in FP_TABLE, so a check can also tell whether the registry still holds the published content
# (a failed run leaves its writes in the registry without publishing them).

FP_TABLE = f"{RESERVED_PREFIX}fp"
# The fingerprint format. DuckDB's hash() isn't guaranteed stable across versions, so the tag carries the
# DuckDB major.minor: after an upgrade no fingerprint matches, which costs one republish per table.
_FP_FORMAT = "fp1"


def _fp_tag() -> str:
    import duckdb

    return f"{_FP_FORMAT}-duckdb{'.'.join(duckdb.__version__.split('.')[:2])}"


def fingerprint(relation) -> str:
    """A digest of ``relation``'s content and schema: the row count and the sum of every row's hash
    (independent of row order, and counting duplicate rows), combined with the column names and types in
    order and a format tag. Costs one scan. Floating-point edge cases (``-0.0``, NaN payloads) follow
    DuckDB's ``hash``."""
    import hashlib
    import json

    count, total = first_row(relation.aggregate("count(*), sum(hash(*COLUMNS(*))::HUGEINT)"))
    schema = [[c, str(t)] for c, t in zip(relation.columns, relation.types, strict=True)]
    tag = _fp_tag()
    digest = hashlib.sha256(json.dumps([tag, schema, int(count), str(total or 0)]).encode()).hexdigest()
    return f"{tag}:{digest}"


def _ensure_fp_table(con) -> None:
    con.execute(f'CREATE TABLE IF NOT EXISTS "{FP_TABLE}" (table_name VARCHAR PRIMARY KEY, fp VARCHAR, f TIMESTAMPTZ)')


def registry_fingerprints(con) -> dict[str, str]:
    """``{table: fp}`` for the registry tables whose last write recorded a fingerprint."""
    from . import trickle_io as trickle

    if not trickle._table_exists(con, FP_TABLE):
        return {}
    return dict(con.execute(f'SELECT table_name, fp FROM "{FP_TABLE}"').fetchall())


def set_registry_fingerprint(con, table: str, fp: str | None, f=None) -> None:
    """Record (or, with ``fp=None``, clear) ``table``'s registry fingerprint. Call inside the transaction
    that writes the table, so the two never disagree."""
    con.execute(f'DELETE FROM "{FP_TABLE}" WHERE table_name = ?', [table])
    if fp is not None:
        con.execute(f'INSERT INTO "{FP_TABLE}" VALUES (?, ?, ?)', [table, fp, f])


def unpublish_table(data_dir, name: str) -> None:
    """Remove a table's **published** collection from ``data_dir`` — every physical form (wholesale
    ``{name}.parquet``, the per-run parts dir ``{name}/``, the ``__changelog`` / ``__band`` / ``__base`` /
    ``__droplog`` companion dirs) **and** its sidecar entry. The registry side is dropped separately
    (:func:`duckstring.trickle.io.drop_table`); together they are one table delete (see plans/deletes.md).
    Idempotent — missing forms are skipped."""
    from . import trickle_io as trickle

    store = _as_storage(data_dir)
    store.remove(f"{name}.parquet")
    for d in (name, trickle.version_dir_name(name), trickle.changelog_name(name), trickle.warm_name(name),
              trickle.base_dir_name(name), f"{name}{trickle.DROPLOG_SUFFIX}"):
        store.rmtree(d)
    sidecar = trickle.load_sidecar(store)
    if name in sidecar:
        del sidecar[name]
        trickle.write_sidecar(store, sidecar)


class ParquetDataPlane(DataPlane):
    """The zero-dependency default. A plain overwrite output is published as **versions**: each run writes
    one immutable ``{table}__v/{f}.parquet``, and a read selects the version its run is pinned to (see
    :func:`prune_versions` for retention). An **append-only** Trickle table (append history,
    ``__changelog``, ``__droplog``) is a *directory* of per-run parts ``{table}/{f}.parquet`` (O(change)
    per run). A **merge main** is log-structured: its ``__changelog`` publishes per run (parts) and its
    base chunks ``{table}__base/`` are rewritten only at a **checkpoint** (when the changelog since the
    fold watermark outgrows the base, past ``DUCKSTRING_COMPACT_THRESHOLD``); reads reconstruct
    base ⊎ changelog (see :meth:`DataPlane.read_select`)."""

    def export(self, con, data_dir, *, mode: str = "overwrite", f=None, retain_from=KEEP_ALL,
               held=frozenset()) -> None:
        """See :meth:`DataPlane.export`. ``retain_from`` drives version retention after the publish (see
        :func:`prune_versions`); the default keeps every version. ``held`` names the overwrite tables this run
        checked as unchanged: they keep their published version and sidecar entry (:func:`publish_plan`)."""
        from datetime import datetime, timezone

        from . import trickle_io as trickle
        from .core import retry_on_lock

        _check_mode(mode)
        data_dir = _as_storage(data_dir)
        data_dir.duckdb_setup(con)  # object store → load httpfs + register the credential secret (no-op local)
        data_dir.mkdir()

        kept = held_tables(data_dir, held)  # unchanged overwrite tables: no new version, entry carried
        # A merge main's bootstrap is written straight to its cold base (trickle.io._bootstrapping): publish
        # it before the sidecar advertises its f_base, so a reader never sees a base that isn't there yet.
        for main, m in trickle.read_meta(con).items():
            if m.get("mode") == "merge":
                retry_on_lock(lambda main=main: _publish_local_base(con, data_dir, main))
        # Validate now (a reserved-column violation aborts before anything is written); the sidecar, the
        # commit marker every read snapshot is chosen from, is written after the files it declares.
        tables = publish_plan(con, data_dir, f, held=kept, write=False)
        meta = trickle.read_meta(con)
        incremental = trickle.incremental_tables(meta) if f is not None else set()
        version_f = f if f is not None else datetime.now(timezone.utc)  # an unstamped publish still versions
        trickle_names = (set(meta) | {trickle.changelog_name(t) for t in meta} | {trickle.warm_name(t) for t in meta}
                         | {f"{t}{trickle.DROPLOG_SUFFIX}" for t in meta}
                         | {trickle.cold_base_name(t) for t, m in meta.items() if m.get("mode") == "merge"})
        plain = {t for t in tables if t not in trickle_names}  # the overwrite tables publish_plan declares
        merge_mains = {t for t, m in meta.items() if m.get("mode") == "merge"}
        colds = {trickle.cold_base_name(t) for t in merge_mains}

        def _export() -> None:
            for table in tables:
                if table in incremental:
                    # An append table's own history can take a large run at once (a first load): chunked.
                    _export_parts(con, data_dir, table, f, chunk=(meta.get(table) or {}).get("mode") == "append")
                elif table in merge_mains or table in colds or table in kept:
                    continue  # a base is published only at a checkpoint (below); a held table not at all
                elif table not in plain:  # a Trickle companion published without a run freshness
                    with data_dir.copy_to(f"{table}.parquet") as uri:
                        con.execute(f'COPY "{table}" TO \'{uri}\' (FORMAT PARQUET)')
                else:
                    # A new immutable version per run (never an in-place overwrite), so a Sink run pinned
                    # to the previous version keeps reading it. Written via tmp+rename locally.
                    vstore = data_dir.child(trickle.version_dir_name(table))
                    vstore.mkdir()
                    with vstore.copy_to(trickle.part_name(version_f)) as uri:
                        con.execute(f"COPY ({_concrete_select(con, table)}) TO '{uri}' (FORMAT PARQUET)")
            for main in merge_mains:
                _publish_tiered_main(con, data_dir, main, f)
            publish_plan(con, data_dir, f, held=kept)  # commit: every file it declares is in place
            if retain_from is not KEEP_ALL:
                prune_versions(data_dir, retain_from)  # after the new version is in place
            gc_tiers(data_dir, retain_from)  # superseded tiers no in-flight read can use
            _export_companions(con, data_dir, f)  # state-format Extension 1 (runs after the sidecar is final)
            _enrich_sidecar(con, data_dir, f)     # state-format Extension 2 (stats/schema hints, best-effort)

        retry_on_lock(_export)

    def _raw_read_select(self, data_dir, table: str, *, as_of=None, sidecar=None) -> str:
        """An overwrite table's version for ``as_of`` (see :func:`select_version`), else the flat layer."""
        from . import trickle_io as trickle

        data_dir = _as_storage(data_dir)
        versions = trickle.table_versions(data_dir, table)
        if versions:
            name = select_version(versions, as_of, table=table)
            return f"SELECT * FROM read_parquet('{data_dir.uri(trickle.version_dir_name(table), name)}')"
        return self._flat_read_select(data_dir, table, as_of=as_of, sidecar=sidecar)

    def _flat_read_select(self, data_dir, table: str, *, as_of=None, sidecar=None) -> str:
        """The unversioned layouts: append parts, merge base chunks, or a single ``{table}.parquet`` (the
        earlier overwrite layout, still used by Puddle snapshots). Trickle operands always land here, so
        they skip the versions probe."""
        from . import trickle_io as trickle

        data_dir = _as_storage(data_dir)
        parts = trickle.table_parts(data_dir, table)
        if parts or trickle.base_chunks(data_dir, table):
            sidecar = trickle.load_sidecar(data_dir) if sidecar is None else sidecar
        if parts:  # append-only parts directory → union the snapshot's parts
            names = parts_snapshot(data_dir, table, sidecar, as_of)
            if not names:  # nothing at the read's freshness: an empty relation with the table's columns
                return f"{_read_parquet_files([data_dir.uri(table, parts[0])])} LIMIT 0"
            return _read_parquet_files([data_dir.uri(table, n) for n in names], as_of)
        if trickle.base_chunks(data_dir, table):  # log-structured merge-main base → the snapshot's chunks
            base = merge_snapshot(data_dir, table, sidecar, as_of)["base"]
            if not base:
                raise FileNotFoundError(data_dir.uri(trickle.base_dir_name(table)))
            return _read_parquet_files(base, as_of)
        if not data_dir.exists(f"{table}.parquet"):
            raise FileNotFoundError(data_dir.uri(f"{table}.parquet"))
        return f"SELECT * FROM read_parquet('{data_dir.uri(table + '.parquet')}')"

    def list_tables(self, data_dir) -> list[str]:
        from . import trickle_io as trickle

        data_dir = _as_storage(data_dir)
        if not data_dir.exists():
            return []
        files = {n[: -len(".parquet")] for n in data_dir.parquet_names()}
        suffix = trickle.VERSION_SUFFIX
        versioned = {d[: -len(suffix)] for d in data_dir.subdir_names()
                     if d.endswith(suffix) and len(d) > len(suffix) and data_dir.parquet_names(d)}
        parts = set(trickle.part_tables(data_dir))
        # A merge main is reconstructed from its changelog; it is a published table even before its base
        # exists (no checkpoint yet → no `{main}.parquet`), so surface it from the sidecar.
        mains = {t for t, m in trickle.sidecar_tables(trickle.load_sidecar(data_dir)).items()
                 if m.get("mode") == "merge"}
        return sorted(files | versioned | parts | mains)


def _export_parts(con, data_dir, table: str, f, *, chunk: bool = False) -> None:
    """Publish an append-only ``table`` as a directory of per-run Parquet parts. Writes one
    ``_duckstring_f``-homogeneous file per registry freshness not already on disk (so a normal run writes
    just its new slice, and a rebuild/restore backfills any missing parts). Idempotent on replay.

    It never deletes a part for being below the floor: :func:`gc_tiers` does, after the sidecar commits and
    once no in-flight read can still be behind the floor. Pruning is floor-anchored, never absence-inferred
    (plans/s3-resident-state.md step 1): a part the registry merely *lacks* is kept, since absence is what a
    partially-hydrated registry looks like.

    When the table is **empty** (a bootstrap-only changelog with no rows yet), a schema-only marker part is
    still written at the run's ``f`` so the table stays readable as an empty relation — a consumer covered
    by the floor then sees an *empty* delta, not a coverage-miss full read.

    With ``chunk`` (an append table's own history), a run's rows are written as size-bounded chunks,
    ``{part}__{i}.parquet``, when they outgrow one: a first load of 268M rows took 34.7 s as one file and 6.1
    s as chunks, since DuckDB writes chunks in parallel. A run that fits in one keeps its plain name."""
    from . import trickle_io as trickle

    part_store = data_dir.child(table)
    part_store.mkdir()
    # A Z-set part's row order means nothing, so DuckDB may write it in any order: 30% quicker for a 13.4M-row
    # change (0.84 s against 0.60 s, 2026-10-25). An append history keeps its order.
    unordered = table.endswith((trickle.CHANGELOG_SUFFIX, trickle.WARM_SUFFIX, trickle.DROPLOG_SUFFIX))
    reg_fs = {r[0] for r in con.execute(f'SELECT DISTINCT "{trickle.F_COL}" FROM "{table}"').fetchall()
              if r[0] is not None}
    if not reg_fs and f is not None:
        reg_fs = {f}  # synthesize a 0-row marker part (the `WHERE = f` below selects nothing → empty part)
    existing: dict = {}  # freshness -> its part's file names (more than one when chunked)
    for n in part_store.parquet_names():
        existing.setdefault(trickle.part_f(n), []).append(n)
    # Write the parts not yet on disk; *always* (re)write the current run's f, whose content may have changed
    # this run (a same-f re-merge, or a replay) — older f's are immutable history and are skipped if present.
    to_write = (reg_fs - set(existing)) | ({f} if (f is not None and f in reg_fs) else set())
    for fi in to_write:
        select = f'SELECT * FROM "{table}" WHERE "{trickle.F_COL}" = {trickle._ts(fi)}'
        written = _write_part_chunks(con, part_store, fi, select) if chunk else []
        if not written:
            with part_store.copy_to(trickle.part_name(fi)) as uri, _unordered(con, unordered):
                con.execute(f"COPY ({select}) TO '{uri}' (FORMAT PARQUET)")
            written = [trickle.part_name(fi)]
        for old in existing.get(fi, []):  # a rewritten run's earlier files (another chunking)
            if old not in written:
                part_store.remove(old)
        existing[fi] = written


@contextmanager
def _unordered(con, on: bool = True):
    """Let DuckDB write without preserving insertion order for the statements inside, then restore the setting."""
    if not on:
        yield
        return
    prior = first_row(con.execute("SELECT current_setting('preserve_insertion_order')"))[0]
    con.execute("SET preserve_insertion_order = false")
    try:
        yield
    finally:
        con.execute(f"SET preserve_insertion_order = {'true' if prior else 'false'}")


def _write_part_chunks(con, part_store, fi, select: str) -> list[str]:
    """Write one run's part as size-bounded chunks through ``{table}/.chunks/`` (inside the table's
    directory, so a crash mid-export never leaves a table-shaped directory) and return their names: the
    plain part name for a single chunk, else ``{part}__{i}.parquet``. ``[]`` when nothing was written (no
    rows), for the caller's single-file marker part."""
    from . import trickle_io as trickle

    stage = part_store.child(".chunks")
    with part_store.copy_dir_to(".chunks") as uri:  # clears an earlier attempt
        con.execute(f"COPY ({select}) TO '{uri}' (FORMAT PARQUET, FILE_SIZE_BYTES "
                    f"{max(_MIN_CHUNK_BYTES, _compact_threshold())}, ROW_GROUP_SIZE {_ROW_GROUP_ROWS})")
    staged = stage.parquet_names()
    stem = trickle.part_name(fi)[: -len(".parquet")]
    names = [f"{stem}.parquet"] if len(staged) == 1 else [f"{stem}__{i}.parquet" for i in range(len(staged))]
    for src, dest in zip(staged, names, strict=True):
        stage.move_into(part_store, src, dest)
    part_store.rmtree(".chunks")
    return names


def _export_companions(con, data_dir, f) -> None:
    """Publish the registry aggregate/accumulate **state companions** ("Extension 1" of the published layout).

    Incremental ``.aggregate()`` / ``.accumulate()`` keep their cross-run fold state in registry-only
    companion tables (``_duckstring_agg_{table}`` / ``_duckstring_acc_{table}``). A registry-less host
    (or a Duck recovering from registry loss) can only resume incremental compute if that state is
    published, under ``{data_dir}/state/{agg|acc}/{table}/``:

    - a scan's state (``acc``) as one **whole snapshot** per run, ``{f}.parquet``, the previous one pruned;
    - an aggregate's state (``agg``, one row per group, which can be large: a group per customer) as a
      snapshot ``{f}.parquet`` plus, each later run, only the groups it changed (its rows stamped ``f``) and
      the keys it dropped (``_duckstring_gone``), in ``deltas/{f}.parquet``. The snapshot is rewritten once
      the deltas since it hold as many rows as it does (or a run rebuilt every group), so a run publishes in
      proportion to its change and recovery reads at most about twice the state. Before 2026-10-25 every run
      rewrote the whole state: 12M groups per run at TPC-DS SF1000, for about 1.2M changed.

    The ``state`` section of the sidecar names each companion's freshness (``f``) and, for an aggregate, its
    snapshot's (``full``), its key (``by``) and the rows in its deltas (``delta_rows``), so a reader pairs
    the companion with the main at the same freshness. The files live under ``state/`` so ``read_table`` /
    ``/api/data`` never expose them."""
    from . import trickle_io as trickle

    if f is None:  # a snapshot is stamped with the run freshness; nothing to key an f-named part on
        return
    data_dir = _as_storage(data_dir)
    f_iso = f.astimezone(_utc()).isoformat()
    prior = trickle.load_sidecar(data_dir).get("state") or {}
    state_section: dict[str, dict] = {}
    full_written: list[tuple[str, str, str]] = []  # (kind, table, kept snapshot name)
    q = trickle._q
    for base in trickle.read_meta(con):
        for prefix, kind in ((trickle.AGG_STATE_PREFIX, "agg"), (trickle.ACC_STATE_PREFIX, "acc")):
            reg = f"{prefix}{base}"
            if not trickle._table_exists(con, reg):
                continue
            total = first_row(con.execute(f'SELECT count(*) FROM "{reg}"'))[0]
            if total == 0:
                continue
            store = data_dir.child("state", kind, base)
            store.mkdir()
            part = trickle.part_name(f)
            entry: dict = {"f": f_iso}
            if kind == "agg":
                cols = [r[0] for r in con.execute(f'DESCRIBE "{reg}"').fetchall()]
                by = cols[: cols.index("_a_cnt")]
                drops = f"{trickle.AGG_DROP_PREFIX}{base}"
                fcond = f'{q(trickle.F_COL)} = {trickle._ts(f)}'
                changed = first_row(con.execute(f'SELECT count(*) FROM "{reg}" WHERE {fcond}'))[0]
                gone = (first_row(con.execute(f'SELECT count(*) FROM {q(drops)} WHERE {fcond}'))[0]
                        if trickle._table_exists(con, drops) else 0)
                last = prior.get(f"agg/{base}") or {}
                have_full = last.get("full") and store.exists(trickle.part_name(datetime.fromisoformat(last["full"])))
                delta_rows = int(last.get("delta_rows") or 0) + changed + gone
                if have_full and changed < total and delta_rows < total:
                    dstore = store.child("deltas")
                    dstore.mkdir()
                    rows = f'SELECT *, false AS "_duckstring_gone" FROM "{reg}" WHERE {fcond}'
                    if gone:
                        keys = ", ".join(q(c) for c in by)
                        rows += (f' UNION ALL BY NAME SELECT {keys}, {q(trickle.F_COL)}, true AS "_duckstring_gone" '
                                 f'FROM {q(drops)} WHERE {fcond}')
                    with dstore.copy_to(part) as uri:
                        con.execute(f"COPY ({rows}) TO '{uri}' (FORMAT PARQUET)")
                    entry.update(full=last["full"], by=by, delta_rows=delta_rows)
                else:
                    with store.copy_to(part) as uri:
                        con.execute(f'COPY (SELECT * FROM "{reg}") TO \'{uri}\' (FORMAT PARQUET)')
                    entry.update(full=f_iso, by=by, delta_rows=0)
                    full_written.append((kind, base, part))
                if trickle._table_exists(con, drops):
                    con.execute(f'DELETE FROM {q(drops)} WHERE {q(trickle.F_COL)} <= {trickle._ts(f)}')  # published
            else:
                with store.copy_to(part) as uri:
                    con.execute(f'COPY (SELECT * FROM "{reg}") TO \'{uri}\' (FORMAT PARQUET)')
                full_written.append((kind, base, part))
            state_section[f"{kind}/{base}"] = entry
    if state_section:  # record the snapshots in the sidecar's `state` section (skipped by read/diff surfaces)
        sidecar = trickle.load_sidecar(data_dir)
        sidecar["state"] = state_section
        trickle.write_sidecar(data_dir, sidecar)
    for kind, base, keep in full_written:  # a new snapshot supersedes the old one and every delta before it
        store = data_dir.child("state", kind, base)
        for old in store.parquet_names():
            if old != keep:
                store.remove(old)
        if store.is_dir("deltas"):
            store.rmtree("deltas")


def _load_state_companion(con, store, kind: str, table: str, entry: dict | None) -> bool:
    """Load a published state companion into the registry (:func:`_export_companions`): the snapshot, and for
    an aggregate with deltas, each group's newest row across the snapshot and the deltas after it, less the
    groups a delta dropped. Returns whether anything was loaded."""
    from . import trickle_io as trickle

    snap_store = store.child("state", kind, table)
    snaps = snap_store.parquet_names()
    if not snaps:
        return False
    prefix = trickle.AGG_STATE_PREFIX if kind == "agg" else trickle.ACC_STATE_PREFIX
    reg = trickle._q(prefix + table)
    entry = entry or {}
    full = trickle.part_name(datetime.fromisoformat(entry["full"])) if entry.get("full") else snaps[-1]
    if full not in snaps:
        full = snaps[-1]
    deltas = [n for n in snap_store.parquet_names("deltas") if trickle.part_f(n) > trickle.part_f(full)]
    if kind != "agg" or not deltas or not entry.get("by"):
        con.execute(f"CREATE OR REPLACE TABLE {reg} AS SELECT * FROM read_parquet('{snap_store.uri(full)}')")
        return True
    keys = ", ".join(trickle._q(c) for c in entry["by"])
    parts = [f"SELECT *, false AS \"_duckstring_gone\", 0 AS _src FROM read_parquet('{snap_store.uri(full)}')"]
    parts += [f"SELECT *, {i} AS _src FROM read_parquet('{snap_store.uri('deltas', n)}')"
              for i, n in enumerate(deltas, start=1)]
    union = " UNION ALL BY NAME ".join(f"({p})" for p in parts)
    con.execute(
        f"CREATE OR REPLACE TABLE {reg} AS SELECT * EXCLUDE (\"_duckstring_gone\", _src, _rn) FROM ("
        f"SELECT *, row_number() OVER (PARTITION BY {keys} ORDER BY _src DESC) AS _rn FROM ({union})"
        f") WHERE _rn = 1 AND NOT \"_duckstring_gone\""
    )
    return True


def _concrete_select(con, table: str) -> str:
    """``SELECT`` over a Ripple's ``table`` with any NULL-typed column published as INTEGER, as DuckDB 1.x typed
    it (2.0 types a bare NULL as the NULL type; see :func:`trickle.io.concrete_type`)."""
    from . import trickle_io as trickle

    cols = con.execute(f'DESCRIBE "{table}"').fetchall()
    sel = ", ".join(
        f"CAST({trickle._q(c)} AS {trickle.concrete_type(t)}) AS {trickle._q(c)}" if trickle.concrete_type(t) != t
        else trickle._q(c)
        for c, t, *_ in cols
    )
    return f'SELECT {sel} FROM "{table}"'


def _utc():
    from datetime import timezone

    return timezone.utc


def _published_bytes(data_dir, table: str) -> int:
    """Total published bytes backing ``table``'s current-state read — the wholesale file + per-run parts
    + warm bands + changelog parts + cold base chunks. A planner size hint (mirrors the Rust driver's
    ``published_bytes``)."""
    from . import trickle_io as trickle

    total = data_dir.size(f"{table}.parquet")
    versions = trickle.table_versions(data_dir, table)
    if versions:
        total += data_dir.size(trickle.version_dir_name(table), versions[-1])
    sidecar = trickle.load_sidecar(data_dir)
    if (sidecar.get(table) or {}).get("mode") == "merge":
        snap = merge_snapshot(data_dir, table, sidecar)
        files = [(trickle.base_dir_name(table), n) for n in snap["base_names"]]
        files += [(trickle.warm_name(table), n) for n in snap["band_names"]]
        files += [(trickle.changelog_name(table), n) for n in snap["hot_names"]]
    else:
        files = [(table, n) for n in parts_snapshot(data_dir, table, sidecar)]
    return total + sum(data_dir.size(*f) for f in files)


def _delta_rows_last(con, table: str, mode: str, f) -> int:
    """This run's delta size: the count of rows stamped at ``f`` in the table's delta structure — the
    merge **changelog**, the append **base**, or (overwrite) the whole rewrite."""
    from . import trickle_io as trickle

    if mode == "overwrite":
        return int(first_row(con.execute(f'SELECT count(*) FROM "{table}"'))[0])
    delta_table = trickle.changelog_name(table) if mode == "merge" else table
    if f is None or not trickle._table_exists(con, delta_table):
        return 0
    return int(first_row(con.execute(
        f'SELECT count(*) FROM "{delta_table}" WHERE "{trickle.F_COL}" = {trickle._ts(f)}'
    ))[0])


def _entry_schema(con, table: str) -> dict | None:
    """The user-column schema (name → DuckDB type) of a registry table, ``_duckstring_*`` system columns
    excluded — described from the base table, or its changelog when no base relation exists (a young
    merge main pre-checkpoint). ``None`` when neither exists."""
    from . import trickle_io as trickle
    from .trickle.context import SYSTEM_PREFIX

    src = table
    if not trickle._table_exists(con, src):
        src = trickle.changelog_name(table)
        if not trickle._table_exists(con, src):
            return None
    rel = con.sql(f'SELECT * FROM "{src}" LIMIT 0')
    return {c: str(t) for c, t in zip(rel.columns, rel.types, strict=True) if not c.startswith(SYSTEM_PREFIX)}


def _enrich_sidecar(con, data_dir, f) -> None:
    """Stamp each sidecar entry with size and schema hints ("Extension 2" of the published layout): per
    entry ``stats: {rows, bytes, delta_rows_last}``, a user-column ``schema`` map, and ``format: 2``.

    They let a reader size or describe a table **without opening Parquet footers** (column lineage reads
    the ``schema`` map). Hints only, never load-bearing: the footers stay the source of truth.
    **Best-effort:** a failure to compute a hint never breaks a publish; the entry just goes un-stamped."""
    from . import trickle_io as trickle

    data_dir = _as_storage(data_dir)
    sidecar = trickle.load_sidecar(data_dir)
    changed = False
    for table, entry in trickle.sidecar_tables(sidecar).items():
        mode = entry.get("mode", "overwrite")
        try:
            delta = _delta_rows_last(con, table, mode, f)
            if mode == "overwrite":
                rows = delta  # the whole rewrite is the current state
            else:
                rows = trickle.count_current(con, table) if mode == "merge" else int(
                    first_row(con.execute(f'SELECT count(*) FROM "{table}"'))[0])
            entry["stats"] = {"rows": rows, "bytes": _published_bytes(data_dir, table), "delta_rows_last": delta}
            schema = _entry_schema(con, table)
            if schema is not None:
                entry["schema"] = schema
            entry["format"] = 2  # the hints' layout (stats/schema), separate from Trickle's format_version
            changed = True
        except Exception:  # a hint, not a publish invariant
            continue
    if changed:
        trickle.write_sidecar(data_dir, sidecar)


def hydrate_registry(con, data_dir, tables=None) -> list[str]:
    """Rebuild registry state **from the published layout**, the recovery inverse of :meth:`export`
    (full-collection for the tiers it copies, because export mirrors the registry back).

    For each sidecar base entry (or just ``tables`` when given): the base/main (an overwrite table's newest
    version;
    append parts; a merge main's cold base chunks), the ``__changelog`` / ``__band`` / ``__droplog``
    companion parts, the meta row (mode/pk/floor from the sidecar; ``f_base`` from the sidecar;
    ``f_warm`` from the newest published band's part name), and the *state-format Extension 1*
    agg/acc accumulator snapshots (``state/{agg|acc}/{table}/``). Tables are ``CREATE OR REPLACE``d,
    so hydration is idempotent and safe over a partially-present registry.

    A merge main's cold base and warm bands aren't copied: they're registered as views over the published
    files (plans/s3-resident-state.md), so ``data_dir`` should be where this registry publishes. A Duck that
    lost its local publish restores it first (:func:`restore_tree`).

    The caller is **Duck registry-loss recovery** (the registry *file* is gone or unreadable: host loss,
    migration, scale-to-zero, a fresh cloud Duck; the published state survives; see ``RippleExecutor``).
    Returns the hydrated base-table names."""
    from . import trickle_io as trickle

    store = _as_storage(data_dir)
    store.duckdb_setup(con)  # configure object-store creds on this connection (a no-op for local)
    plane = ParquetDataPlane()  # hydration reads the flat layer (every backend also writes it)
    sidecar = trickle.load_sidecar(store)
    hydrated: list[str] = []
    for table, entry in trickle.sidecar_tables(sidecar).items():
        if tables is not None and table not in tables:
            continue
        trickle.check_format_version(entry.get("format_version"), table)
        mode = entry.get("mode", "overwrite")
        loaded = False
        if mode == "merge":
            # The registry main is the COLD BASE (a young merge main pre-checkpoint has none) — never the
            # reconstruction; reconstruct happens at read time over base ⊎ changelog. Register it as a VIEW over
            # the published S3 chunks, NOT a materialised copy: the base can be arbitrarily large (TB), a fresh
            # Duck must not copy it down to run a small delta, and a steady-state incremental run never reads it
            # (only a checkpoint / comprehensive fallback does, and they read on demand). See
            # plans/s3-resident-state.md. The changelog/warm below stay materialised for now (later steps).
            snap = merge_snapshot(store, table, sidecar)  # the committed state: its base, bands and hot parts
            if snap["base"]:
                sql = _read_parquet_files(snap["base"])
                cold = trickle.cold_base_name(table)
                trickle._drop_relation(con, table)  # a legacy base under the main's own name
                trickle._drop_relation(con, cold)
                con.execute(f'CREATE VIEW {trickle._q(cold)} AS {sql}')
                loaded = True
        else:  # append parts dir, or an overwrite table's newest version (or the earlier single file)
            if (trickle.table_versions(store, table) or trickle.table_parts(store, table)
                    or store.exists(f"{table}.parquet")):
                sql = plane._raw_read_select(store, table)
                con.execute(f'CREATE OR REPLACE TABLE {trickle._q(table)} AS {sql}')
                loaded = True
        for companion in (trickle.changelog_name(table), f"{table}{trickle.DROPLOG_SUFFIX}"):
            parts = trickle.table_parts(store, companion)
            if not parts:
                continue
            if companion == trickle.changelog_name(table) and mode == "merge":
                # The hot parts above the newest band; folded parts a reader may still need stay on disk,
                # but their rows are in the bands.
                names = snap["hot_names"]
            else:
                names = parts_snapshot(store, companion, sidecar)
            uris = [store.uri(companion, n) for n in (names or parts[:1])]
            sql = _read_parquet_files(uris) + ("" if names else " LIMIT 0")
            con.execute(f'CREATE OR REPLACE TABLE {trickle._q(companion)} AS {sql}')
            loaded = True
        from datetime import datetime

        f_base = datetime.fromisoformat(entry["f_base"]) if entry.get("f_base") else None
        bands = _band_names(store, table, f_base) if mode == "merge" else []
        loaded = loaded or bool(bands)
        if mode in ("merge", "append") and loaded:
            trickle._record_meta(con, table, mode, tuple(entry.get("pk") or ()))
            if entry.get("floor"):
                trickle._advance_floor(con, table, bootstrap_f=datetime.fromisoformat(entry["floor"]))
            if f_base is not None:
                trickle._set_f_base(con, table, f_base)
            # The warm watermark is the newest band still above the base (a band at or below it is stale).
            f_warm = max(trickle.part_f(n) for n in bands) if bands else f_base
            if f_warm is not None:
                trickle._set_f_warm(con, table, f_warm)
            if mode == "merge":
                _set_band_view(con, store, table)
            trickle.refresh_current_view(con, table)
        # Extension 1: the agg/acc accumulator state (a snapshot, and an aggregate's deltas after it).
        for kind in ("agg", "acc"):
            if _load_state_companion(con, store, kind, table, (sidecar.get("state") or {}).get(f"{kind}/{table}")):
                loaded = True
        if loaded:
            hydrated.append(table)
    return hydrated


def restore_tree(src, dest) -> int:
    """Copy a published layout from ``src`` (the durable persist layer) to ``dest`` (a lost local publish),
    every file, with the ``_trickle.json`` sidecar last: a local sidecar is what makes a layout count as
    published (``registry.resolve_data_dir``, Duck recovery), so an interrupted restore is never trusted.
    The inverse of :func:`persist_tree` for a Duck whose machine lost its local publish: the local layout
    must be complete again, since Ponds on the same machine read it and the registry's views point at it.
    Returns the number of files copied."""
    from .storage import copy_tree

    src, dest = _as_storage(src), _as_storage(dest)
    dest.mkdir()
    count = 0
    for sub in src.subdir_names():
        count += copy_tree(src.child(sub), dest.child(sub))
    for name in sorted(src.names(), key=lambda n: n == "_trickle.json"):
        dest.write_bytes(src.read_bytes(name), name)
        count += 1
    return count


def persist_tree(local_dir, dest) -> int:
    """Mirror a Pond's locally-published output to its durable **persist layer** (plans/persist.md) —
    the async Duck-side reconcile behind ``persisted_f``. Returns the number of files uploaded.

    Semantics per entry kind:

    - **top-level files** (the sidecar, a legacy single-file ``{table}.parquet``) — always uploaded
      (rewritten per run; small, or the table's whole content by design);
    - **directory files** (overwrite versions, append/changelog/band parts, base chunks, state snapshots)
      — immutable and idempotent **by name**: upload only what the destination lacks, and prune a
      destination file only when the local sidecar's watermark covers it (version retention, append
      retention, warm folds, checkpoint token swaps); snapshot pruning in ``state/`` propagates as is;
    - **directories of tables the sidecar no longer declares** (a dropped table) — removed at the
      destination.

    **Safety guard**: a local dir with no ``_trickle.json`` sidecar has published nothing — the mirror
    refuses to touch the destination at all (a fresh/lost box must never wipe the durable layer).
    Replayable: a mirror that dies mid-way re-runs idempotently (parts by name, wholesale re-upload)."""
    import json
    from datetime import datetime

    from .trickle.io import base_table_name

    local = _as_storage(local_dir)
    root = local.root  # persist mirrors FROM a local publish dir by definition
    if not (root / "_trickle.json").exists():
        return 0  # nothing published locally — never touch the durable layer from an empty source
    try:
        sidecar = json.loads((root / "_trickle.json").read_text())
    except Exception:
        sidecar = {}

    def _drop_before(dirname: str):
        """The prune watermark for a directory of freshness-named files — **retention is an explicit
        signal, never inferred from absence** (plans/s3-resident-state.md step 1): a destination part is
        removed only when the LOCAL SIDECAR's floor (or, for base chunks, ``f_base``) says its freshness
        was genuinely dropped (retention / warm fold / checkpoint token supersession / re-bootstrap). A
        part merely absent locally is KEPT — absence is what a partial local (a future partially-hydrated
        box) looks like, and pruning on it would delete real history from the durable plane. ``None`` =
        no watermark → never prune this dir's files."""
        from .trickle.io import BASE_SUFFIX, CHANGELOG_SUFFIX, VERSION_SUFFIX, WARM_SUFFIX

        entry = sidecar.get(base_table_name(dirname))
        if not isinstance(entry, dict):
            return None
        if dirname.endswith(WARM_SUFFIX):
            # Warm bands stay live until a checkpoint folds them into the base, so the base's watermark is
            # the signal, inclusive: a band at or below f_base is in the base. (The floor rises with every
            # fold while the older bands are still state; pruning by it lost them from the durable layer.)
            iso = entry.get("f_base")
            return (datetime.fromisoformat(iso), True) if iso else None
        if dirname.endswith(VERSION_SUFFIX):
            # Overwrite versions: the oldest version retention kept (prune_versions); older ones are gone.
            iso = entry.get("v_floor")
            return (datetime.fromisoformat(iso), False) if iso else None

        if dirname.endswith(BASE_SUFFIX):
            # Base chunks: a checkpoint's token supersession — STRICTLY older tokens only. The CURRENT
            # token equals f_base, and pruning it when merely absent locally would delete the live base.
            iso = entry.get("f_base")
            return (datetime.fromisoformat(iso), False) if iso else None
        # Per-run parts: the floor. INCLUSIVE only for a merge __changelog (a warm fold moves the
        # boundary part's rows into the band — leaving it would double-count on plane reads); STRICT for
        # an append history/droplog (an at-floor part holds real rows a full read needs).
        iso = entry.get("floor")
        return (datetime.fromisoformat(iso), dirname.endswith(CHANGELOG_SUFFIX)) if iso else None

    copied = 0
    local_dirs: set[str] = set()
    files: list = []
    # Data first, the SIDECAR LAST: `_trickle.json` is the mirror's commit marker — a cross-Pool reader
    # trusts its freshness claims, so at every instant the mirrored data must be a superset of what the
    # mirrored sidecar declares. Uploading it before its parts would expose a torn window (sidecar at f,
    # parts for f still in flight → a delta read misses rows). Directories, then plain files, then it.
    for p in sorted(root.iterdir()):
        if p.name.endswith(".tmp"):
            continue
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            local_dirs.add(p.name)
            copied += _mirror_dir(p, dest, (p.name,), drop_before=_drop_before(p.name))
    for p in sorted(files, key=lambda q: q.name == "_trickle.json"):  # sidecar sorts last
        dest.put_file(p, p.name)
        copied += 1
    # Prune destination directories only for tables the local sidecar no longer DECLARES (a table
    # dropped/unpublished — an explicit signal); a declared table's missing local dir is left alone, except
    # warm bands, which are pruned up to the base watermark once a checkpoint has folded them.
    from .trickle.io import WARM_SUFFIX, part_f

    for name in dest.subdir_names():
        if name in local_dirs or name == "state":
            continue
        if base_table_name(name) not in sidecar:
            dest.rmtree(name)
        elif name.endswith(WARM_SUFFIX):
            # A checkpoint removed the local band directory: its bands are in the base now.
            mark = _drop_before(name)
            for n in dest.parquet_names(name):
                ff = _file_f(n, part_f)
                if mark is not None and ff is not None and ff <= mark[0]:
                    dest.remove(name, n)
    return copied


def _mirror_dir(src: Path, dest, parts: tuple[str, ...], drop_before=None) -> int:
    """Reconcile one directory of immutable, name-addressed files: upload the locally-present-but-missing;
    prune a destination file only when ``drop_before`` (the explicit retention/fold/checkpoint watermark)
    covers its parsed freshness — with one exception: the ``state/`` snapshot tree (``drop_before`` None
    at its root) keeps exact keep-latest semantics, pruning what local no longer holds (each snapshot dir
    is rewritten wholesale every run, so local is always complete there)."""
    from .trickle.io import part_f

    copied = 0
    existing = set(dest.names(*parts))
    local_files: set[str] = set()
    local_dirs: set[str] = set()
    state_tree = parts[0] == "state"
    for p in sorted(src.iterdir()):
        if p.name.endswith(".tmp"):
            continue
        if p.is_dir():
            local_dirs.add(p.name)
            copied += _mirror_dir(p, dest, parts + (p.name,), drop_before=drop_before)
            continue
        local_files.add(p.name)
        if p.name not in existing:
            dest.put_file(p, *parts, p.name)
            copied += 1
    for name in existing - local_files:
        if state_tree:
            dest.remove(*parts, name)  # keep-latest snapshots: local is authoritative + complete
        elif drop_before is not None:
            mark, inclusive = drop_before
            ff = _file_f(name, part_f)
            if ff is not None and (ff <= mark if inclusive else ff < mark):
                dest.remove(*parts, name)  # genuinely dropped — the watermark says so
    for name in set(dest.child(*parts).subdir_names()) - local_dirs:
        if state_tree:
            dest.rmtree(*parts, name)
    return copied


def _file_f(name: str, part_f) -> "datetime | None":  # noqa: F821 - forward ref for the docstring type
    """The freshness encoded in a published file name: a per-run part (``{f}.parquet``) or a base chunk
    (``{token}__{i}.parquet``, token = the checkpoint f). ``None`` if unparseable (never pruned)."""
    stem = name[: -len(".parquet")] if name.endswith(".parquet") else name
    if "__" in stem:
        stem = stem.rsplit("__", 1)[0]  # a base chunk's token
    try:
        return part_f(stem)
    except Exception:
        return None


def _compact_threshold(con=None, main=None) -> int:
    """The checkpoint floor / target base-chunk size in bytes. A merge main checkpoints when its
    changelog-since-the-fold-watermark outgrows ``max(base size, this)`` — so it never checkpoints below
    this, and otherwise at ~k=1 (changelog ≥ base). Resolution order: a per-table override recorded at the
    merge write (``merge_table(..., compact_threshold=)``), else the catchment-level
    ``DUCKSTRING_COMPACT_THRESHOLD`` env, else 256 MiB."""
    import os

    if con is not None and main is not None:
        from . import trickle_io as trickle

        override = trickle.read_meta(con).get(main, {}).get("compact_threshold")
        if override is not None:
            return int(override)
    return int(os.environ.get("DUCKSTRING_COMPACT_THRESHOLD", str(256 * 1024 * 1024)))


def _base_bytes(data_dir, main: str, f_base=None) -> int:
    """The on-disk size of a merge main's published base — the sum of its ``{main}__base/`` chunks, or the
    legacy single ``{main}.parquet`` file. ``0`` when no base has been published yet."""
    from . import trickle_io as trickle

    chunks = trickle.base_chunks(data_dir, main)
    if chunks:
        base = data_dir.child(trickle.base_dir_name(main))
        current = _current_base_names(data_dir, main, f_base) if f_base is not None else chunks
        return sum(base.size(n) for n in current or chunks)
    return data_dir.size(f"{main}.parquet")


def _publish_tiered_main(con, data_dir: Path, main: str, f) -> None:
    """Maintain a merge main's tiered storage at publish time (see plans/trickle-main-incremental.md):

    - **Cold compaction** (rare, O(base)) when the warm tier has grown to match the cold base (k=1): fold
      base + warm + hot ``≤ f`` into a fresh clean base, republish the base chunks, and clear the warm bands.
    - **Warm fold** (frequent, cheap) otherwise, once the hot changelog has accumulated past ``2×`` the chunk
      threshold: move its older slice into a warm band, leaving a ``~threshold`` hot window for caught-up
      consumers' delta reads, and publish the new band.

    The hot changelog parts themselves are exported by the main publish loop; here we only re-sync them after
    a fold/compaction trims the registry changelog."""
    from . import trickle_io as trickle

    _export_bands(con, data_dir, main)  # a band staged but not yet published (a crash), or a legacy warm table
    threshold = _compact_threshold(con, main)
    clog, warm = trickle.changelog_name(main), trickle.warm_name(main)
    warm_store = data_dir.child(warm)
    f_base_now = trickle._f_base(con, main)
    warm_bytes = sum(warm_store.size(n) for n in _band_names(data_dir, main, f_base_now))
    cold_bytes = _base_bytes(data_dir, main, f_base_now)

    clog_store = data_dir.child(clog)
    f_warm = trickle._f_warm(con, main) or trickle._f_base(con, main)
    hot = [n for n in trickle.table_parts(data_dir, clog)
           if f_warm is None or trickle.part_f(n) > f_warm]
    hot_bytes = sum(clog_store.size(n) for n in hot)

    # Cold compaction (k=1: warm ≥ cold), or the **bootstrap** of the very first base directly from the hot
    # changelog (no warm tier yet) so a fresh main folds straight to cold rather than via a warm round-trip.
    bootstrap = cold_bytes == 0 and (warm_bytes + hot_bytes) >= threshold
    if warm_bytes >= max(cold_bytes, threshold) or bootstrap:  # cold compaction (k=1: warm ≥ cold)
        trickle.checkpoint(con, main, f)  # fold base+warm+hot≤f → clean base (a local table); clear warm
        if trickle._table_exists(con, trickle.cold_base_name(main)):
            _publish_base_chunks(con, data_dir, main, f, threshold)
            _review_base(con, data_dir, main)  # drop the local base; point the registry at the published S3 chunks
        # The bands are folded into the new base now; they stay until no in-flight read uses them (gc_tiers).
        _export_parts(con, data_dir, clog, f)  # re-sync the hot parts after retention trim
        return

    if hot_bytes >= 2 * threshold:  # warm fold: pack the oldest hot parts, leaving a ~threshold hot window
        warm_target, remaining = None, hot_bytes
        for n in hot[:-1]:  # oldest-first; never the newest part → a caught-up consumer's latest delta stays
            if remaining <= threshold:
                break
            remaining -= clog_store.size(n)
            warm_target = trickle.part_f(n)
        if warm_target is not None:
            trickle.fold_warm(con, main, warm_target)
            _export_bands(con, data_dir, main)
            _export_parts(con, data_dir, clog, f)  # drop the folded hot parts (now in the warm band)


# Where a merge table's first load is staged under its Pond's data directory until the publish moves it into
# place (stage_boot_base). Not a table: nothing lists a directory holding no Parquet of its own.
BOOT_STAGING = ".boot"


def stage_boot_base(con, data_dir, main: str, rows_sql: str) -> str | None:
    """Write a merge main's first load (``rows_sql``) as size-bounded Parquet chunks under the local
    ``data_dir``'s :data:`BOOT_STAGING`, in the order the rows arrive, and return a ``SELECT`` over them for
    the registry's base to be a view of. :func:`_publish_local_base` moves them into ``{main}__base/`` at
    publish. The base is written once, where before it was written into the registry, read back, sorted and
    written again: 30 s and 153 s for a 269M-row table on a laptop (TPC-DS SF100), against 19 s for this. A
    declared ``cluster_by`` is still applied at publish, by rewriting the chunks in that order; the default
    primary-key order waits for the first compaction. ``None`` (the base stays a registry table) for an
    empty first load, which writes no file."""
    data_dir = _as_storage(data_dir)
    size = max(_MIN_CHUNK_BYTES, int(_compact_threshold(con, main)))
    with data_dir.copy_dir_to(BOOT_STAGING, main) as uri:  # clears an earlier attempt's chunks
        con.execute(f"COPY ({rows_sql}) TO '{uri}' "
                    f"(FORMAT PARQUET, FILE_SIZE_BYTES {size}, ROW_GROUP_SIZE {_ROW_GROUP_ROWS})")
    names = data_dir.parquet_names(BOOT_STAGING, main)
    if not names:
        return None
    files = ", ".join(f"'{data_dir.uri(BOOT_STAGING, main, n)}'" for n in names)
    return f"SELECT * FROM read_parquet([{files}])"


def _publish_local_base(con, data_dir: Path, main: str) -> None:
    """Publish a merge main's cold base if it isn't published yet, then point the registry at the published
    chunks (:func:`_review_base`). A base is unpublished only between being written (a bootstrap, or a
    checkpoint whose publish didn't finish) and its publish: a registry table, or a first load staged by
    :func:`stage_boot_base`, which is moved into place (or, with a declared ``cluster_by``, rewritten in that
    order). Once published the base is a view over the chunks, and this is a no-op."""
    from . import trickle_io as trickle

    data_dir = _as_storage(data_dir)
    cold = trickle.cold_base_name(main)
    f_base = trickle._f_base(con, main)
    staged = data_dir.parquet_names(BOOT_STAGING, main) if data_dir.is_dir(BOOT_STAGING, main) else []
    local = first_row(con.execute("SELECT 1 FROM duckdb_tables() WHERE table_name = ? AND schema_name = 'main'",
                                  [cold])) is not None
    if f_base is None or not (staged or local):
        return
    if staged and not local and not (trickle.read_meta(con).get(main) or {}).get("cluster"):
        base_store = data_dir.child(trickle.base_dir_name(main))
        base_store.mkdir()
        token = trickle.part_name(f_base)[: -len(".parquet")]
        stage = data_dir.child(BOOT_STAGING, main)
        for i, name in enumerate(staged):
            stage.move_into(base_store, name, f"{token}__{i}.parquet")
        # An earlier base (a rebuild's predecessor) has another token and stays until gc_tiers.
    else:
        _publish_base_chunks(con, data_dir, main, f_base, _compact_threshold(con, main))
    _review_base(con, data_dir, main)
    if data_dir.is_dir(BOOT_STAGING, main):
        data_dir.rmtree(BOOT_STAGING, main)


def _review_base(con, data_dir: Path, main: str) -> None:
    """After a checkpoint materialised the new base as a registry table and published it as S3 chunks, drop the
    local table and re-register ``main`` as a **view** over those chunks — so the (possibly huge) base is not
    held in the registry between checkpoints. Symmetric with :func:`hydrate_registry`'s base branch; a
    steady-state run never reads it, and a later checkpoint / comprehensive read hits S3 on demand. See
    plans/s3-resident-state.md."""
    from . import trickle_io as trickle

    data_dir = _as_storage(data_dir)
    names = _current_base_names(data_dir, main, trickle._f_base(con, main))
    if not names:  # nothing published (shouldn't happen post-publish) → leave as-is
        return
    sql = _read_parquet_files([data_dir.uri(trickle.base_dir_name(main), n) for n in names])
    cold = trickle.cold_base_name(main)
    trickle._drop_relation(con, cold)
    con.execute(f'CREATE VIEW {trickle._q(cold)} AS {sql}')
    trickle.refresh_current_view(con, main)


def _current_base_names(data_dir, main: str, f_base) -> list[str]:
    """The chunk names of merge main ``main``'s base at token ``f_base`` (the registry's or the sidecar's):
    older tokens may still be on disk for in-flight readers (:func:`gc_tiers`). Every chunk when ``f_base``
    is unknown."""
    tokens = _base_tokens(data_dir, main)
    if f_base is None or f_base not in tokens:
        return [n for names in tokens.values() for n in names] if f_base is None else []
    return tokens[f_base] + tokens.get(None, [])


def _band_names(data_dir, main: str, f_base) -> list[str]:
    """The published band files of merge main ``main`` still above its cold base (``f > f_base``); bands at
    or below it were folded into the base by a checkpoint and are stale."""
    from . import trickle_io as trickle

    return [n for n in trickle.table_parts(data_dir, trickle.warm_name(main))
            if f_base is None or trickle.part_f(n) > f_base]


def _set_band_view(con, data_dir, main: str) -> None:
    """Point the registry's ``{main}__band`` at the published band files (an explicit file list, so the view
    changes only when it's recreated, never because a file appeared), or drop it when there are none. The
    warm tier is read from where it's published and never copied into the registry
    (plans/s3-resident-state.md)."""
    from . import trickle_io as trickle

    warm = trickle.warm_name(main)
    names = _band_names(data_dir, main, trickle._f_base(con, main))
    trickle._drop_relation(con, warm)  # a view, or a legacy table (its rows are published; see _export_bands)
    if names:
        files = ", ".join(f"'{data_dir.uri(warm, n)}'" for n in names)
        con.execute(f'CREATE VIEW {trickle._q(warm)} AS '
                    f'SELECT * FROM read_parquet([{files}], union_by_name=true)')


def _export_bands(con, data_dir: Path, main: str) -> None:
    """Publish the band a warm fold staged (``fold_warm`` → :func:`~duckstring.trickle.io.warm_pending_name`)
    as ``{main}__band/{f_warm}.parquet``, then, in one transaction, repoint the ``{main}__band`` view to
    include it and drop the staged rows, so a concurrent read sees the band exactly once. Each band keeps its
    rows' original ``_duckstring_f`` (as-of reads stay correct) and is named by its upper freshness.

    Idempotent: a band file already published isn't rewritten. Also converts a legacy registry, where the
    warm tier was a table: its rows not yet in a published band are staged and published like a fold's. A
    no-op when nothing is staged and the view is current."""
    from . import trickle_io as trickle

    warm, pending = trickle.warm_name(main), trickle.warm_pending_name(main)
    q, fb = trickle._q, trickle._q(trickle.F_COL)
    band_store = data_dir.child(warm)
    published = [trickle.part_f(n) for n in band_store.parquet_names()]
    last_hi = max(published) if published else None
    above = f"{fb} > {trickle._ts(last_hi)}" if last_hi is not None else "1=1"

    legacy = first_row(con.execute("SELECT 1 FROM duckdb_tables() WHERE table_name = ? AND schema_name = 'main'",
                         [warm])) is not None
    if legacy:
        if trickle._table_exists(con, pending):
            con.execute(f'INSERT INTO {q(pending)} SELECT * FROM {q(warm)} WHERE {above}')
        else:
            con.execute(f'CREATE TABLE {q(pending)} AS SELECT * FROM {q(warm)} WHERE {above}')
    staged = trickle._table_exists(con, pending)
    if not legacy and not staged:
        return

    f_warm = trickle._f_warm(con, main)
    if staged and f_warm is not None:
        dest = trickle.part_name(f_warm)
        rows = first_row(con.execute(f'SELECT count(*) FROM {q(pending)} WHERE {above}'))[0]
        if rows and not band_store.exists(dest):
            band_store.mkdir()
            with band_store.copy_to(dest) as uri:
                con.execute(f'COPY (SELECT * FROM {q(pending)} WHERE {above}) TO \'{uri}\' (FORMAT PARQUET)')

    con.execute("BEGIN TRANSACTION")
    try:
        _set_band_view(con, data_dir, main)
        con.execute(f'DROP TABLE IF EXISTS {q(pending)}')
        trickle.refresh_current_view(con, main)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


# The smallest chunk the cold base is split into. The DuckDB 2.0 alphas' COPY rollover never terminated when
# FILE_SIZE_BYTES was a few bytes (empty files without end; fixed in 2.0.0), and a threshold that small is only
# ever set to make compaction eager, not to get byte-sized chunks.
_MIN_CHUNK_BYTES = 64 * 1024


# Rows per row group in a cold base chunk: DuckDB's default, set explicitly because the automatic
# cluster_bits sizes rank-Hilbert cells against it.
_ROW_GROUP_ROWS = 122_880

# Up to this many rows, a clustering column's bucket boundaries are exact quantiles (deterministic: the same data
# gives the same layout). Above it they come from approx_quantile, a one-pass t-digest, because an exact quantile
# sorts the column and the boundaries only need to be close.
_EXACT_QUANTILE_ROWS = 10_000_000

# Types approx_quantile accepts. Others (strings, booleans, UUIDs, intervals) always use exact quantiles.
_APPROX_QUANTILE_TYPES = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT",
                          "UINTEGER", "UBIGINT", "UHUGEINT", "FLOAT", "DOUBLE", "DECIMAL", "DATE", "TIMESTAMP")


def _bits_per_column(total: int, k: int) -> int:
    """Bits per column for a ``total``-bit key over ``k`` columns: the Hilbert curve needs every column to have
    the same bits, so ``total`` is rounded up to a multiple of ``k``, within the 63 bits a BIGINT key holds."""
    return max(1, min(-(-total // k), 63 // k))


def _auto_cluster_bits(rows: int, k: int) -> int:
    """Rank-Hilbert bits for a base of ``rows`` rows: enough cells that each is just under one row group
    (``2**n`` cells of at most one row group), at least one bit per column, at most 63."""
    import math

    groups = rows / _ROW_GROUP_ROWS
    n = math.ceil(math.log2(groups)) + 1 if groups > 1 else 1
    return max(k, min(63, n))


def rank_hilbert_sql(source: str, cols: list[str], b: int, nullable: list[bool] | None = None,
                     approx: list[bool] | None = None) -> str:
    """``source`` with a ``_duckstring_key`` column: the **rank-Hilbert** key over ``cols``, ``b`` bits each.

    Each column is replaced by its quantile rank in ``2**b`` buckets of about equal population, so every
    column's distribution is flattened to uniform and skewed columns get as much ordering as even ones. A
    column that has NULLs (``nullable[i]``) keeps its top bucket for them and ranks its other values in the
    rest. The ranks are then mapped onto a Hilbert curve, so rows close in the key are close in every column
    and sorting by it gives each row group narrow ranges on all of ``cols``.

    The bucket boundaries come from one aggregate over ``source``: exact quantiles, or ``approx_quantile`` for
    the columns where ``approx[i]`` is set. A row's rank is then found by binary search, one comparison per
    bit, most significant first: the boundary compared at each level is picked by the bits found so far.
    Equal values always share a bucket. The rank is mapped onto the curve with Skilling's transpose
    ("Programming the Hilbert curve", 2004), unrolled into one projection per step, and the result's bits
    are interleaved, most significant first, one per column per round in list order."""
    from . import trickle_io as trickle

    q = trickle._q
    k = len(cols)
    nullable = nullable if nullable is not None else [True] * k
    approx = approx if approx is not None else [False] * k
    r = [f'"_duckstring_r{i}"' for i in range(k)]
    bounds, levels = [], []
    for i, (c, has_null, apx) in enumerate(zip(cols, nullable, approx, strict=True)):
        buckets = max((1 << b) - 1, 1) if has_null else 1 << b
        if buckets > 1:
            fn, cast = ("approx_quantile", "::FLOAT") if apx else ("quantile_disc", "")
            fractions = ", ".join(f"({j}/{buckets}){cast}" for j in range(1, buckets))
            bounds.append(f'{fn}({q(c)}, [{fractions}]) AS "_duckstring_q{i}"')
            levels.append(i)
    # A boundary past the end of the list (a NULL bucket's) is NULL, which no value exceeds.
    sql = f"SELECT *, {', '.join(f'0::BIGINT AS {x}' for x in r)} FROM {source}"
    if bounds:
        sql = f"SELECT * FROM ({sql}) CROSS JOIN (SELECT {', '.join(bounds)} FROM {source})"

    def layer(sql: str, assigns: dict[str, str]) -> str:
        return f"SELECT * REPLACE ({', '.join(f'{e} AS {c}' for c, e in assigns.items())}) FROM ({sql})"

    for depth in range(b):
        step = 1 << (b - 1 - depth)
        if levels:
            sql = layer(sql, {r[i]: f'{r[i]} * 2 + coalesce({q(cols[i])} > "_duckstring_q{i}"[({r[i]} * 2 + 1) * {step}], '
                                    f'false)::BIGINT' for i in levels})
    nulls = {r[i]: f"CASE WHEN {q(cols[i])} IS NULL THEN {(1 << b) - 1} ELSE {r[i]} END"
             for i in range(k) if nullable[i]}
    if nulls:
        sql = layer(sql, nulls)
    big_q = 1 << (b - 1)
    qq = big_q
    while qq > 1:  # Skilling's AxestoTranspose: undo the excess work
        p = qq - 1
        sql = layer(sql, {r[0]: f"CASE WHEN ({r[0]} & {qq}) <> 0 THEN xor({r[0]}, {p}) ELSE {r[0]} END"})
        for i in range(1, k):
            t = f"(xor({r[0]}, {r[i]}) & {p})"
            sql = layer(sql, {r[0]: f"CASE WHEN ({r[i]} & {qq}) <> 0 THEN xor({r[0]}, {p}) ELSE xor({r[0]}, {t}) END",
                              r[i]: f"CASE WHEN ({r[i]} & {qq}) <> 0 THEN {r[i]} ELSE xor({r[i]}, {t}) END"})
        qq >>= 1
    for i in range(1, k):  # Gray encode
        sql = layer(sql, {r[i]: f"xor({r[i]}, {r[i - 1]})"})
    terms, qq = [], big_q
    while qq > 1:
        terms.append(f"CASE WHEN ({r[k - 1]} & {qq}) <> 0 THEN {qq - 1} ELSE 0 END")
        qq >>= 1
    if terms:
        t = terms[0]
        for term in terms[1:]:
            t = f"xor({t}, {term})"
        sql = layer(sql, {x: f"xor({x}, {t})" for x in r})
    positions, p = [], k * b - 1
    for depth in range(b):
        for i in range(k):
            positions.append(f"((({r[i]} >> {b - 1 - depth}) & 1) << {p})")
            p -= 1
    helpers = ", ".join([*r, *(f'"_duckstring_q{i}"' for i in levels)])
    return f'SELECT * EXCLUDE ({helpers}), CAST({" + ".join(positions)} AS BIGINT) AS "_duckstring_key" FROM ({sql})'


def cluster_order_select(con, source: str, spec: dict) -> str:
    """``source`` (a relation name or a parenthesised query) in the order a :func:`cluster_spec
    <duckstring.trickle.io.cluster_spec>` asks for: a sort by its columns, or by a rank-Hilbert key when it
    interleaves two or more (``cluster_bits`` sized to the data when unset)."""
    from . import trickle_io as trickle

    q = trickle._q
    cols = spec["by"]
    if not spec["interleave"]:
        return f"SELECT * FROM {source} ORDER BY {', '.join(q(c) for c in cols)}"
    counts = first_row(con.execute(f"SELECT count(*), {', '.join(f'count(*) FILTER (WHERE {q(c)} IS NULL)' for c in cols)} "
                         f"FROM {source}"))
    types = dict(con.execute(f"SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM {source})").fetchall())
    large = counts[0] > _EXACT_QUANTILE_ROWS
    approx = [large and types[c].upper().startswith(_APPROX_QUANTILE_TYPES) for c in cols]
    total = spec.get("bits") or _auto_cluster_bits(counts[0], len(cols))
    keyed = rank_hilbert_sql(source, cols, _bits_per_column(total, len(cols)), [n > 0 for n in counts[1:]], approx)
    return f'SELECT * EXCLUDE ("_duckstring_key") FROM ({keyed}) ORDER BY "_duckstring_key"'


def _base_order_select(con, main: str, cold: str) -> str:
    """The cold base ``cold`` of merge main ``main`` in its stored order: by the ``cluster_by`` declared on
    its merge writes (sorted, or by a rank-Hilbert key when interleaving two or more columns), else by its
    primary key, so a read filtering on those columns skips most chunks and row groups."""
    from . import trickle_io as trickle

    meta = trickle.read_meta(con).get(main, {})
    spec = meta.get("cluster")
    if spec is None and meta.get("pk"):
        spec = {"by": list(meta["pk"]), "interleave": False, "bits": None}
    if spec is None:
        return f"SELECT * FROM {trickle._q(cold)}"
    return cluster_order_select(con, trickle._q(cold), spec)


def _publish_base_chunks(con, data_dir: Path, main: str, f, chunk_bytes: int) -> None:
    """Publish the registry base table ``main`` as a directory of size-bounded Parquet chunks
    (``{main}__base/``), ordered by its primary key or its declared ``cluster_by`` (:func:`_base_order_select`).
    The new chunks are written under this checkpoint's unique token (its freshness); a read picks one token
    (:func:`merge_snapshot`), and the older tokens' chunks are removed by :func:`gc_tiers` once no in-flight
    read can pick them."""
    from . import trickle_io as trickle

    base_name = trickle.base_dir_name(main)
    base_store = data_dir.child(base_name)
    base_store.mkdir()
    token = trickle.part_name(f)[: -len(".parquet")]  # unique per checkpoint, freshness-ordered
    staging_name = base_name + ".tmp"
    staging_store = data_dir.child(staging_name)
    size = max(_MIN_CHUNK_BYTES, int(chunk_bytes))
    ordered = _base_order_select(con, main, trickle.cold_base_name(main))
    written = []
    with data_dir.copy_dir_to(staging_name) as staging_uri:  # clears staging, yields the dir target
        con.execute(
            f"COPY ({ordered}) TO '{staging_uri}' "
            f"(FORMAT PARQUET, FILE_SIZE_BYTES {size}, ROW_GROUP_SIZE {_ROW_GROUP_ROWS})"
        )
        for i, name in enumerate(staging_store.parquet_names()):  # commit each staged chunk under our token
            dest = f"{token}__{i}.parquet"
            staging_store.move_into(base_store, name, dest)
            written.append(dest)
    data_dir.rmtree(staging_name)
    # The previous checkpoint's chunks (another token) stay until no in-flight read can pick them (gc_tiers).


def get_data_plane() -> DataPlane:
    """The data plane: versioned Parquet (``plans/data-plane-choice.md`` removed the Iceberg plane)."""
    return ParquetDataPlane()
