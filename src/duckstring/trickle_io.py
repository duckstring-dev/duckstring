"""Trickle: incremental I/O and transfer — the history-preserving Ripple variant.

A **Trickle** is a Ripple that maintains *history* instead of overwriting wholesale, so a consumer can
read just the rows that changed in the window ``(previous_f, f]`` (a small **delta out**, the win — see
``plans/trickle.md``). It delivers incremental I/O and transfer, **not** incremental computation: joins
still recompute fully; the gain is the small write/read, not less work in.

The freshness stamp ``_duckstring_f`` **lives in the data** and is read as a **content predicate**
(``WHERE _duckstring_f > previous_f AND _duckstring_f <= f``), never a snapshot cursor — so the window
read works regardless of compaction and over either data plane. The whole ``_duckstring_*`` namespace is
framework-owned (rejected from user output by the data plane); Trickle's system columns are:

- ``_duckstring_f``    — the run's freshness, stamped on every history/changelog row;
- ``_duckstring_op``   — ``upsert`` / ``delete`` in a merge Trickle's changelog;
- ``_duckstring_hash`` — the change-detection digest, stored in a merge Trickle's clean *main* table.

Two write modes (binary; no append-then-compact middle):

- **append** — insert-only, trust-the-writer. One table, append-only: it is at once the history, the
  full-read source, and the delta source. No PK uniqueness check, no diff, no deletes.
- **merge** — upsert (+delete) with auto change-detection. A clean *main* table (one row per PK, no
  tombstones — ``SELECT *`` over it is the current state) **plus** an append-only *changelog* CDC stream
  (``__changelog`` companion). ``comprehensive=True`` (default, safe) diffs the full new state against
  the prior main via ``_duckstring_hash`` to derive inserts/updates/deletes automatically;
  ``comprehensive=False`` (expert) applies a supplied partial change-set + explicit ``deletes``.

A consumer reads a source's delta via ``pond.read_delta("source.table")`` → a :class:`Delta`
(``.upserts`` / ``.deletes`` / ``.keys()``); the merge collapses the changelog window per PK to the
latest op (handles delete-then-re-add). The partial-path helpers (:meth:`KeySet.union`,
``pond.keys_joining``, :meth:`KeySet.dropped`) do the affected-key bookkeeping for ``comprehensive=False``
without imposing a compute layer — they operate on key-set relations only.

Storage is kept in the Pond's **registry** (the compute substrate that persists across runs) and
published wholesale by the data plane each run; the window read prunes on the consumer side via the
content predicate. Mode + PK are recorded in a registry meta table and mirrored to a ``_trickle.json``
sidecar in the published ``data_dir`` so a cross-Pond reader (which has no access to the producer's
registry) can resolve them.
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
OP_COL = "_duckstring_op"
HASH_COL = "_duckstring_hash"

OP_UPSERT = "upsert"
OP_DELETE = "delete"

# A merge Trickle's CDC stream lives in a ``{table}__changelog`` companion registry table.
CHANGELOG_SUFFIX = "__changelog"
# The mode/PK registry: one row per Trickle output table. Named in the reserved namespace so
# ``registry_tables`` hides it from the publish set.
META_TABLE = "_duckstring_trickle"
# The published sidecar carrying mode/PK to cross-Pond readers (they can't see the registry meta table).
SIDECAR = "_trickle.json"


class DeltaError(ValueError):
    """A delta read or partial-merge helper was used incompatibly (e.g. a non-PK delta-side join)."""


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


def _hash_expr(nonpk: list[str], alias: str | None = None) -> str:
    """A 64-bit change-detection digest over the non-PK columns in schema order. Per-PK comparison
    (new vs old for the same key) keeps it collision-safe (≈2⁻⁶⁴ per changed row; no birthday bound).
    Casts to VARCHAR to canonicalise types/nulls so the hash is stable run to run."""
    if not nonpk:
        return "'0'"  # all-PK table: nothing non-key can change → only insert/delete
    pref = f"{alias}." if alias else ""
    items = ", ".join(f"CAST({pref}{_q(c)} AS VARCHAR)" for c in nonpk)
    # As VARCHAR (not the raw UBIGINT hash): per-PK equality is all the diff needs, and an unsigned 64-bit
    # value overflows the Iceberg/pyiceberg signed-long column stats.
    return f"CAST(hash(list_value({items})) AS VARCHAR)"


def _apply_retention(con, table: str, f, retain_t, retain_n) -> None:
    """Bound a history/changelog table's retained window at write time — a **lag SLA**, not a
    correctness control (a consumer behind the retained window falls back to a full read of the clean
    state; see :func:`read_delta`). Both are opt-in (``None`` keeps everything, the audit/replay choice):

    - ``retain_t`` (a ``timedelta``): drop rows stamped older than ``f - retain_t`` — time-based, so it
      scales with run frequency. The current run's ``f`` rows are always kept (``f - retain_t <= f``).
    - ``retain_n`` (a count): keep only the newest ``retain_n`` distinct ``_duckstring_f`` runs.

    The coverage watermark for the window read is just ``min(_duckstring_f)`` over what remains, so
    trimming here automatically advances it — no separate watermark to store."""
    if retain_t is not None:
        cutoff = f - retain_t
        con.execute(f'DELETE FROM {_q(table)} WHERE {_q(F_COL)} < {_ts(cutoff)}')
    if retain_n is not None and retain_n >= 1:
        con.execute(
            f'DELETE FROM {_q(table)} WHERE {_q(F_COL)} < ('
            f'SELECT min(g) FROM (SELECT DISTINCT {_q(F_COL)} AS g FROM {_q(table)} '
            f'ORDER BY g DESC LIMIT {int(retain_n)}))'
        )


# ─── meta (mode + PK) ─────────────────────────────────────────────────────────


def _ensure_meta(con) -> None:
    con.execute(
        f'CREATE TABLE IF NOT EXISTS {_q(META_TABLE)} '
        f"(table_name VARCHAR PRIMARY KEY, mode VARCHAR, pk VARCHAR)"
    )


def _record_meta(con, table: str, mode: str, pk: tuple[str, ...]) -> None:
    _ensure_meta(con)
    con.execute(
        f'INSERT OR REPLACE INTO {_q(META_TABLE)} (table_name, mode, pk) VALUES (?, ?, ?)',
        [table, mode, ",".join(pk)],
    )


def read_meta(con) -> dict[str, dict]:
    """``{table: {"mode", "pk": [...]}}`` for every Trickle table in this registry (``{}`` if none)."""
    if not _table_exists(con, META_TABLE):
        return {}
    rows = con.execute(f'SELECT table_name, mode, pk FROM {_q(META_TABLE)}').fetchall()
    return {r[0]: {"mode": r[1], "pk": (r[2].split(",") if r[2] else [])} for r in rows}


def write_sidecar(data_dir: Path, meta: dict[str, dict]) -> None:
    """Publish mode/PK next to the data so a cross-Pond reader can resolve a Trickle source."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = {t: {"mode": m["mode"], "pk": list(m["pk"])} for t, m in meta.items()}
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
    """The published table names whose history a draw can window by ``_duckstring_f`` (append tables +
    merge changelogs). The merge main and plain overwrite output are not windowable (wholesale)."""
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
    """The freshness a consumer has fully landed = ``min`` over its windowable tables' ``max
    (_duckstring_f)`` (conservative, so no table is under-served). ``None`` — bootstrap (no sidecar) or
    any windowable table empty — means *transfer wholesale*."""
    data_dir = Path(data_dir)
    windowable = windowable_tables(load_sidecar(data_dir))
    if not windowable:
        return None
    con = _con_utc()
    try:
        maxes = []
        for table in windowable:
            pq = data_dir / f"{table}.parquet"
            if not pq.exists():
                continue
            m = con.execute(
                f"SELECT max({_q(F_COL)}) FROM read_parquet('{_sql_lit(pq)}')"
            ).fetchone()[0]
            if m is None:
                return None  # an empty windowable table → fall back to a wholesale transfer
            maxes.append(m)
        return min(maxes).isoformat() if maxes else None
    finally:
        con.close()


