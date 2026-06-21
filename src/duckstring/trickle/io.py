"""Trickle: incremental I/O via **Z-sets** (DBSP-style) — the history-preserving Ripple variant.

A **Trickle** is a Ripple that maintains *history* instead of overwriting wholesale, so a consumer can
compute its output from just the rows that changed in the window ``(previous_f, f]`` (a small **delta**,
the win — see ``plans/trickle-dbsp.md``). Every change is a **Z-set**: a relation carrying an integer
weight column ``_duckstring_d`` per row (``+1`` = a present row, ``-1`` = a retraction). A normal table is
a Z-set with every weight ``+1``; an **update is a `-1` of the old full image plus a `+1` of the new** —
so deletions/updates carry *full row images*, not key-only tombstones. That is what lets a change compose
through a join/project on **any** key (the old tombstone-on-PK constraint is gone).

The freshness stamp ``_duckstring_f`` **lives in the data** and is read as a **content predicate**
(``WHERE _duckstring_f > previous_f AND _duckstring_f <= f``), never a snapshot cursor — so the window
read works regardless of compaction and over either data plane. The ``_duckstring_*`` namespace is
framework-owned; Trickle's system columns are now just:

- ``_duckstring_f`` — the run's freshness, stamped on every history/changelog row;
- ``_duckstring_d`` — the Z-set weight (``+1`` / ``-1``) on a merge Trickle's changelog.

(The old ``_duckstring_op`` and ``_duckstring_hash`` columns are gone: a comprehensive diff is now a
full-row Z-set difference ``new(+1) ⊎ main(-1)`` consolidated, which needs no per-row hash, and the merge
*main* is pure user columns.)

Two write modes:

- **append** — insert-only history. One table; its delta is the window of new rows, each weight ``+1``.
- **merge** — a clean *main* table (one row per PK, ``SELECT *`` = current state) **plus** an append-only
  ``__changelog`` Z-set stream. The public :func:`merge_table` takes the *complete current state* and
  diffs it against the prior main to derive the Z-set; the builder composes a Z-set directly and applies
  it via :func:`apply_zset`.

A consumer reads a source's delta via ``pond.read_delta("source.table")`` → a :class:`Delta` exposing the
Z-set (``.zset``) plus ``.is_full`` (a from-scratch full read: bootstrap, coverage-miss, or a *changed*
overwrite Ripple — the consumer must then recompute comprehensively). An *unchanged* overwrite Ripple
returns an **empty** delta (detected by comparing the source's published freshness to ``previous_f``), so
it contributes only as a stable history operand — the master-data common case, free.

Storage is kept in the Pond's **registry** and published wholesale by the data plane each run; the window
read prunes on the consumer side. Mode/PK/floor and the source's run freshness ``f`` are mirrored to a
``_trickle.json`` sidecar so a cross-Pond reader (no registry access) can resolve them.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

_uid = itertools.count()


def unique_name(prefix: str) -> str:
    """A process-unique scratch identifier in the reserved namespace (so ``registry_tables`` hides it).
    Used where a *materialised* result must outlive any shared-name view it was built from (a returned
    relation that would otherwise re-bind when the next call re-creates that view)."""
    return f"_duckstring_ds_{prefix}_{next(_uid)}"


# System columns — the reserved namespace this engine owns (see :data:`.context.SYSTEM_PREFIX`).
F_COL = "_duckstring_f"
D_COL = "_duckstring_d"  # the Z-set weight (+1 present / -1 retraction)

# A merge Trickle's CDC stream lives in a ``{table}__changelog`` companion registry table.
CHANGELOG_SUFFIX = "__changelog"
# A builder ``.append(..., log_drops=True)`` records rows it could not append (retractions / value
# conflicts under ``fail_on_conflict=False``) in a ``{table}__droplog`` companion — an append-only diagnostic
# published alongside the table (like ``__changelog``), one growing record of what each run dropped.
DROPLOG_SUFFIX = "__droplog"
# A merge main is log-structured: its folded **base** (the checkpointed state up to ``f_base``) is published
# as a directory of size-bounded, freshness-ordered Parquet **chunks** under ``{table}__base/`` (so a single
# base can hold far more than one Parquet file's worth, and a partition-granular checkpoint can rewrite just
# the chunks holding changed PKs — see plans/trickle-main-incremental.md). The base is wholesale (rewritten
# at a checkpoint), distinct from the per-run append parts; ``part_tables`` excludes it for that reason.
BASE_SUFFIX = "__base"
# A ``.aggregate(...)`` output keeps its raw accumulators (count + per-summed-col sum & non-NULL count) in a
# ``_duckstring_agg_{name}`` companion. Reserved prefix → ``registry_tables`` hides it from publish; the
# published main holds only the derived user columns.
AGG_STATE_PREFIX = "_duckstring_agg_"
# The mode/PK registry: one row per Trickle output table. Named in the reserved namespace so
# ``registry_tables`` hides it from the publish set.
META_TABLE = "_duckstring_trickle"
# The published sidecar carrying mode/PK/floor + the source run freshness to cross-Pond readers.
SIDECAR = "_trickle.json"


class DeltaError(ValueError):
    """A delta read or Trickle write was used incompatibly."""


def changelog_name(table: str) -> str:
    return f"{table}{CHANGELOG_SUFFIX}"


def base_dir_name(table: str) -> str:
    """The published-base directory name for a log-structured merge main ``table``."""
    return f"{table}{BASE_SUFFIX}"


def base_chunks(data_dir: Path, table: str) -> list[Path]:
    """The published base chunk files of a merge main ``table`` (its ``{table}__base/`` directory), sorted;
    ``[]`` when the base has not been chunk-published (no checkpoint yet, or a legacy single-file base)."""
    d = Path(data_dir) / base_dir_name(table)
    return sorted(d.glob("*.parquet")) if d.is_dir() else []


def normalize_pk(pk) -> tuple[str, ...]:
    """Coerce a PK declaration (str / sequence) to a tuple of column names."""
    if pk is None:
        return ()
    if isinstance(pk, str):
        return (pk,)
    return tuple(pk)


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _ts(dt) -> str:
    return f"TIMESTAMPTZ '{dt.isoformat()}'"


def _table_exists(con, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM duckdb_tables() WHERE table_name = ?", [name]
    ).fetchone() is not None


def _user_cols(columns) -> list[str]:
    """The user columns of a relation — everything outside the reserved ``_duckstring_*`` namespace."""
    from .context import SYSTEM_PREFIX as RESERVED_PREFIX

    return [c for c in columns if not c.startswith(RESERVED_PREFIX)]


def _apply_retention(con, table: str, f, retain_t, retain_n) -> None:
    """Bound a history/changelog table's retained window at write time — a **lag SLA**, not a
    correctness control (a consumer behind the retained window falls back to a full read of the clean
    state; see :func:`read_delta`). Both are opt-in (``None`` keeps everything):

    - ``retain_t`` (a ``timedelta``): drop rows stamped older than ``f - retain_t``.
    - ``retain_n`` (a count): keep only the newest ``retain_n`` distinct ``_duckstring_f`` runs.

    Returns the **cutoff** it applied (the oldest freshness still retained) so the caller can raise the
    published ``floor`` to it — a consumer behind the cutoff then coverage-misses and full-reads."""
    cutoff = None
    if retain_t is not None:
        cutoff = f - retain_t
        con.execute(f'DELETE FROM {_q(table)} WHERE {_q(F_COL)} < {_ts(cutoff)}')
    if retain_n is not None and retain_n >= 1:
        kept_min = con.execute(
            f'SELECT min(g) FROM (SELECT DISTINCT {_q(F_COL)} AS g FROM {_q(table)} '
            f'ORDER BY g DESC LIMIT {int(retain_n)})'
        ).fetchone()[0]
        if kept_min is not None:
            con.execute(f'DELETE FROM {_q(table)} WHERE {_q(F_COL)} < {_ts(kept_min)}')
            cutoff = kept_min if cutoff is None else max(cutoff, kept_min)
    return cutoff


# ─── meta (mode + PK + floor) ──────────────────────────────────────────────────


def _ensure_meta(con) -> None:
    con.execute(
        f'CREATE TABLE IF NOT EXISTS {_q(META_TABLE)} '
        f"(table_name VARCHAR PRIMARY KEY, mode VARCHAR, pk VARCHAR, floor VARCHAR, f_base VARCHAR, "
        f"compact_threshold VARCHAR)"
    )
    # Migrate an older meta table that predates a column: f_base (the log-structured merge main fold
    # watermark) and compact_threshold (the per-table checkpoint-size override — see _compact_threshold).
    cols = [r[1] for r in con.execute(f'PRAGMA table_info({_q(META_TABLE)})').fetchall()]
    if "f_base" not in cols:
        con.execute(f'ALTER TABLE {_q(META_TABLE)} ADD COLUMN f_base VARCHAR')
    if "compact_threshold" not in cols:
        con.execute(f'ALTER TABLE {_q(META_TABLE)} ADD COLUMN compact_threshold VARCHAR')


def _record_meta(con, table: str, mode: str, pk: tuple[str, ...]) -> None:
    _ensure_meta(con)
    # Preserve any existing floor / f_base (a normal incremental run must not reset them).
    con.execute(
        f'INSERT INTO {_q(META_TABLE)} (table_name, mode, pk) VALUES (?, ?, ?) '
        f'ON CONFLICT (table_name) DO UPDATE SET mode=excluded.mode, pk=excluded.pk',
        [table, mode, ",".join(pk)],
    )


def _f_base(con, table: str):
    """The merge main's **fold watermark** — the freshness up to which the ``__changelog`` has been folded
    into the base table. ``None`` before the first checkpoint (the whole changelog is still the main)."""
    from datetime import datetime

    if not _table_exists(con, META_TABLE):
        return None
    row = con.execute(f'SELECT f_base FROM {_q(META_TABLE)} WHERE table_name = ?', [table]).fetchone()
    return datetime.fromisoformat(row[0]) if (row and row[0]) else None


def _set_f_base(con, table: str, f) -> None:
    from datetime import timezone

    _ensure_meta(con)
    con.execute(
        f'UPDATE {_q(META_TABLE)} SET f_base = ? WHERE table_name = ?',
        [f.astimezone(timezone.utc).isoformat(), table],
    )


def _advance_floor(con, table: str, *, bootstrap_f=None, cutoff=None) -> None:
    """Maintain a Trickle table's coverage **floor** — the earliest freshness a windowed read can rely on
    (below it, a consumer full-reads the clean state). A bootstrap/refresh **sets** it to that run's ``f``;
    retention **raises** it to its cutoff. A normal incremental run touches neither."""
    from datetime import datetime, timezone

    cur = con.execute(f'SELECT floor FROM {_q(META_TABLE)} WHERE table_name = ?', [table]).fetchone()
    floor = datetime.fromisoformat(cur[0]) if (cur and cur[0]) else None
    if bootstrap_f is not None:
        floor = bootstrap_f
    if cutoff is not None and (floor is None or cutoff > floor):
        floor = cutoff
    if floor is not None:
        con.execute(
            f'UPDATE {_q(META_TABLE)} SET floor = ? WHERE table_name = ?',
            [floor.astimezone(timezone.utc).isoformat(), table],
        )


def read_meta(con) -> dict[str, dict]:
    """``{table: {"mode", "pk": [...], "floor": iso|None, "f_base": iso|None,
    "compact_threshold": int|None}}`` for every Trickle table."""
    if not _table_exists(con, META_TABLE):
        return {}
    rows = con.execute(
        f'SELECT table_name, mode, pk, floor, f_base, compact_threshold FROM {_q(META_TABLE)}'
    ).fetchall()
    return {r[0]: {"mode": r[1], "pk": (r[2].split(",") if r[2] else []), "floor": r[3], "f_base": r[4],
                   "compact_threshold": (int(r[5]) if r[5] else None)}
            for r in rows}


def _set_compact_threshold(con, table: str, n) -> None:
    """Record a per-table checkpoint-size override (bytes) for a merge main — the changelog must outgrow
    ``max(base, this)`` before a checkpoint folds. ``None`` leaves the catchment default in force."""
    if n is None:
        return
    _ensure_meta(con)
    con.execute(
        f'UPDATE {_q(META_TABLE)} SET compact_threshold = ? WHERE table_name = ?', [str(int(n)), table]
    )


def write_sidecar(data_dir: Path, payload: dict[str, dict]) -> None:
    """Publish ``{table: {mode, pk, floor, f}}`` next to the data so a cross-Pond reader can resolve a
    Trickle source's coverage and detect whether an overwrite source advanced (its ``f`` vs the
    consumer's ``previous_f``)."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = data_dir / (SIDECAR + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(data_dir / SIDECAR)


