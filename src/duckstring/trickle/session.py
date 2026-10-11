"""The standalone host: keep tables in one DuckDB database up to date incrementally, with no Duckstring.

A Duckstring ``Pond`` is one host for the Trickle engine; this is the other, for a user who has a DuckDB
database and wants derived tables in it maintained from the changes to their inputs::

    import duckdb
    import trickle
    from trickle import agg

    con = duckdb.connect("shop.duckdb")
    with trickle.run(con, pipeline="nightly") as t:
        t.merge_table("orders", con.sql("FROM read_parquet('drops/orders/*.parquet')"), pk="order_id")
        t.merge_table("products", con.sql("FROM read_csv('drops/products.csv')"), pk="product_id")
        (t.source("orders")
           .join(t.source("products"), on="product_id")
           .select("s1.category, s0.quantity * s1.unit_price AS revenue")
           .aggregate(by="category", revenue=agg.sum("revenue"), orders=agg.count())
           .merge("revenue_by_category"))

Each run of a named **pipeline** gets an epoch ``f``, stamped on everything it writes, and reads its sources'
changes since the pipeline's last committed run (``previous_f``). The run state lives in the database itself
(:data:`RUN_TABLE`), so several pipelines can share one database by name.

- **Epochs** are monotonic: ``f`` is the current time, or just past every epoch the database has recorded if
  the clock is behind it.
- **A failed run is replayed.** A run that raises leaves its epoch pending, and the pipeline's next run reuses
  it, so it replays at the same ``f``. The engine's writes are idempotent at an epoch, so the replay replaces
  what the failed run wrote. The epoch is committed only when the run finishes cleanly.
- **Sources** are read from the same database. A table written with ``append_table``/``merge_table`` (or by a
  builder terminal) gives its changes over ``(previous_f, f]``; any other table is read in full each run,
  which recomputes whatever depends on it. To make a snapshot source incremental, write it with
  ``merge_table``, which records only what changed.

Runs of pipelines that share tables should not overlap: an epoch is taken when a run starts, so rows a
concurrent run writes at an earlier epoch can land after a later run has read past it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import io
from .builder import TrickleBuilder
from .context import NEVER

# The run state: per pipeline, the last committed epoch and the epoch of a run that didn't finish.
RUN_TABLE = "_duckstring_trickle_run"

_TICK = timedelta(microseconds=1)  # DuckDB's TIMESTAMPTZ resolution


def _utc(value):
    return value.astimezone(timezone.utc) if value is not None else None


class Run:
    """One run of a pipeline: the host the builder and the write methods work against. Use it as a context
    manager (``with trickle.run(con) as t``), which commits the epoch when the block finishes and leaves it
    pending, to be replayed, when the block raises.

    Attributes:
        con: The DuckDB connection.
        pipeline: The pipeline's name.
        f: This run's epoch (timezone-aware UTC), stamped on every row it writes.
        previous_f: The epoch of the pipeline's last committed run, or :data:`~trickle.context.NEVER` on its
            first. Sources are read over ``(previous_f, f]``.
        replay: Whether this run reuses the epoch of a run that didn't finish.
    """

    def __init__(self, con, pipeline: str = "default", *, now=None) -> None:
        self.con = con
        self.pipeline = pipeline
        self.f, self.previous_f, self.replay = _begin(con, pipeline, now)
        self.committed = False

    # ─── run lifecycle ─────────────────────────────────────────────────────────

    def __enter__(self) -> Run:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.commit()
        return False  # a failed run's epoch stays pending, for the next run to replay

    def commit(self) -> None:
        """Record this run's epoch as the pipeline's last, so its next run reads from here."""
        if self.committed:
            return
        self.con.execute(f"UPDATE {io._q(RUN_TABLE)} SET last_f = ?, pending_f = NULL WHERE pipeline = ?",
                         [self.f, self.pipeline])
        self.committed = True

    # ─── reading ───────────────────────────────────────────────────────────────

    def source(self, ref: str, *, p: float = 0.3) -> TrickleBuilder:
        """Start a builder on the table ``ref``. ``p`` is the change-fraction threshold: if this source's
        changes touch more than ``p`` of its rows, the run recomputes in full. ``1.0`` disables the check."""
        return TrickleBuilder(self, ref, p=p)

    def read_table(self, ref: str):
        """The current state of ``ref``, without system columns."""
        return io.current_state(self.con, ref)

    def read_delta(self, ref: str) -> io.Delta:
        """``ref``'s changes over ``(previous_f, f]``. A table that isn't a Trickle reads in full
        (``is_full``)."""
        if ref in io.read_meta(self.con):
            return io.read_registry_delta(self.con, ref, self.previous_f, self.f)
        state = io._strip_system(self.con.sql(f"SELECT * FROM {io._q(ref)}"))
        return io.Delta(self.con, (), io._as_zset(state, 1), is_full=True)

    def count_table(self, ref: str) -> int:
        """The current number of rows in ``ref``, without scanning a merge table's base."""
        return io.count_current(self.con, ref)

    # ─── writing ───────────────────────────────────────────────────────────────

    def append_table(self, name: str, relation, *, pk=None, fail_on_conflict=True, retain_t=None,
                     retain_n=None) -> bool:
        """Append ``relation`` to the append Trickle ``name`` at this run's epoch. See
        :func:`trickle.io.append_table`. Returns whether rows were appended."""
        return io.append_table(self.con, name, relation, self.f, io.normalize_pk(pk),
                               fail_on_conflict=fail_on_conflict, retain_t=retain_t, retain_n=retain_n)

    def merge_table(self, name: str, relation, *, pk, retain_t=None, retain_n=None, **kw) -> bool:
        """Merge the complete current state ``relation`` into the merge Trickle ``name``, recording only what
        changed. See :func:`trickle.io.merge_table`. Returns whether anything changed."""
        return io.merge_table(self.con, name, relation, self.f, io.normalize_pk(pk),
                              retain_t=retain_t, retain_n=retain_n, **kw)

    def apply_zset(self, name: str, zset, *, pk, retain_t=None, retain_n=None, **kw) -> bool:
        """Append an already-computed change to the merge Trickle ``name``. See :func:`trickle.io.apply_zset`."""
        return io.apply_zset(self.con, name, zset, self.f, io.normalize_pk(pk),
                             retain_t=retain_t, retain_n=retain_n, **kw)


