"""The host seam for the Trickle incremental engine.

Trickle is a self-contained DBSP-style incremental engine over DuckDB (Z-sets, incremental joins and
aggregation — see ``plans/trickle-dbsp.md`` / ``plans/trickle-dag.md``). It is deliberately decoupled from
the rest of Duckstring: it depends on **nothing** in the wider package, only on this module's small
:class:`Context` protocol and two owned constants. That keeps it ready to lift out into its own distribution
at any time (see the Duckstring ``CLAUDE.md`` note). Any host that can supply a DuckDB connection, a stable
**epoch**, and a way to read a source's current state and windowed delta can drive it — the Pond/Ripple
runtime is one such host, but nothing here assumes it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

# The reserved system-column / system-table namespace Trickle owns (``_duckstring_f``, ``_duckstring_d``,
# the changelog/agg-state companions, …). A host that also reserves a namespace must use the same string;
# Duckstring's data plane re-exports this as ``RESERVED_PREFIX`` so there is a single source of truth.
SYSTEM_PREFIX = "_duckstring_"

# The bottom of the epoch order — the sentinel ``previous_f`` for "never read before" (a bootstrap). Any
# epoch a host stamps must compare greater than this. Compared by value (``==`` / ``>=``), never identity,
# so a host's own equal-valued sentinel interoperates.
NEVER = datetime.min.replace(tzinfo=timezone.utc)


@runtime_checkable
class Context(Protocol):
    """What the builder needs from its host to maintain a view over a run at a single **epoch**.

    - ``con`` — a DuckDB connection holding the working registry (where sources are read and outputs written).
    - ``f`` — this run's epoch: the stable, monotonic stamp written on every history/changelog row and used
      as the upper bound of the read window. Replay-stable (a re-run at the same ``f`` is idempotent).
    - ``previous_f`` — the epoch of the consumer's previous read; the window is ``(previous_f, f]``.
      :data:`NEVER` on a first read (bootstrap).
    - ``read_table(ref)`` — the **current clean state** of a source ``ref`` as a DuckDB relation
      (system columns stripped).
    - ``read_delta(ref)`` — the source's **Z-set change** over ``(previous_f, f]`` as a
      :class:`~duckstring.trickle.io.Delta`.

    A host **may** also offer ``count_table(ref) -> int`` — a metadata-fast current-row count of a source
    (no scan). It is optional: the builder's ``.count()`` uses it when present and otherwise falls back to
    ``count(*)`` over :meth:`read_table`.
    """

    con: object
    f: datetime
    previous_f: datetime

    def read_table(self, ref: str): ...

    def read_delta(self, ref: str): ...