def load_sidecar(data_dir: Path) -> dict[str, dict]:
    path = Path(data_dir) / SIDECAR
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


# ─── incremental publish + draw (per-run parts) ────────────────────────────────
#
# Append-only published tables — an append Trickle's history and every merge Trickle's ``__changelog`` /
# ``__droplog`` — are written **incrementally** as a *directory of per-run Parquet parts*
# (``{data_dir}/{table}/{f}.parquet``, one ``_duckstring_f``-homogeneous file per run) rather than one
# wholesale file rewritten each run. A run therefore writes only its own delta (O(change), not O(history)),
# and a cross-Catchment draw ships only the new part files. A merge *main* and plain overwrite output stay
# single-file wholesale (clean current state). The data plane reads either layout transparently
# (``read_parquet('{table}/*.parquet')`` vs ``'{table}.parquet'``).


def incremental_tables(meta: dict[str, dict]) -> set[str]:
    """The append-only published table names for a Pond whose Trickle ``meta`` is given — written as
    per-run parts: every append base, plus each merge base's ``__changelog`` and each append base's
    ``__droplog`` companion. (Droplog names are included unconditionally; a caller only acts on those that
    actually exist in the registry.)"""
    out: set[str] = set()
    for base, m in meta.items():
        if m.get("mode") == "append":
            out.add(base)
            out.add(f"{base}{DROPLOG_SUFFIX}")
        elif m.get("mode") == "merge":
            out.add(changelog_name(base))
    return out


def part_name(f) -> str:
    """The filename for the per-run part holding the rows stamped freshness ``f`` — the **UTC** ISO
    timestamp with ``:`` swapped for ``_`` (filesystem-safe, lexically sortable, reversible by
    :func:`part_f`). Normalised to UTC so the name is canonical regardless of the writer's session tz."""
    from datetime import timezone

    return f.astimezone(timezone.utc).isoformat().replace(":", "_") + ".parquet"


def part_f(name: str):
    """Recover the freshness ``f`` a part file was stamped with from its :func:`part_name`."""
    from datetime import datetime

    stem = name[: -len(".parquet")] if name.endswith(".parquet") else name
    return datetime.fromisoformat(stem.replace("_", ":"))


def table_parts(data_dir: Path, table: str) -> list[Path]:
    """The per-run part files of an append-only ``table`` (a directory), sorted oldest-first; ``[]`` if it
    is not a parts directory (e.g. a wholesale single-file table, or absent)."""
    d = Path(data_dir) / table
    return sorted(d.glob("*.parquet")) if d.is_dir() else []