def run(con, pipeline: str = "default", *, now=None) -> Run:
    """Start a run of ``pipeline`` against the database ``con``. Use as ``with trickle.run(con) as t:``.
    ``now`` overrides the clock (tests); an epoch is never earlier than one the database has recorded."""
    return Run(con, pipeline, now=now)


def _begin(con, pipeline: str, now):
    """Take this run's epoch: the pending one if the pipeline's last run didn't finish, else a new one past
    every epoch recorded in the database. Returns ``(f, previous_f, replay)``."""
    q = io._q(RUN_TABLE)
    con.execute(f"CREATE TABLE IF NOT EXISTS {q} (pipeline VARCHAR PRIMARY KEY, last_f TIMESTAMPTZ, "
                f"pending_f TIMESTAMPTZ)")
    con.execute("BEGIN TRANSACTION")
    try:
        row = con.execute(f"SELECT last_f, pending_f FROM {q} WHERE pipeline = ?", [pipeline]).fetchone()
        last, pending = (_utc(row[0]), _utc(row[1])) if row else (None, None)
        previous = last if last is not None else NEVER
        if pending is not None:
            con.execute("COMMIT")
            return pending, previous, True
        (latest,) = con.execute(f"SELECT max(e) FROM (SELECT last_f AS e FROM {q} UNION ALL "
                                f"SELECT pending_f FROM {q})").fetchone()
        f = _utc(now) if now is not None else datetime.now(timezone.utc)
        if latest is not None and f <= _utc(latest):
            f = _utc(latest) + _TICK
        con.execute(f"INSERT INTO {q} (pipeline, last_f, pending_f) VALUES (?, ?, ?) "
                    f"ON CONFLICT (pipeline) DO UPDATE SET pending_f = excluded.pending_f",
                    [pipeline, last, f])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return f, previous, False
