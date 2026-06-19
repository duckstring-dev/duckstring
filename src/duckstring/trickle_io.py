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


# System columns — the reserved ``_duckstring_*`` namespace (see :mod:`duckstring.dataplane`).
F_COL = "_duckstring_f"
D_COL = "_duckstring_d"  # the Z-set weight (+1 present / -1 retraction)

# A merge Trickle's CDC stream lives in a ``{table}__changelog`` companion registry table.
CHANGELOG_SUFFIX = "__changelog"
# The mode/PK registry: one row per Trickle output table. Named in the reserved namespace so
# ``registry_tables`` hides it from the publish set.
META_TABLE = "_duckstring_trickle"
# The published sidecar carrying mode/PK/floor + the source run freshness to cross-Pond readers.
SIDECAR = "_trickle.json"


class DeltaError(ValueError):
    """A delta read or Trickle write was used incompatibly."""


def changelog_name(table: str) -> str:
    return f"{table}{CHANGELOG_SUFFIX}"


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
    from .dataplane import RESERVED_PREFIX

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
        f"(table_name VARCHAR PRIMARY KEY, mode VARCHAR, pk VARCHAR, floor VARCHAR)"
    )


def _record_meta(con, table: str, mode: str, pk: tuple[str, ...]) -> None:
    _ensure_meta(con)
    # Preserve any existing floor (a normal incremental run must not reset it).
    con.execute(
        f'INSERT INTO {_q(META_TABLE)} (table_name, mode, pk) VALUES (?, ?, ?) '
        f'ON CONFLICT (table_name) DO UPDATE SET mode=excluded.mode, pk=excluded.pk',
        [table, mode, ",".join(pk)],
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
    """``{table: {"mode", "pk": [...], "floor": iso|None}}`` for every Trickle table (``{}`` if none)."""
    if not _table_exists(con, META_TABLE):
        return {}
    rows = con.execute(f'SELECT table_name, mode, pk, floor FROM {_q(META_TABLE)}').fetchall()
    return {r[0]: {"mode": r[1], "pk": (r[2].split(",") if r[2] else []), "floor": r[3]} for r in rows}


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


# ─── incremental draw (cross-Catchment transfer) ──────────────────────────────
#
# The append-only history a draw can ship *incrementally*: an append Trickle's single table and every
# merge Trickle's __changelog. A merge *main* is clean current state → always shipped wholesale.


def windowable_tables(sidecar: dict[str, dict]) -> set[str]:
    """Published table names whose history a draw can window by ``_duckstring_f`` (append tables + merge
    changelogs). The merge main and plain overwrite output are not windowable (wholesale)."""
    out: set[str] = set()
    for table, meta in sidecar.items():
        if meta.get("mode") == "append":
            out.add(table)
        elif meta.get("mode") == "merge":
            out.add(changelog_name(table))
    return out


def _con_utc():
    import duckdb

    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    return con


def landed_after(data_dir: Path) -> str | None:
    """The freshness a consumer has fully landed = ``min`` over its windowable tables' high-water, where a
    table's high-water is ``max(its floor, max(_duckstring_f))``. ``None`` means *transfer wholesale*."""
    from datetime import datetime

    data_dir = Path(data_dir)
    sidecar = load_sidecar(data_dir)
    windowable = windowable_tables(sidecar)
    if not windowable:
        return None
    con = _con_utc()
    try:
        highs = []
        for table in windowable:
            base = table[: -len(CHANGELOG_SUFFIX)] if table.endswith(CHANGELOG_SUFFIX) else table
            floor = sidecar.get(base, {}).get("floor")
            high = datetime.fromisoformat(floor) if floor else None
            pq = data_dir / f"{table}.parquet"
            if pq.exists():
                rows_max = con.execute(
                    f"SELECT max({_q(F_COL)}) FROM read_parquet('{_sql_lit(pq)}')"
                ).fetchone()[0]
                if rows_max is not None and (high is None or rows_max > high):
                    high = rows_max
            if high is None:
                return None  # nothing landed for this table → wholesale
            highs.append(high)
        return min(highs).isoformat() if highs else None
    finally:
        con.close()


def window_parquet_bytes(pq_path: Path, after_iso: str) -> bytes:
    """The rows of ``pq_path`` newer than ``after_iso`` (``_duckstring_f > after``), as Parquet bytes."""
    import os
    import tempfile

    con = _con_utc()
    fd, tmp = tempfile.mkstemp(suffix=".parquet")
    os.close(fd)
    try:
        con.execute(
            f"COPY (SELECT * FROM read_parquet('{_sql_lit(pq_path)}') "
            f"WHERE {_q(F_COL)} > TIMESTAMPTZ '{after_iso}') TO '{_sql_lit(tmp)}' (FORMAT PARQUET)"
        )
        return Path(tmp).read_bytes()
    finally:
        con.close()
        os.unlink(tmp)


def land_windowed(dest_path: Path, shipped: bytes, after_iso: str) -> None:
    """Land an incremental slice: keep the consumer's rows ``<= after`` and add the shipped rows
    (``> after``) — idempotent. A brand-new table (no destination yet) is shipped whole."""
    import os
    import tempfile

    dest_path = Path(dest_path)
    if not dest_path.exists():
        dest_path.write_bytes(shipped)
        return
    fd, ship_tmp = tempfile.mkstemp(suffix=".parquet")
    os.close(fd)
    Path(ship_tmp).write_bytes(shipped)
    con = _con_utc()
    out_tmp = dest_path.with_suffix(dest_path.suffix + ".tmp")
    try:
        con.execute(
            f"COPY (SELECT * FROM read_parquet('{_sql_lit(dest_path)}') WHERE {_q(F_COL)} <= TIMESTAMPTZ '{after_iso}' "
            f"UNION ALL BY NAME SELECT * FROM read_parquet('{_sql_lit(ship_tmp)}')) "
            f"TO '{_sql_lit(out_tmp)}' (FORMAT PARQUET)"
        )
        out_tmp.replace(dest_path)  # atomic publish
    finally:
        con.close()
        os.unlink(ship_tmp)
        if out_tmp.exists():
            out_tmp.unlink()


def _sql_lit(path) -> str:
    return str(path).replace("'", "''")


# ─── write: append ──────────────────────────────────────────────────────────────


def append_table(
    con, name: str, relation, f, pk: tuple[str, ...], *, validate_pk=False, retain_t=None, retain_n=None
) -> None:
    """Append ``relation``'s rows to the history table ``name``, each stamped ``_duckstring_f = f``.
    Insert-only: no diff, no deletes (its Z-set is all ``+1``). Idempotent at a given ``f`` (rows already
    stamped ``f`` are dropped before re-appending). ``pk`` is recorded as the declared key; with
    ``validate_pk=True`` it is asserted unique across the appended rows and the existing history (raising
    :class:`DeltaError` before any write, so the live table is untouched on a violation)."""
    if f is None:
        raise DeltaError("a Trickle needs the run freshness pond.f — none was set (is this a Trickle run?)")
    src = "_duckstring_ds_append_src"
    relation.create_view(src, replace=True)
    cols = relation.columns
    sel_cols = ", ".join(_q(c) for c in cols)
    first = not _table_exists(con, name)  # the floor anchors at the first append's freshness
    if validate_pk:
        if not pk:
            raise DeltaError(f"append_table('{name}', validate_pk=True) needs a primary key — pass pk=...")
        missing = [c for c in pk if c not in cols]
        if missing:
            raise DeltaError(f"append_table('{name}', validate_pk=True): primary key column(s) {missing} not in the relation")
        pk_list = ", ".join(_q(c) for c in pk)
        dup = con.execute(
            f'SELECT 1 FROM {_q(src)} GROUP BY {pk_list} HAVING count(*) > 1 LIMIT 1'
        ).fetchone()
        if dup:
            raise DeltaError(f"append_table('{name}', validate_pk=True): duplicate primary key {pk} among the appended rows")
        if not first:
            # Collide only against rows from *other* runs — a replay re-appends this f's identical rows.
            coll = con.execute(
                f'SELECT 1 FROM {_q(src)} s JOIN {_q(name)} t USING ({pk_list}) '
                f'WHERE t.{_q(F_COL)} IS DISTINCT FROM {_ts(f)} LIMIT 1'
            ).fetchone()
            if coll:
                raise DeltaError(f"append_table('{name}', validate_pk=True): primary key {pk} already present in history")
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


# ─── write: merge (Z-set apply) ───────────────────────────────────────────────


def apply_zset(con, name: str, zset, f, pk: tuple[str, ...], *, retain_t=None, retain_n=None) -> None:
    """Apply a Z-set ``zset`` (a relation of user columns + ``_duckstring_d``) to the clean *main* table
    ``name`` and record it on the ``__changelog``. ``zset`` is the **change** to the output — its ``+1``
    rows are the new/updated rows, its ``-1`` rows the retractions of superseded/deleted ones.

    The main is the materialised prior output (``O_old``); we update it in place (a copy-on-write upsert),
    never recomputing it. Idempotent replay at the same ``f``: the apply re-deletes-and-re-inserts the same
    keys (a no-op the second time), and an **empty** consolidated change leaves the changelog untouched (so
    a comprehensive replay, whose diff against the already-advanced main is empty, preserves the first
    attempt's changelog rows)."""
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
    pk_list = ", ".join(_q(c) for c in pk)
    clog = changelog_name(name)
    main_exists = _table_exists(con, name)

    # Consolidate by full row: an update's old(-1)/new(+1) survive as distinct rows; a spurious +1/-1 of
    # the same row cancels. This is the Z-set `distinct`/`consolidate` operator. The weight is cast to a
    # BIGINT (the changelog's stored type — a DuckDB SUM widens to HUGEINT, which Iceberg can't hold).
    consol = unique_name("consol")
    con.execute(
        f'CREATE OR REPLACE TEMP TABLE {_q(consol)} AS '
        f'SELECT {sel_user}, CAST(SUM({_q(D_COL)}) AS BIGINT) AS {_q(D_COL)} FROM {_q(src)} '
        f'GROUP BY {sel_user} HAVING SUM({_q(D_COL)}) <> 0'
    )
    nonempty = con.execute(f'SELECT count(*) FROM {_q(consol)}').fetchone()[0] > 0
    # The changelog table always exists before the apply — including a bootstrap, which writes no rows but
    # must still publish an (empty) changelog and give retention a table to trim. Its schema is borrowed
    # from the consolidated delta, so this works before the main exists.
    _ensure_changelog(con, clog, consol)

    # Apply the changelog and the main in ONE transaction, **changelog first**. A comprehensive merge
    # derives its delta against the *current main*, so a crash that left the main advanced past a changelog
    # missing this run's rows would make the replay diff against the already-advanced main, compute an empty
    # delta, and lose the changelog entry for good. Committing both together (single-writer-per-line) means a
    # replay sees either all-old — re-derive and apply — or all-new — an empty delta that no-ops. A bootstrap
    # writes no changelog (a first consumer reads the main; no window predates this run); a normal run
    # rewrites this F's window only when the change is non-empty (the replay-idempotency guard).
    con.execute("BEGIN TRANSACTION")
    try:
        if main_exists:
            if nonempty:
                con.execute(f'DELETE FROM {_q(clog)} WHERE {_q(F_COL)} = {_ts(f)}')
                con.execute(
                    f'INSERT INTO {_q(clog)} ({sel_user}, {_q(D_COL)}, {_q(F_COL)}) '
                    f'SELECT {sel_user}, {_q(D_COL)}, {_ts(f)} FROM {_q(consol)}'
                )
            # CoW upsert: drop every key the change touches, re-insert the surviving positive rows.
            con.execute(f'DELETE FROM {_q(name)} WHERE ({pk_list}) IN (SELECT {pk_list} FROM {_q(consol)})')
            con.execute(f'INSERT INTO {_q(name)} SELECT {sel_user} FROM {_q(consol)} WHERE {_q(D_COL)} > 0')
        else:  # bootstrap: stand up the clean main from the +1 rows
            con.execute(f'CREATE TABLE {_q(name)} AS SELECT {sel_user} FROM {_q(consol)} WHERE {_q(D_COL)} > 0')
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    _record_meta(con, name, "merge", pk)
    cutoff = _apply_retention(con, clog, f, retain_t, retain_n)
    _advance_floor(con, name, bootstrap_f=(f if not main_exists else None), cutoff=cutoff)


def merge_table(con, name: str, relation, f, pk: tuple[str, ...], *, retain_t=None, retain_n=None) -> None:
    """Comprehensive merge: ``relation`` is the **complete current state**. Diff it against the prior main
    as a Z-set (``new(+1) ⊎ main(-1)``, consolidated by full row — rows present in both cancel, only-in-new
    are inserts/updates ``+1``, only-in-main are retractions ``-1``) and apply it. No per-row hash needed;
    the main holds pure user columns."""
    if not pk:
        raise DeltaError(f"merge_table('{name}', ...) needs a primary key — pass pk=...")
    cols = list(relation.columns)
    missing = [c for c in pk if c not in cols]
    if missing:
        raise DeltaError(f"merge_table('{name}', ...): primary key column(s) {missing} not in the relation")
    sel = ", ".join(_q(c) for c in cols)
    state = unique_name("state")
    relation.create_view(state, replace=True)
    if _table_exists(con, name):
        zset = con.sql(
            f'SELECT {sel}, 1 AS {_q(D_COL)} FROM {_q(state)} '
            f'UNION ALL BY NAME SELECT {sel}, -1 AS {_q(D_COL)} FROM {_q(name)}'
        )
    else:
        zset = con.sql(f'SELECT {sel}, 1 AS {_q(D_COL)} FROM {_q(state)}')
    apply_zset(con, name, zset, f, pk, retain_t=retain_t, retain_n=retain_n)


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

    from .engine.core import NEVER

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
    # As-of pin to `f`: read the one Source snapshot at this run's freshness for BOTH the data and the
    # `oldest` coverage probe, so a mid-run republish can't make them see different snapshots (read skew).
    rel = con.sql(dp.read_select(data_dir, table, as_of=f))  # includes _duckstring_f
    oldest = con.sql(f"SELECT min({_q(F_COL)}) FROM ({dp.read_select(data_dir, table, as_of=f)})").fetchone()[0]
    full = not _covered(previous_f, NEVER, floor, oldest)
    upper = f"{_q(F_COL)} <= {_ts(f)}"
    cond = upper if full else f"{_q(F_COL)} > {_ts(previous_f)} AND {upper}"
    rows = _strip_system(rel.filter(cond))  # append rows are all present (+1); never retracted
    return Delta(con, pk, _as_zset(rows, 1), is_full=full)


def _read_merge_delta(con, data_dir, table, previous_f, f, pk, dp, NEVER, floor) -> Delta:
    clog = changelog_name(table)
    try:
        clog_sql = dp.read_select(data_dir, clog, as_of=f)  # as-of pin to this run's freshness
    except FileNotFoundError:
        clog_sql = None
    oldest = None
    if clog_sql is not None:
        oldest = con.sql(f"SELECT min({_q(F_COL)}) FROM ({clog_sql})").fetchone()[0]
    full = clog_sql is None or not _covered(previous_f, NEVER, floor, oldest)
    if full:
        main = _strip_system(con.sql(dp.read_select(data_dir, table, as_of=f)))
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


def _strip_system(rel):
    """Project out any ``_duckstring_*`` system columns — a clean user-column view of the rows."""
    from .dataplane import RESERVED_PREFIX

    sys_cols = [c for c in rel.columns if c.startswith(RESERVED_PREFIX)]
    if not sys_cols:
        return rel
    return rel.project(f"* EXCLUDE ({', '.join(_q(c) for c in sys_cols)})")