def part_tables(data_dir: Path) -> list[str]:
    """The names of the append-only (parts-directory) tables published under ``data_dir``. A merge main's
    ``{table}__base/`` directory is **excluded** — it is a wholesale base (rewritten at a checkpoint), not
    a per-run-parts table, so the incremental-draw / ``landed_after`` machinery must not treat it as one."""
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        return []
    return sorted(p.name for p in data_dir.iterdir()
                  if p.is_dir() and not p.name.endswith(BASE_SUFFIX) and any(p.glob("*.parquet")))


def landed_after(data_dir: Path) -> str | None:
    """The freshness a consumer has fully landed = ``min`` over its append-only tables of each table's
    high-water ``max(floor, max part f)`` (read from the part filenames, no Parquet open). ``None`` means
    *transfer wholesale* (no append-only tables landed yet — a bootstrap)."""
    from datetime import datetime

    data_dir = Path(data_dir)
    sidecar = load_sidecar(data_dir)
    tables = part_tables(data_dir)
    if not tables:
        return None
    highs = []
    for table in tables:
        base = table
        for suffix in (CHANGELOG_SUFFIX, DROPLOG_SUFFIX):
            if table.endswith(suffix):
                base = table[: -len(suffix)]
                break
        floor = sidecar.get(base, {}).get("floor")
        high = datetime.fromisoformat(floor) if floor else None
        for pq in table_parts(data_dir, table):
            pf = part_f(pq.name)
            if high is None or pf > high:
                high = pf
        if high is None:
            return None
        highs.append(high)
    return min(highs).isoformat() if highs else None


# ─── write: append ──────────────────────────────────────────────────────────────


def append_table(
    con, name: str, relation, f, pk: tuple[str, ...], *, fail_on_conflict=True, retain_t=None, retain_n=None
) -> None:
    """Append ``relation``'s rows to the history table ``name``, each stamped ``_duckstring_f = f``.
    Insert-only: no diff, no deletes (its Z-set is all ``+1``). Idempotent at a given ``f`` (rows already
    stamped ``f`` are dropped before re-appending). ``pk`` is recorded as the declared key; when it is set,
    ``fail_on_conflict=True`` (the default) asserts it is unique across the appended rows and the existing
    history, raising :class:`DeltaError` before any write (the live table is untouched on a violation). Pass
    ``fail_on_conflict=False`` for the trust-the-writer fast path (no check). With ``pk`` unset the check is a
    no-op regardless."""
    if f is None:
        raise DeltaError("a Trickle needs the run freshness pond.f — none was set (is this a Trickle run?)")
    src = "_duckstring_ds_append_src"
    relation.create_view(src, replace=True)
    cols = relation.columns
    sel_cols = ", ".join(_q(c) for c in cols)
    first = not _table_exists(con, name)  # the floor anchors at the first append's freshness
    if fail_on_conflict and pk:
        missing = [c for c in pk if c not in cols]
        if missing:
            raise DeltaError(f"append_table('{name}'): primary key column(s) {missing} not in the relation")
        pk_list = ", ".join(_q(c) for c in pk)
        dup = con.execute(
            f'SELECT 1 FROM {_q(src)} GROUP BY {pk_list} HAVING count(*) > 1 LIMIT 1'
        ).fetchone()
        if dup:
            raise DeltaError(f"append_table('{name}'): duplicate primary key {pk} among the appended rows")
        if not first:
            # Collide only against rows from *other* runs — a replay re-appends this f's identical rows.
            coll = con.execute(
                f'SELECT 1 FROM {_q(src)} s JOIN {_q(name)} t USING ({pk_list}) '
                f'WHERE t.{_q(F_COL)} IS DISTINCT FROM {_ts(f)} LIMIT 1'
            ).fetchone()
            if coll:
                raise DeltaError(f"append_table('{name}'): primary key {pk} already present in history")
    if first:
        con.execute(
            f'CREATE TABLE {_q(name)} AS '
            f'SELECT {sel_cols}, CAST(NULL AS TIMESTAMPTZ) AS {_q(F_COL)} FROM {_q(src)} LIMIT 0'
        )
    con.execute(f'DELETE FROM {_q(name)} WHERE {_q(F_COL)} = {_ts(f)}')  # idempotent replay
    con.execute(
        f'INSERT INTO {_q(name)} ({sel_cols}, {_q(F_COL)}) '
        f'SELECT {sel_cols}, {_ts(f)} FROM {_q(src)}'
    )
    _record_meta(con, name, "append", pk)
    cutoff = _apply_retention(con, name, f, retain_t, retain_n)
    _advance_floor(con, name, bootstrap_f=(f if first else None), cutoff=cutoff)


