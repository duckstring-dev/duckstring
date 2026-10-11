"""Shared helpers for the engine tests: epochs, reading a table's current state, and the demo-shaped
sources (order lines and a product catalog) most builder tests start from. Sources and outputs live in one
database, and each run is a :func:`trickle.at` window."""

from __future__ import annotations

from datetime import datetime, timezone

import trickle
from trickle import NEVER
from trickle import io as T
from trickle.session import Epoch

UTC = timezone.utc

__all__ = ["NEVER", "T", "UTC", "at", "merge", "rows", "spy_paths", "star", "ts", "values"]


def ts(hour: int) -> datetime:
    return datetime(2026, 6, 16, hour, tzinfo=UTC)


def at(con, f, previous_f=NEVER):
    """The host for one run: the window ``(previous_f, f]`` over ``con``."""
    return trickle.at(con, f, previous_f)


def rows(con, table: str, proj: str = "*") -> list:
    """``proj`` over ``table``'s current state (system columns stripped), sorted."""
    return sorted(T.current_state(con, table).query("t", f"SELECT {proj} FROM t").fetchall())


def values(rows_: list, cols: str, types: str | None = None) -> str:
    """A ``SELECT`` over literal rows with columns ``cols``; an empty list gives an empty relation, which
    needs the column ``types`` (``"INTEGER, VARCHAR"``)."""
    names = [c.strip() for c in cols.split(",")]
    if not rows_:
        casts = ", ".join(f"NULL::{t.strip()} AS {n}" for n, t in zip(names, (types or "").split(","), strict=True))
        return f"SELECT {casts} WHERE 1=0"

    def lit(v):
        return "NULL" if v is None else repr(v) if isinstance(v, str) else str(v)

    body = ", ".join("(" + ", ".join(lit(v) for v in r) + ")" for r in rows_)
    return f"SELECT * FROM (VALUES {body}) v({cols})"


def merge(con, table: str, rows_: list, cols: str, f, pk, *, types: str | None = None, **kw) -> bool:
    """Write the complete state ``rows_`` to the merge Trickle ``table`` at ``f``: a Source changing."""
    return T.merge_table(con, table, con.sql(values(rows_, cols, types)), f, T.normalize_pk(pk), **kw)


def star(con):
    """The order-line spine and the product dimension most builder tests join: returns ``(ol, pr)``, each
    taking a complete state and an epoch."""

    def ol(state, f, **kw):
        merge(con, "order_line", state, "order_id, product_id, qty", f, "order_id", **kw)

    def pr(state, f):
        merge(con, "product", state, "product_id, price", f, "product_id")

    return ol, pr


def spy_paths(monkeypatch) -> list[str]:
    """Record which write each builder terminal used: 'comprehensive' (``merge_table``, the whole output
    recomputed and diffed) or 'incremental' (``apply_zset``, the change composed from deltas)."""
    paths: list[str] = []
    orig_m, orig_z = Epoch.merge_table, Epoch.apply_zset

    def spy_m(self, *a, **k):
        paths.append("comprehensive")
        return orig_m(self, *a, **k)

    def spy_z(self, *a, **k):
        paths.append("incremental")
        return orig_z(self, *a, **k)

    monkeypatch.setattr(Epoch, "merge_table", spy_m)
    monkeypatch.setattr(Epoch, "apply_zset", spy_z)
    return paths