def window_parquet_bytes(pq_path: Path, after_iso: str) -> bytes:
    """The rows of ``pq_path`` newer than ``after_iso`` (``_duckstring_f > after``), as Parquet bytes —
    the producer's incremental slice for a draw."""
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
    (``> after``) — idempotent (a re-transfer replaces, never duplicates, the ``> after`` rows). A brand-
    new table (no destination yet) is shipped whole, so it just writes through."""
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


# ─── write: append ────────────────────────────────────────────────────────────


def append_table(con, name: str, relation, f, pk: tuple[str, ...], *, retain_t=None, retain_n=None) -> None:
    """Append ``relation``'s rows to the history table ``name``, each stamped ``_duckstring_f = f``.
    Insert-only: no PK uniqueness check, no diff. Idempotent at a given ``f`` (replay/retry re-run at the
    same freshness): rows already stamped ``f`` are dropped before re-appending. ``retain_t`` /
    ``retain_n`` bound the kept history (see :func:`_apply_retention`)."""
    if f is None:
        raise DeltaError("a Trickle needs the run freshness pond.f — none was set (is this a Trickle run?)")
    src = "_duckstring_ds_append_src"
    relation.create_view(src, replace=True)
    cols = relation.columns
    sel_cols = ", ".join(_q(c) for c in cols)
    if not _table_exists(con, name):
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
    _apply_retention(con, name, f, retain_t, retain_n)


# ─── write: merge ─────────────────────────────────────────────────────────────


def merge_table(
    con, name: str, relation, f, pk: tuple[str, ...], *,
    comprehensive: bool, deletes=None, retain_t=None, retain_n=None,
) -> None:
    """Upsert ``relation`` into the clean *main* table ``name`` and append the changes to its
    ``__changelog`` CDC stream, stamped ``_duckstring_f = f``.

    ``comprehensive=True`` (default): ``relation`` is the *complete* current state — diff it against the
    prior main (via ``_duckstring_hash``) to derive inserts/updates/deletes; the main is overwritten.
    ``comprehensive=False``: ``relation`` is a *partial* change-set (upserts only) and ``deletes`` (a
    relation of PK rows) the explicit removals — the main is upserted in place. ``retain_t`` / ``retain_n``
    bound the kept *changelog* history (the main is the clean current state and is never trimmed)."""
    if f is None:
        raise DeltaError("a Trickle needs the run freshness pond.f — none was set (is this a Trickle run?)")
    if not pk:
        raise DeltaError(f"merge_table('{name}', ...) needs a primary key (declare @trickle(pk=...) or pass pk=...)")
    cols = list(relation.columns)
    missing = [c for c in pk if c not in cols]
    if missing:
        raise DeltaError(f"merge_table('{name}', ...): primary key column(s) {missing} not in the relation")
    nonpk = [c for c in cols if c not in pk]
    clog = changelog_name(name)

    src = "_duckstring_ds_merge_src"
    relation.create_view(src, replace=True)
    new = "_duckstring_ds_merge_new"
    con.execute(
        f'CREATE OR REPLACE TEMP TABLE {_q(new)} AS '
        f'SELECT {", ".join(_q(c) for c in cols)}, {_hash_expr(nonpk)} AS {_q(HASH_COL)} FROM {_q(src)}'
    )

    main_exists = _table_exists(con, name)
    _ensure_changelog(con, clog, cols)

    if comprehensive:
        if deletes is not None:
            raise DeltaError("merge_table(comprehensive=True) derives deletes from the diff — drop the deletes= argument")
        _merge_comprehensive(con, name, clog, new, cols, nonpk, pk, f, main_exists)
    else:
        con.execute(f'DELETE FROM {_q(clog)} WHERE {_q(F_COL)} = {_ts(f)}')  # idempotent replay (re-apply supplied set)
        _merge_partial(con, name, clog, new, cols, nonpk, pk, f, main_exists, deletes)
    _record_meta(con, name, "merge", pk)
    _apply_retention(con, clog, f, retain_t, retain_n)  # bound the changelog; the main is current-state


def _ensure_changelog(con, clog: str, cols: list[str]) -> None:
    if _table_exists(con, clog):
        return
    src = "_duckstring_ds_merge_src"  # the new-state view, for its column types
    sel = ", ".join(_q(c) for c in cols)
    con.execute(
        f'CREATE TABLE {_q(clog)} AS SELECT {sel}, '
        f'CAST(NULL AS VARCHAR) AS {_q(OP_COL)}, CAST(NULL AS TIMESTAMPTZ) AS {_q(F_COL)} '
        f'FROM {_q(src)} LIMIT 0'
    )


def _on(pk: tuple[str, ...], a: str, b: str) -> str:
    return " AND ".join(f"{a}.{_q(c)} = {b}.{_q(c)}" for c in pk)


def _merge_comprehensive(con, name, clog, new, cols, nonpk, pk, f, main_exists) -> None:
    sel_user = ", ".join(_q(c) for c in cols)
    pk_list = ", ".join(_q(c) for c in pk)
    # Stage this run's CDC delta (upserts + deletes) in a temp with the changelog schema, derived against
    # the *current* main (the state before this F). Apply it to the changelog only when non-empty — this
    # is what makes a comprehensive replay idempotent: the main is overwritten in place (the diff's prior
    # snapshot is destroyed), so a re-run after a successful apply derives an empty delta; leaving the
    # changelog untouched then preserves the F rows the first attempt already wrote.
    delta = "_duckstring_ds_cdelta"
    con.execute(f'CREATE OR REPLACE TEMP TABLE {_q(delta)} AS SELECT * FROM {_q(clog)} LIMIT 0')
    if main_exists:
        # upserts = rows new-or-changed vs the prior main (hash differs); deletes = PKs gone from new.
        con.execute(
            f'INSERT INTO {_q(delta)} ({sel_user}, {_q(OP_COL)}, {_q(F_COL)}) '
            f'SELECT {", ".join("n." + _q(c) for c in cols)}, \'{OP_UPSERT}\', {_ts(f)} '
            f'FROM {_q(new)} n LEFT JOIN {_q(name)} o ON {_on(pk, "n", "o")} '
            f'WHERE o.{_q(pk[0])} IS NULL OR o.{_q(HASH_COL)} IS DISTINCT FROM n.{_q(HASH_COL)}'
        )
        con.execute(
            f'INSERT INTO {_q(delta)} ({pk_list}, {_q(OP_COL)}, {_q(F_COL)}) '
            f'SELECT {", ".join("o." + _q(c) for c in pk)}, \'{OP_DELETE}\', {_ts(f)} '
            f'FROM {_q(name)} o LEFT JOIN {_q(new)} n ON {_on(pk, "o", "n")} '
            f'WHERE n.{_q(pk[0])} IS NULL'
        )
    else:
        con.execute(
            f'INSERT INTO {_q(delta)} ({sel_user}, {_q(OP_COL)}, {_q(F_COL)}) '
            f'SELECT {sel_user}, \'{OP_UPSERT}\', {_ts(f)} FROM {_q(new)}'
        )
    if con.execute(f'SELECT count(*) FROM {_q(delta)}').fetchone()[0] > 0:
        con.execute(f'DELETE FROM {_q(clog)} WHERE {_q(F_COL)} = {_ts(f)}')  # replace this F's window
        con.execute(f'INSERT INTO {_q(clog)} SELECT * FROM {_q(delta)}')
    # The main is the clean current state: overwrite it with the full new state (reuses overwrite — no
    # Iceberg delete-files / CoW needed). Carries _duckstring_hash for the next run's diff.
    con.execute(f'CREATE OR REPLACE TABLE {_q(name)} AS SELECT * FROM {_q(new)}')


def _merge_partial(con, name, clog, new, cols, nonpk, pk, f, main_exists, deletes) -> None:
    sel_user = ", ".join(_q(c) for c in cols)
    pk_list = ", ".join(_q(c) for c in pk)
    if not main_exists:
        # No prior main: stand one up empty so the upsert below lands somewhere.
        con.execute(f'CREATE TABLE {_q(name)} AS SELECT * FROM {_q(new)} LIMIT 0')

    # Changelog: the supplied upserts, then the explicit deletes (PK only; non-PK cols default NULL).
    con.execute(
        f'INSERT INTO {_q(clog)} ({sel_user}, {_q(OP_COL)}, {_q(F_COL)}) '
        f'SELECT {sel_user}, \'{OP_UPSERT}\', {_ts(f)} FROM {_q(new)}'
    )
    del_view = None
    if deletes is not None:
        del_view = "_duckstring_ds_merge_del"
        _delete_relation(deletes).create_view(del_view, replace=True)
        dcols = list(_delete_relation(deletes).columns)
        if list(dcols) != list(pk) and len(dcols) == len(pk):
            # tolerate column-name drift but require matching arity; align positionally
            sel_del = ", ".join(_q(dc) for dc in dcols)
        else:
            sel_del = ", ".join(_q(c) for c in pk)
        con.execute(
            f'INSERT INTO {_q(clog)} ({pk_list}, {_q(OP_COL)}, {_q(F_COL)}) '
            f'SELECT {sel_del}, \'{OP_DELETE}\', {_ts(f)} FROM {_q(del_view)}'
        )

    # Main: drop the upserted PKs and the deleted PKs, then re-insert the upserts (a CoW upsert).
    con.execute(f'DELETE FROM {_q(name)} WHERE ({pk_list}) IN (SELECT {pk_list} FROM {_q(new)})')
    if del_view is not None:
        sel_del = ", ".join(_q(c) for c in (list(_delete_relation(deletes).columns) or pk))
        con.execute(f'DELETE FROM {_q(name)} WHERE ({pk_list}) IN (SELECT {sel_del} FROM {_q(del_view)})')
    con.execute(f'INSERT INTO {_q(name)} SELECT * FROM {_q(new)}')


def _delete_relation(deletes):
    """Coerce a ``deletes`` argument (a :class:`KeySet` or a raw DuckDB relation) to a relation."""
    return deletes.relation if isinstance(deletes, KeySet) else deletes


# ─── read: source.delta ───────────────────────────────────────────────────────


class Delta:
    """A source's change-set over the window ``(previous_f, f]`` — :attr:`upserts` (the net upsert rows,
    user columns), :attr:`deletes` (the deleted PK rows), and :meth:`keys` (their union, a key-set).

    :attr:`is_full` is ``True`` when this is **not** a windowed delta but a *full read* — a bootstrap, a
    coverage-miss (the consumer fell behind the source's retained history / its floor), or an overwrite
    (plain Ripple) source. A full read has no deletes to report (``deletes`` is empty), because deletes
    can only be derived against the consumer's own prior state. So a consumer **must absorb a full read
    comprehensively** (recompute its whole output and ``merge_table(comprehensive=True)``, which diffs
    against its own main and computes the right deletes) — never a partial merge trusting the empty
    ``deletes``. The builder does this automatically; hand-rolled partial consumers must check this."""

    def __init__(self, con, pk: tuple[str, ...], upserts, deletes, *, is_full: bool = False) -> None:
        self.con = con
        self.pk = tuple(pk)
        self.upserts = upserts
        self.deletes = deletes
        self.is_full = is_full

    def keys(self) -> "KeySet":
        """The changed PKs — ``upserts ∪ deletes`` — as a key-set (folds source deletes in, so a deleted
        spine key lands in the delete set automatically; see :meth:`KeySet.dropped`)."""
        if not self.pk:
            raise DeltaError("this delta has no declared primary key — .keys() needs one")
        pk_sel = ", ".join(_q(c) for c in self.pk)
        u = self.upserts.project(pk_sel)
        d = self.deletes.project(pk_sel)
        return KeySet(self.con, u.union(d).distinct(), self.pk)


class KeySet:
    """A relation of primary keys — the currency of the partial-merge (``comprehensive=False``) helpers.
    Operating on keys only keeps the bookkeeping framework-agnostic (no imposed compute layer)."""

    def __init__(self, con, relation, pk: tuple[str, ...]) -> None:
        self.con = con
        self.relation = relation
        self.pk = tuple(pk)

    def union(self, other: "KeySet") -> "KeySet":
        return KeySet(self.con, self.relation.union(other.relation).distinct(), self.pk)

    def create_view(self, name: str):
        self.relation.create_view(name, replace=True)
        return self

    def dropped(self, recomputed):
        """The keys that fell out of the recompute = ``self EXCEPT recomputed[pk]`` — the deletes for a
        partial merge. Correct however ``self`` was built: a source-deleted key is in ``self`` (``.keys()``
        folds deletes in) and never in ``recomputed``, so it lands here automatically."""
        pk_sel = ", ".join(_q(c) for c in self.pk)
        rec_pk = recomputed.project(pk_sel)
        return self.relation.project(pk_sel).except_(rec_pk)


def read_delta(con, data_dir: Path, table: str, previous_f, f, *, dp) -> Delta:
    """Resolve ``table``'s declared mode in ``data_dir`` and read its change-set over ``(previous_f, f]``.

    - **append** source: window-filter the single history table.
    - **merge** source: read the changelog window, collapse per PK to the max-``_duckstring_f`` row (the
      *net* change per key).
    - **overwrite** source (a plain Ripple): no history → a full read, every row an upsert.
    - **coverage / bootstrap**: ``previous_f = NEVER`` or earlier than the oldest retained stamp → a full
      read (of the main / the whole table); resume incrementally next run."""
    from .engine.core import NEVER

    meta = load_sidecar(data_dir).get(table, {})
    mode = meta.get("mode", "overwrite")
    pk = tuple(meta.get("pk", ()))

    if mode == "append":
        return _read_append_delta(con, data_dir, table, previous_f, f, pk, dp, NEVER)
    if mode == "merge":
        return _read_merge_delta(con, data_dir, table, previous_f, f, pk, dp, NEVER)
    # overwrite source (a plain Ripple): no history → always a full read, every row an upsert.
    upserts = _strip_system(con.sql(dp.read_select(data_dir, table)))
    return Delta(con, pk, upserts, _empty_pk(upserts, pk), is_full=True)


def _read_append_delta(con, data_dir, table, previous_f, f, pk, dp, NEVER) -> Delta:
    rel = con.sql(dp.read_select(data_dir, table))  # includes _duckstring_f
    oldest = con.sql(f"SELECT min({_q(F_COL)}) FROM ({dp.read_select(data_dir, table)})").fetchone()[0]
    full = previous_f == NEVER or (oldest is not None and previous_f < oldest)
    upper = f"{_q(F_COL)} <= {_ts(f)}"
    cond = upper if full else f"{_q(F_COL)} > {_ts(previous_f)} AND {upper}"
    upserts = _strip_system(rel.filter(cond))
    return Delta(con, pk, upserts, _empty_pk(upserts, pk), is_full=full)


def _read_merge_delta(con, data_dir, table, previous_f, f, pk, dp, NEVER) -> Delta:
    clog = changelog_name(table)
    try:
        clog_sql = dp.read_select(data_dir, clog)
    except FileNotFoundError:
        clog_sql = None
    oldest = None
    if clog_sql is not None:
        oldest = con.sql(f"SELECT min({_q(F_COL)}) FROM ({clog_sql})").fetchone()[0]
    full = clog_sql is None or previous_f == NEVER or (oldest is not None and previous_f < oldest)
    if full:
        main = _strip_system(con.sql(dp.read_select(data_dir, table)))
        return Delta(con, pk, main, _empty_pk(main, pk), is_full=True)
    # Window the changelog, then collapse per PK to the latest op (net change per key). Inlined as a
    # self-contained subquery (over immutable read_parquet) — NOT a named view: several read_delta calls
    # in one run would share a view name, and the lazy upserts/deletes relations would re-bind to whoever
    # wrote it last (e.g. a spine delta silently re-pointing at a dimension's changelog).
    window = (
        f"(SELECT * FROM ({clog_sql}) "
        f"WHERE {_q(F_COL)} > {_ts(previous_f)} AND {_q(F_COL)} <= {_ts(f)} "
        f"QUALIFY row_number() OVER (PARTITION BY {', '.join(_q(c) for c in pk)} "
        f"ORDER BY {_q(F_COL)} DESC) = 1)"
    )
    upserts = _strip_system(con.sql(f"SELECT * FROM {window} WHERE {_q(OP_COL)} = '{OP_UPSERT}'"))
    pk_sel = ", ".join(_q(c) for c in pk)
    deletes = con.sql(f"SELECT {pk_sel} FROM {window} WHERE {_q(OP_COL)} = '{OP_DELETE}'")
    return Delta(con, pk, upserts, deletes)


def _strip_system(rel):
    """Project out any ``_duckstring_*`` system columns — a clean user-column view of the rows."""
    from .dataplane import RESERVED_PREFIX

    sys_cols = [c for c in rel.columns if c.startswith(RESERVED_PREFIX)]
    if not sys_cols:
        return rel
    return rel.project(f"* EXCLUDE ({', '.join(_q(c) for c in sys_cols)})")


def _empty_pk(rel, pk: tuple[str, ...]):
    """An empty deletes relation with the PK schema (for sources that never delete: append / full reads)."""
    if not pk:
        return rel.filter("1=0")
    return rel.project(", ".join(_q(c) for c in pk)).filter("1=0")