def append_zset(
    con, name: str, zset, f, pk: tuple[str, ...], *, fail_on_conflict=True, log_drops=True,
    retain_t=None, retain_n=None,
) -> None:
    """Append the **present** rows of a Z-set ΔO (the builder's incremental output, or a full recompute
    tagged ``+1``) to the insert-only history ``name`` — the ``.append()`` terminal of the builder. An
    insert-only table can't reflect a *change to the past*, so two things are conflicts:

    - a **retraction** (a ``-1`` row) — a previously-emitted output row changed or disappeared;
    - a present (``+1``) row whose ``pk`` is already in history with a **different** image.

    A present row whose ``pk`` is already in history with an **identical** image is a benign skip (an
    idempotent replay or a comprehensive re-derivation re-producing it) — never a conflict, never logged.
    A ``pk`` duplicated **within this run** with distinct images is unresolvable (one freshness, no
    recency to choose by) and always raises.

    ``fail_on_conflict=True`` (default — correctness over speed) raises :class:`DeltaError` on any conflict
    before writing. ``False`` drops conflicting rows (history wins) and appends the rest; with
    ``log_drops`` the dropped rows land in a ``{name}__droplog`` companion (user columns + ``_duckstring_d``
    sign + ``_duckstring_f``), published alongside the table like ``__changelog``. ``pk`` unset skips the pk
    checks entirely (only retractions are conflicts) — fast, sound only when duplicates and past-changes are
    impossible by construction."""
    if f is None:
        raise DeltaError("a Trickle needs the run freshness pond.f — none was set (is this a Trickle run?)")
    src = unique_name("appz")
    zset.create_view(src, replace=True)
    user = [c for c in zset.columns if c != D_COL]
    if D_COL not in zset.columns:
        raise DeltaError(f"append_zset('{name}', ...): the relation has no {D_COL} weight column")
    sel = ", ".join(_q(c) for c in user)
    first = not _table_exists(con, name)

    # Consolidate by full row: net weight per distinct image (an update's -old/+new survive as two rows).
    consol = unique_name("appc")
    con.execute(
        f'CREATE OR REPLACE TEMP TABLE {_q(consol)} AS '
        f'SELECT {sel}, CAST(SUM({_q(D_COL)}) AS BIGINT) AS {_q(D_COL)} FROM {_q(src)} '
        f'GROUP BY {sel} HAVING SUM({_q(D_COL)}) <> 0'
    )

    # Idempotent replay: drop this run's prior attempt (rows stamped f) so the history checks below see the
    # pre-f state and a retry re-derives the same result. Done before the conflict probes.
    if not first:
        con.execute(f'DELETE FROM {_q(name)} WHERE {_q(F_COL)} = {_ts(f)}')

    retractions = con.execute(f'SELECT count(*) FROM {_q(consol)} WHERE {_q(D_COL)} < 0').fetchone()[0]
    pk_dup = 0
    hist_conflict = 0
    if pk:
        missing = [c for c in pk if c not in user]
        if missing:
            raise DeltaError(f"append_zset('{name}', ...): primary key column(s) {missing} not in the relation")
        pk_list = ", ".join(_q(c) for c in pk)
        pk_dup = con.execute(
            f'SELECT count(*) FROM (SELECT 1 FROM {_q(consol)} WHERE {_q(D_COL)} > 0 '
            f'GROUP BY {pk_list} HAVING count(*) > 1)'
        ).fetchone()[0]
        if pk_dup:
            # Two distinct images for one key in one run — no recency to disambiguate; always fatal.
            raise DeltaError(
                f"append_zset('{name}', ...): primary key {pk} produced {pk_dup} duplicate key(s) with "
                f"differing values in one run — the output is not unique by {pk}"
            )
        if not first:
            eq = " AND ".join(f'p.{_q(c)} IS NOT DISTINCT FROM h.{_q(c)}' for c in user)
            hist_conflict = con.execute(
                f'SELECT count(*) FROM {_q(consol)} p JOIN {_q(name)} h USING ({pk_list}) '
                f'WHERE p.{_q(D_COL)} > 0 AND NOT ({eq})'
            ).fetchone()[0]

    if fail_on_conflict and (retractions or hist_conflict):
        raise DeltaError(
            f"append_zset('{name}', ...): not append-safe — {retractions} retraction(s) and {hist_conflict} "
            f"changed-past row(s). Pass fail_on_conflict=False to drop them, or use .merge() to track changes."
        )

    # The rows to actually append: present (+1) rows that are genuinely new (pk absent from history, or no
    # pk → all present rows). Benign skips (pk present with identical image) and dropped conflicts fall out.
    if pk and not first:
        pk_list = ", ".join(_q(c) for c in pk)
        new_rows = con.sql(
            f'SELECT {sel} FROM {_q(consol)} WHERE {_q(D_COL)} > 0 '
            f'AND ({pk_list}) NOT IN (SELECT {pk_list} FROM {_q(name)})'
        )
    else:
        new_rows = con.sql(f'SELECT {sel} FROM {_q(consol)} WHERE {_q(D_COL)} > 0')

    if first:
        con.execute(
            f'CREATE TABLE {_q(name)} AS '
            f'SELECT {sel}, CAST(NULL AS TIMESTAMPTZ) AS {_q(F_COL)} FROM {_q(consol)} LIMIT 0'
        )
    new_view = unique_name("appn")
    new_rows.create_view(new_view, replace=True)
    con.execute(f'INSERT INTO {_q(name)} ({sel}, {_q(F_COL)}) SELECT {sel}, {_ts(f)} FROM {_q(new_view)}')

    if log_drops and not fail_on_conflict and (retractions or hist_conflict):
        _log_drops(con, name, consol, user, pk, f, first)

    _record_meta(con, name, "append", pk)
    cutoff = _apply_retention(con, name, f, retain_t, retain_n)
    _advance_floor(con, name, bootstrap_f=(f if first else None), cutoff=cutoff)


def _log_drops(con, name, consol, user, pk, f, first_output) -> None:
    """Record the rows ``.append(fail_on_conflict=False)`` could not append — retractions (``_duckstring_d``
    < 0) and present rows whose ``pk`` collided with a different image — in a ``{name}__droplog`` companion
    (append-only, published alongside the table like ``__changelog``). Replay-idempotent (this run's prior
    drops are cleared first)."""
    drops = f"{name}{DROPLOG_SUFFIX}"
    sel = ", ".join(_q(c) for c in user)
    if pk:
        pk_list = ", ".join(_q(c) for c in pk)
        eq = " AND ".join(f'c.{_q(col)} IS NOT DISTINCT FROM h.{_q(col)}' for col in user)
        # Present rows that collided with a *different* image in history (benign identical-image skips excluded).
        conflicts = (
            f'SELECT {", ".join(f"c.{_q(col)}" for col in user)}, c.{_q(D_COL)} FROM {_q(consol)} c '
            f'JOIN {_q(name)} h USING ({pk_list}) WHERE c.{_q(D_COL)} > 0 AND NOT ({eq})'
        )
    else:
        conflicts = f'SELECT {sel}, {_q(D_COL)} FROM {_q(consol)} WHERE 1=0'
    dropped = con.sql(
        f'SELECT {sel}, {_q(D_COL)} FROM {_q(consol)} WHERE {_q(D_COL)} < 0 '
        f'UNION ALL BY NAME ({conflicts})'
    )
    if not _table_exists(con, drops):
        con.execute(
            f'CREATE TABLE {_q(drops)} AS '
            f'SELECT {sel}, {_q(D_COL)}, CAST(NULL AS TIMESTAMPTZ) AS {_q(F_COL)} FROM {_q(consol)} LIMIT 0'
        )
    else:
        con.execute(f'DELETE FROM {_q(drops)} WHERE {_q(F_COL)} = {_ts(f)}')  # replay-idempotent
    dview = unique_name("appd")
    dropped.create_view(dview, replace=True)
    con.execute(
        f'INSERT INTO {_q(drops)} ({sel}, {_q(D_COL)}, {_q(F_COL)}) '
        f'SELECT {sel}, {_q(D_COL)}, {_ts(f)} FROM {_q(dview)}'
    )


# ─── write: merge (Z-set apply) ───────────────────────────────────────────────


_RECON_CTE = "_duckstring_recon"


def reconstruct_sql(base_sql, clog_sql, f_base, pk, *, upper=None, before=None) -> str:
    """SQL for the **current state** of a log-structured merge main: latest-per-PK over the ``base`` (rows
    folded up to the watermark ``f_base``) overlaid with the changelog window ``(f_base, upper]``. Produces
    the user columns + ``_duckstring_f`` (each row's last-write freshness). ``base_sql`` is ``None`` before
    the first checkpoint (the whole changelog is still the main); ``f_base`` ``None`` ⇒ window = all changelog.

    The changelog ``≤ f_base`` is *already in the base*, so it is excluded here (it is retained only for a
    lagging consumer's window read). An update a→b→c collapses to the surviving image with its latest ``f``;
    a net retraction drops the PK; an unchanged base PK keeps its base row and base freshness. Column-agnostic
    (``* EXCLUDE`` + ``GROUP BY ALL``) — needs only the PK, so it composes over any base/changelog SELECT
    without inspecting schemas (the data plane builds it straight from the sidecar)."""
    pksel = ", ".join(_q(c) for c in pk)
    lo = f"{_q(F_COL)} > {_ts(f_base)}" if f_base is not None else "1=1"
    hi = f" AND {_q(F_COL)} <= {_ts(upper)}" if upper is not None else ""
    if before is not None:  # exclusive upper — the state strictly *before* a freshness (the merge prior)
        hi += f" AND {_q(F_COL)} < {_ts(before)}"
    consol = (
        f"SELECT * EXCLUDE ({_q(D_COL)}, {_q(F_COL)}), MAX({_q(F_COL)}) AS {_q(F_COL)}, "
        f"CAST(SUM({_q(D_COL)}) AS BIGINT) AS {_q(D_COL)} "
        f"FROM (SELECT * FROM ({clog_sql}) WHERE {lo}{hi}) GROUP BY ALL HAVING SUM({_q(D_COL)}) <> 0"
    )
    upserts = f"SELECT * EXCLUDE ({_q(D_COL)}) FROM {_RECON_CTE} WHERE {_q(D_COL)} > 0"
    if base_sql is None:
        return f"WITH {_RECON_CTE} AS ({consol}) {upserts}"
    return (
        f"WITH {_RECON_CTE} AS ({consol}) "
        f"SELECT * FROM ({base_sql}) WHERE ({pksel}) NOT IN (SELECT {pksel} FROM {_RECON_CTE}) "
        f"UNION ALL BY NAME {upserts}"
    )


def _reconstruct_sql_for(con, name: str, *, upper=None, before=None) -> str | None:
    """Build :func:`reconstruct_sql` for a merge main ``name`` from the registry (base table ``name`` +
    its ``__changelog``). ``None`` if the table has no changelog and no base (nothing written yet)."""
    clog = changelog_name(name)
    if not _table_exists(con, clog):
        return f'SELECT * FROM {_q(name)}' if _table_exists(con, name) else None
    pk = tuple(read_meta(con).get(name, {}).get("pk", ()))
    base_sql = f'SELECT * FROM {_q(name)}' if _table_exists(con, name) else None
    return reconstruct_sql(base_sql, f'SELECT * FROM {_q(clog)}', _f_base(con, name), pk,
                           upper=upper, before=before)


def reconstruct_current(con, name: str, *, upper=None, before=None):
    """The current clean state of merge main ``name`` (registry) as a relation — user columns +
    ``_duckstring_f`` — reconstructed from the base table + changelog. ``before`` (exclusive) gives the
    state strictly before a freshness (the prior a same-run merge diffs against). ``None`` if nothing yet."""
    sql = _reconstruct_sql_for(con, name, upper=upper, before=before)
    return con.sql(sql) if sql is not None else None


def current_state(con, name: str):
    """The current clean state of a registry table as a user-column relation: a merge main is reconstructed
    from its base ⊎ changelog; anything else (append history, plain output) is the table itself. System
    columns are stripped (the user-facing view ``read_table`` returns)."""
    if read_meta(con).get(name, {}).get("mode") == "merge":
        rel = reconstruct_current(con, name)
        if rel is not None:
            return _strip_system(rel)
    return _strip_system(con.sql(f'SELECT * FROM {_q(name)}'))


def apply_zset(con, name: str, zset, f, pk: tuple[str, ...], *, retain_t=None, retain_n=None,
               compact_threshold=None) -> None:
    """Append a Z-set ``zset`` (user columns + ``_duckstring_d``) to the merge main's append-only
    ``__changelog``. The main is **log-structured**: the changelog is the source of truth and the clean
    current state is reconstructed on read (:func:`reconstruct_current`) from a base table (written only by
    :func:`checkpoint`) overlaid with the changelog. So a run only ever *appends* its delta here (O(change));
    the base is never touched per run.

    Idempotent replay at the same ``f``: a non-empty change rewrites just this ``f``'s changelog window; an
    empty consolidated change leaves it untouched (so a comprehensive replay, whose diff against the
    already-advanced state is empty, preserves the first attempt's rows)."""
    if f is None:
        raise DeltaError("a Trickle needs the run freshness pond.f — none was set (is this a Trickle run?)")
    if not pk:
        raise DeltaError(f"apply_zset('{name}', ...) needs a primary key — pass pk=...")
    src = unique_name("zset")
    zset.create_view(src, replace=True)
    cols = list(zset.columns)
    if D_COL not in cols:
        raise DeltaError(f"apply_zset('{name}', ...): the relation has no {D_COL} weight column")
    user = [c for c in cols if c != D_COL]
    missing = [c for c in pk if c not in user]
    if missing:
        raise DeltaError(f"apply_zset('{name}', ...): primary key column(s) {missing} not in the relation")
    sel_user = ", ".join(_q(c) for c in user)
    clog = changelog_name(name)
    clog_existed = _table_exists(con, clog)

    # Consolidate by full row (the Z-set `distinct` operator); BIGINT weight (Iceberg can't hold a HUGEINT).
    consol = unique_name("consol")
    con.execute(
        f'CREATE OR REPLACE TEMP TABLE {_q(consol)} AS '
        f'SELECT {sel_user}, CAST(SUM({_q(D_COL)}) AS BIGINT) AS {_q(D_COL)} FROM {_q(src)} '
        f'GROUP BY {sel_user} HAVING SUM({_q(D_COL)}) <> 0'
    )
    nonempty = con.execute(f'SELECT count(*) FROM {_q(consol)}').fetchone()[0] > 0
    _ensure_changelog(con, clog, consol)  # borrows schema from the delta; works before any base exists
    if nonempty:
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(f'DELETE FROM {_q(clog)} WHERE {_q(F_COL)} = {_ts(f)}')  # idempotent replay
            con.execute(
                f'INSERT INTO {_q(clog)} ({sel_user}, {_q(D_COL)}, {_q(F_COL)}) '
                f'SELECT {sel_user}, {_q(D_COL)}, {_ts(f)} FROM {_q(consol)}'
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    _record_meta(con, name, "merge", pk)
    _set_compact_threshold(con, name, compact_threshold)
    cutoff = _apply_retention(con, clog, f, retain_t, retain_n)
    _advance_floor(con, name, bootstrap_f=(f if not clog_existed else None), cutoff=cutoff)


def merge_table(con, name: str, relation, f, pk: tuple[str, ...], *, retain_t=None, retain_n=None,
                compact_threshold=None) -> None:
    """Comprehensive merge: ``relation`` is the **complete current state**. Diff it against the
    reconstructed prior state (base ⊎ changelog) as a full-row Z-set (``new(+1) ⊎ prior(-1)``, consolidated)
    and append the diff to the changelog."""
    if not pk:
        raise DeltaError(f"merge_table('{name}', ...) needs a primary key — pass pk=...")
    cols = list(relation.columns)
    missing = [c for c in pk if c not in cols]
    if missing:
        raise DeltaError(f"merge_table('{name}', ...): primary key column(s) {missing} not in the relation")
    sel = ", ".join(_q(c) for c in cols)
    state = unique_name("state")
    relation.create_view(state, replace=True)
    # The prior is the state *strictly before* this run's f — so a second merge at the same f (or a replay)
    # diffs against the pre-f state and replaces this f's changelog window with the full net change, rather
    # than diffing against its own earlier write at the same f and losing it.
    prior = reconstruct_current(con, name, before=f)
    if prior is not None:
        pv = unique_name("prior")
        _strip_system(prior).create_view(pv, replace=True)  # user columns only (drop _duckstring_f)
        zset = con.sql(
            f'SELECT {sel}, 1 AS {_q(D_COL)} FROM {_q(state)} '
            f'UNION ALL BY NAME SELECT {sel}, -1 AS {_q(D_COL)} FROM {_q(pv)}'
        )
    else:
        zset = con.sql(f'SELECT {sel}, 1 AS {_q(D_COL)} FROM {_q(state)}')
    apply_zset(con, name, zset, f, pk, retain_t=retain_t, retain_n=retain_n,
               compact_threshold=compact_threshold)


def checkpoint(con, name: str, target_f, *, retain_t=None, retain_n=None) -> None:
    """Fold the changelog into the base up to ``target_f`` and advance the fold watermark — the amortised
    O(table) write that keeps the per-run cost O(change). The base is rewritten to the reconstructed state
    (``base ⊎ changelog ≤ target_f``, latest-per-PK, dead versions/tombstones dropped). Crash-safe and
    lock-free: ``f_base`` is advanced *after* the base is replaced, and reads are latest-per-PK over
    ``base ⊎ changelog``, so an interrupted checkpoint just leaves a redundant (idempotent) changelog window."""
    clog = changelog_name(name)
    sql = _reconstruct_sql_for(con, name, upper=target_f)
    if sql is None:
        return
    tmp = unique_name("ckpt")
    con.execute(f'CREATE OR REPLACE TEMP TABLE {_q(tmp)} AS {sql}')   # reads the OLD base + changelog
    con.execute(f'CREATE OR REPLACE TABLE {_q(name)} AS SELECT * FROM {_q(tmp)}')  # replace the base
    con.execute(f'DROP TABLE IF EXISTS {_q(tmp)}')
    _set_f_base(con, name, target_f)
    cutoff = _apply_retention(con, clog, target_f, retain_t, retain_n)
    _advance_floor(con, name, cutoff=cutoff)


# ─── write: incremental aggregation (distributive / algebraic) ──────────────────


def apply_aggregate(con, name, by, metrics, kind, rel, current, f, *, retain_t=None, retain_n=None) -> None:
    """Maintain a grouped aggregate output ``name`` (a merge Trickle keyed by ``by``) incrementally.

    ``metrics`` is ``{out_col: (kind, src_col, how)}`` over count / sum / mean / min / max / var / stddev.
    Raw accumulators live in a registry-only ``_duckstring_agg_{name}`` companion (count; per additive column
    a running sum, non-NULL count, sum-of-squares; per extreme column a stored min & max); the published main
    holds only the derived user columns. ``kind`` is the builder's ``_compute`` class for the input:
    ``incremental`` (a Z-set ΔO → fold weighted contributions, O(δ); min/max extend on insert and **rescan**
    ``current`` — the full current join output — on a retraction of the supporting row), ``comprehensive`` (a
    full clean output → rebuild the accumulators wholesale), or ``empty`` (no-op)."""
    if f is None:
        raise DeltaError("a Trickle needs the run freshness pond.f — none was set (is this a Trickle run?)")
    if kind == "empty":
        return
    by = tuple(by)
    by_list = ", ".join(_q(b) for b in by)
    add_cols, ext_cols = _agg_cols(metrics)              # additive (sum/mean/var/std) and extreme (min/max) cols
    sidx = {c: i for i, c in enumerate(add_cols)}
    eidx = {c: j for j, c in enumerate(ext_cols)}
    state = f"{AGG_STATE_PREFIX}{name}"
    acc_order = (
        ["_a_cnt"]
        + [col for i in range(len(add_cols)) for col in (f"_a_sum_{i}", f"_a_cnt_{i}", f"_a_sumsq_{i}")]
        + [col for j in range(len(ext_cols)) for col in (f"_a_min_{j}", f"_a_max_{j}")]
    )

    if kind == "comprehensive":
        _agg_rebuild(con, state, rel, by_list, add_cols, ext_cols, f)
        derived = con.sql(f"SELECT {by_list}, {_agg_derive(metrics, sidx, eidx)} FROM {_q(state)} WHERE _a_cnt > 0")
        merge_table(con, name, derived, f, by, retain_t=retain_t, retain_n=retain_n)
        return

    # Incremental. Per-group accumulator delta from ΔO (a +1/-1 row contributes ±x to sum, ±x² to sumsq, ±1
    # to the counts — the distributive fold). For extremes: the min/max of the *inserted* (+1) rows, plus a
    # per-group flag for whether the group has any retraction (which forces a rescan, below).
    delta = unique_name("aggd")
    rel.create_view(delta, replace=True)
    dexprs = [f"CAST(SUM({_q(D_COL)}) AS BIGINT) AS _a_cnt"]
    for i, c in enumerate(add_cols):
        dexprs.append(f"COALESCE(SUM({_q(D_COL)} * {_q(c)}), 0) AS _a_sum_{i}")
        dexprs.append(f"CAST(SUM(CASE WHEN {_q(c)} IS NOT NULL THEN {_q(D_COL)} ELSE 0 END) AS BIGINT) AS _a_cnt_{i}")
        dexprs.append(f"COALESCE(SUM({_q(D_COL)} * {_q(c)} * {_q(c)}), 0) AS _a_sumsq_{i}")
    for j, c in enumerate(ext_cols):
        dexprs.append(f"MIN({_q(c)}) FILTER (WHERE {_q(D_COL)} > 0) AS _a_minp_{j}")
        dexprs.append(f"MAX({_q(c)}) FILTER (WHERE {_q(D_COL)} > 0) AS _a_maxp_{j}")
    if ext_cols:
        dexprs.append(f"BOOL_OR({_q(D_COL)} < 0) AS _a_ret")
    dacc = unique_name("dacc")
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE {_q(dacc)} AS "
        f"SELECT {by_list}, {', '.join(dexprs)} FROM {_q(delta)} GROUP BY {by_list}"
    )

    # Rescan the *current* membership for the extreme columns of groups that saw a retraction (the supporting
    # min/max may be gone). Bounded by those groups; append-only groups never rescan.
    rescan = None
    if ext_cols and current is not None:
        cur = unique_name("aggcur")
        current.create_view(cur, replace=True)
        rexprs = [f"MIN({_q(c)}) AS _a_min_{j}, MAX({_q(c)}) AS _a_max_{j}" for j, c in enumerate(ext_cols)]
        rescan = unique_name("aggrs")
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE {_q(rescan)} AS "
            f"SELECT {by_list}, {', '.join(rexprs)} FROM {_q(cur)} "
            f"WHERE ({by_list}) IN (SELECT {by_list} FROM {_q(dacc)} WHERE _a_ret) GROUP BY {by_list}"
        )

    # Merge the delta into the state — additive for the distributive accumulators; for extremes, extend in
    # place from the inserts, or take the rescanned value when the group retracted. For affected groups not
    # already at this f (the replay guard).
    macc = ["CAST(COALESCE(a._a_cnt, 0) + d._a_cnt AS BIGINT) AS _a_cnt"]
    for i in range(len(add_cols)):
        macc.append(f"COALESCE(a._a_sum_{i}, 0) + COALESCE(d._a_sum_{i}, 0) AS _a_sum_{i}")
        macc.append(f"CAST(COALESCE(a._a_cnt_{i}, 0) + COALESCE(d._a_cnt_{i}, 0) AS BIGINT) AS _a_cnt_{i}")
        macc.append(f"COALESCE(a._a_sumsq_{i}, 0) + COALESCE(d._a_sumsq_{i}, 0) AS _a_sumsq_{i}")
    for j in range(len(ext_cols)):
        macc.append(f"(CASE WHEN d._a_ret THEN r._a_min_{j} ELSE least(a._a_min_{j}, d._a_minp_{j}) END) AS _a_min_{j}")
        macc.append(f"(CASE WHEN d._a_ret THEN r._a_max_{j} ELSE greatest(a._a_max_{j}, d._a_maxp_{j}) END) AS _a_max_{j}")
    rescan_join = f" LEFT JOIN {_q(rescan)} r USING ({by_list})" if rescan is not None else ""
    merged = unique_name("magg")
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE {_q(merged)} AS "
        f"SELECT {', '.join(f'd.{_q(b)} AS {_q(b)}' for b in by)}, {', '.join(macc)} "
        f"FROM {_q(dacc)} d LEFT JOIN {_q(state)} a USING ({by_list}){rescan_join} "
        f"WHERE a.{_q(F_COL)} IS DISTINCT FROM {_ts(f)}"
    )
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute(f"DELETE FROM {_q(state)} WHERE ({by_list}) IN (SELECT {by_list} FROM {_q(merged)})")
        con.execute(
            f"INSERT INTO {_q(state)} ({by_list}, {', '.join(acc_order)}, {_q(F_COL)}) "
            f"SELECT {by_list}, {', '.join(acc_order)}, {_ts(f)} FROM {_q(merged)}"
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    con.execute(f"DELETE FROM {_q(state)} WHERE _a_cnt <= 0 AND ({by_list}) IN (SELECT {by_list} FROM {_q(dacc)})")

    # Emit the output delta for the affected groups: new (derived from the updated state) +1, old (the prior
    # published main) −1 — unchanged groups cancel; emptied groups (gone from state) are retracted.
    affected = f"({by_list}) IN (SELECT {by_list} FROM {_q(dacc)})"
    out_cols = list(by) + list(metrics.keys())
    out_sel = ", ".join(_q(c) for c in out_cols)
    new_out = f"SELECT {by_list}, {_agg_derive(metrics, sidx, eidx)} FROM {_q(state)} WHERE {affected} AND _a_cnt > 0"
    # The prior published main is the reconstructed current state (the agg output is a log-structured merge).
    recon = _reconstruct_sql_for(con, name)
    old_out = f"SELECT {out_sel} FROM ({recon}) WHERE {affected}" if recon \
        else f"SELECT {out_sel} FROM {_q(state)} WHERE 1=0"
    delta_out = con.sql(
        f"SELECT {out_sel}, 1 AS {_q(D_COL)} FROM ({new_out}) "
        f"UNION ALL BY NAME SELECT {out_sel}, -1 AS {_q(D_COL)} FROM ({old_out})"
    )
    apply_zset(con, name, delta_out, f, by, retain_t=retain_t, retain_n=retain_n)


def _agg_cols(metrics):
    """The columns needing **additive** accumulators (sum/mean/var/stddev) and **extreme** accumulators
    (min/max), each de-duplicated and order-stable."""
    add, ext = [], []
    for _out, (k, c, _how) in metrics.items():
        if k in ("sum", "mean", "var", "stddev") and c not in add:
            add.append(c)
        if k in ("min", "max") and c not in ext:
            ext.append(c)
    return add, ext


def _agg_rebuild(con, state, rel, by_list, add_cols, ext_cols, f) -> None:
    """(Re)build the accumulator state wholesale from a clean full output ``rel`` — the comprehensive path
    (bootstrap / coverage-miss). Idempotent: same input → same state."""
    src = unique_name("aggfull")
    rel.create_view(src, replace=True)
    exprs = ["CAST(count(*) AS BIGINT) AS _a_cnt"]
    for i, c in enumerate(add_cols):
        exprs.append(f"COALESCE(SUM({_q(c)}), 0) AS _a_sum_{i}")
        exprs.append(f"CAST(COUNT({_q(c)}) AS BIGINT) AS _a_cnt_{i}")
        exprs.append(f"COALESCE(SUM({_q(c)} * {_q(c)}), 0) AS _a_sumsq_{i}")
    for j, c in enumerate(ext_cols):
        exprs.append(f"MIN({_q(c)}) AS _a_min_{j}")
        exprs.append(f"MAX({_q(c)}) AS _a_max_{j}")
    con.execute(f"DROP TABLE IF EXISTS {_q(state)}")
    con.execute(
        f"CREATE TABLE {_q(state)} AS "
        f"SELECT {by_list}, {', '.join(exprs)}, {_ts(f)} AS {_q(F_COL)} FROM {_q(src)} GROUP BY {by_list}"
    )


def _agg_derive(metrics, sidx, eidx) -> str:
    """The select list deriving the user-facing aggregate columns from the accumulator state."""
    exprs = []
    for out, (k, c, how) in metrics.items():
        if k == "count":
            e = "_a_cnt"
        elif k == "sum":
            i = sidx[c]
            e = f"(CASE WHEN _a_cnt_{i} = 0 THEN NULL ELSE _a_sum_{i} END)"
        elif k == "mean":
            i = sidx[c]
            e = f"(CASE WHEN _a_cnt_{i} = 0 THEN NULL ELSE _a_sum_{i}::DOUBLE / _a_cnt_{i} END)"
        elif k == "min":
            e = f"_a_min_{eidx[c]}"
        elif k == "max":
            e = f"_a_max_{eidx[c]}"
        elif k in ("var", "stddev"):
            i = sidx[c]
            n, s, sq = f"_a_cnt_{i}", f"_a_sum_{i}::DOUBLE", f"_a_sumsq_{i}::DOUBLE"
            min_n, denom = (2, f"({n} - 1)") if how == "sample" else (1, n)
            v = f"GREATEST(({sq} - {s} * {s} / {n}) / {denom}, 0)"   # clamp float error to ≥ 0
            inner = v if k == "var" else f"SQRT({v})"
            e = f"(CASE WHEN _a_cnt_{i} < {min_n} THEN NULL ELSE {inner} END)"
        else:
            raise DeltaError(f"aggregate metric '{out}': unsupported kind {k!r}")
        exprs.append(f"{e} AS {_q(out)}")
    return ", ".join(exprs)


def _ensure_changelog(con, clog: str, schema_src: str) -> None:
    """Create the changelog table if absent, borrowing its column schema (user columns + the BIGINT
    ``_duckstring_d`` weight) from the consolidated-delta table ``schema_src`` and adding ``_duckstring_f``.
    Borrowing from the delta (not the main) means this works on a bootstrap, before the main exists."""
    if _table_exists(con, clog):
        return
    con.execute(
        f'CREATE TABLE {_q(clog)} AS '
        f'SELECT *, CAST(NULL AS TIMESTAMPTZ) AS {_q(F_COL)} FROM {_q(schema_src)} LIMIT 0'
    )


# ─── read: source.delta ───────────────────────────────────────────────────────


class Delta:
    """A source's change over the window ``(previous_f, f]`` as a **Z-set** (:attr:`zset` — user columns +
    ``_duckstring_d``).

    :attr:`is_full` is ``True`` when this is a *full read*, not a windowed delta — a bootstrap, a
    coverage-miss (the consumer fell behind the source's retained history / its floor), or a **changed**
    overwrite (plain Ripple) source. A full read is the whole current state at weight ``+1``; a consumer
    must **absorb it comprehensively** (recompute its whole output and diff against its own main), never
    treat it as an incremental slice. An *unchanged* overwrite source returns an **empty** Z-set
    (``is_full`` False, no rows) — it contributes only as a stable history operand."""

    def __init__(self, con, pk: tuple[str, ...], zset, *, is_full: bool = False) -> None:
        self.con = con
        self.pk = tuple(pk)
        self.zset = zset
        self.is_full = is_full

    def is_empty(self) -> bool:
        return self.zset.aggregate("count(*) AS n").fetchone()[0] == 0

    def keys_count(self) -> int:
        """Distinct rows that changed — the cost the change-fraction threshold measures against."""
        return self.zset.aggregate("count(*) AS n").fetchone()[0]

    @property
    def upserts(self):
        """The net present rows (weight ``> 0``), user columns only — a convenience for hand-rolled
        consumers and the comprehensive case."""
        consolidated = self._consolidated()
        return _strip_system(consolidated.filter(f"{_q(D_COL)} > 0"))

    @property
    def deletes(self):
        """The PKs that were removed — keys appearing only with retractions (no surviving positive row)."""
        if not self.pk:
            return self.zset.filter("1=0").project(", ".join(_q(c) for c in self.zset.columns if c != D_COL))
        consolidated = self._consolidated()
        pk_sel = ", ".join(_q(c) for c in self.pk)
        neg = consolidated.filter(f"{_q(D_COL)} < 0").project(pk_sel)
        pos = consolidated.filter(f"{_q(D_COL)} > 0").project(pk_sel)
        return neg.except_(pos)

    def _consolidated(self):
        user = [c for c in self.zset.columns if c != D_COL]
        sel = ", ".join(_q(c) for c in user)
        return self.con.sql(
            f'SELECT {sel}, SUM({_q(D_COL)}) AS {_q(D_COL)} FROM ({self.zset.sql_query()}) '
            f'GROUP BY {sel} HAVING SUM({_q(D_COL)}) <> 0'
        )


def read_delta(con, data_dir: Path, table: str, previous_f, f, *, dp) -> Delta:
    """Resolve ``table``'s mode in ``data_dir`` and read its Z-set change over ``(previous_f, f]``."""
    from datetime import datetime

    from .context import NEVER

    meta = load_sidecar(data_dir).get(table, {})
    mode = meta.get("mode", "overwrite")
    pk = tuple(meta.get("pk", ()))
    floor = datetime.fromisoformat(meta["floor"]) if meta.get("floor") else None

    if mode == "append":
        return _read_append_delta(con, data_dir, table, previous_f, f, pk, dp, NEVER, floor)
    if mode == "merge":
        return _read_merge_delta(con, data_dir, table, previous_f, f, pk, dp, NEVER, floor)

    # overwrite source (a plain Ripple): no history. If its published freshness `f` shows it has not
    # advanced past the consumer's previous_f, it is unchanged → an empty delta (stable history operand).
    # Otherwise (advanced / unknown / bootstrap) → a full read at +1, forcing the comprehensive path.
    src_f = datetime.fromisoformat(meta["f"]) if meta.get("f") else None
    state = _strip_system(con.sql(dp.read_select(data_dir, table, as_of=f)))
    if previous_f != NEVER and src_f is not None and src_f <= previous_f:
        return Delta(con, pk, _as_zset(state, 1).filter("1=0"), is_full=False)
    return Delta(con, pk, _as_zset(state, 1), is_full=True)


def _as_zset(relation, weight: int):
    """Tag every row of a clean relation with the constant Z-set weight ``weight``."""
    return relation.project(f"*, {int(weight)} AS {_q(D_COL)}")


def _covered(previous_f, NEVER, floor, oldest) -> bool:
    """Whether ``previous_f`` is covered by the available history (a windowed read is valid)."""
    if previous_f == NEVER:
        return False
    bound = floor if floor is not None else oldest
    return bound is None or previous_f >= bound


def _read_append_delta(con, data_dir, table, previous_f, f, pk, dp, NEVER, floor) -> Delta:
    # As-of pin to `f`: read the one Source snapshot at this run's freshness (the data plane pins it; the
    # coverage probe shares the same SELECT so a mid-run republish can't make them see different snapshots).
    return _append_delta_from_sql(con, dp.read_select(data_dir, table, as_of=f), previous_f, f, pk, NEVER, floor)


def _append_delta_from_sql(con, hist_sql, previous_f, f, pk, NEVER, floor) -> Delta:
    """The append-delta core over a single history SELECT — shared by the published read and the in-run
    registry read. Append rows are all present (``+1``); never retracted."""
    rel = con.sql(hist_sql)  # includes _duckstring_f
    oldest = con.sql(f"SELECT min({_q(F_COL)}) FROM ({hist_sql})").fetchone()[0]
    full = not _covered(previous_f, NEVER, floor, oldest)
    upper = f"{_q(F_COL)} <= {_ts(f)}"
    cond = upper if full else f"{_q(F_COL)} > {_ts(previous_f)} AND {upper}"
    return Delta(con, pk, _as_zset(_strip_system(rel.filter(cond)), 1), is_full=full)


def _read_merge_delta(con, data_dir, table, previous_f, f, pk, dp, NEVER, floor) -> Delta:
    clog = changelog_name(table)
    try:
        clog_sql = dp.read_select(data_dir, clog, as_of=f)  # as-of pin to this run's freshness
    except FileNotFoundError:
        clog_sql = None
    main_sql = dp.read_select(data_dir, table, as_of=f)
    return _merge_delta_from_sql(con, main_sql, clog_sql, previous_f, f, pk, NEVER, floor)


def _merge_delta_from_sql(con, main_sql, clog_sql, previous_f, f, pk, NEVER, floor) -> Delta:
    """The merge-delta core over two source SELECTs — a clean *main* and its *changelog* (``None`` if the
    changelog is unavailable). Shared by the published read (``_read_merge_delta``, over the data plane) and
    the in-run registry read (:func:`read_registry_delta`). ``previous_f`` covered by the floor → a windowed
    Z-set; otherwise a full read at ``+1``."""
    oldest = None
    if clog_sql is not None:
        oldest = con.sql(f"SELECT min({_q(F_COL)}) FROM ({clog_sql})").fetchone()[0]
    full = clog_sql is None or not _covered(previous_f, NEVER, floor, oldest)
    if full:
        main = _strip_system(con.sql(main_sql))
        return Delta(con, pk, _as_zset(main, 1), is_full=True)
    # Window the changelog and consolidate by full row (the net Z-set over the window — multiple updates
    # and delete-then-re-add collapse). Inlined as a self-contained subquery (over immutable read_parquet),
    # NOT a named view: several read_delta calls in one run would share a view name and re-bind.
    user = [c for c in con.sql(clog_sql).columns if c not in (F_COL, D_COL)]
    sel = ", ".join(_q(c) for c in user)
    zset = con.sql(
        f"SELECT {sel}, SUM({_q(D_COL)}) AS {_q(D_COL)} FROM ("
        f"  SELECT * FROM ({clog_sql}) "
        f"  WHERE {_q(F_COL)} > {_ts(previous_f)} AND {_q(F_COL)} <= {_ts(f)}"
        f") GROUP BY {sel} HAVING SUM({_q(D_COL)}) <> 0"
    )
    return Delta(con, pk, zset, is_full=False)


def read_registry_delta(con, table, previous_f, f, pk) -> Delta:
    """The Z-set change a **just-written** Trickle ``table`` exposes over ``(previous_f, f]``, read back from
    the **registry** (its clean main + ``__changelog`` for a merge, or its history for an append) rather than
    the published data plane. Used to thread a mid-chain ``pond.trickle(...).merge(name)`` / ``.append(name)``
    forward as an in-run join operand: nothing is published until end-of-run, so a downstream ``.join(...)``
    in the same Ripple can't go through the normal (data-plane) ``read_delta``. The coverage rule is
    identical to the published read — a bootstrap (floor just set to ``f``) or a retention/coverage gap
    yields a full read (``is_full``), forcing the downstream comprehensive path; otherwise a windowed delta
    (empty if the write produced nothing this run)."""
    from datetime import datetime

    from .context import NEVER

    m = read_meta(con).get(table, {})
    floor = datetime.fromisoformat(m["floor"]) if m.get("floor") else None
    out_pk = normalize_pk(pk)
    if m.get("mode") == "append":
        return _append_delta_from_sql(con, f'SELECT * FROM {_q(table)}', previous_f, f, out_pk, NEVER, floor)
    clog = changelog_name(table)
    clog_sql = f'SELECT * FROM {_q(clog)}' if _table_exists(con, clog) else None
    # The full-read main is the *reconstructed* current state (base ⊎ changelog), not the bare base table.
    main_sql = _reconstruct_sql_for(con, table) or f'SELECT * FROM {_q(table)}'
    return _merge_delta_from_sql(con, main_sql, clog_sql, previous_f, f, out_pk, NEVER, floor)


def _strip_system(rel):
    """Project out any ``_duckstring_*`` system columns — a clean user-column view of the rows."""
    from .context import SYSTEM_PREFIX as RESERVED_PREFIX

    sys_cols = [c for c in rel.columns if c.startswith(RESERVED_PREFIX)]
    if not sys_cols:
        return rel
    return rel.project(f"* EXCLUDE ({', '.join(_q(c) for c in sys_cols)})")
