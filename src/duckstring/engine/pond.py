"""The Pond run ledger: a tiny SQLite file in the Pond's storage dir (``ponds/{base_pond}/pond.db``).

It is the **durable, authoritative record of intra-Pond execution** — per-Ripple ``start_f``/``end_f``
and the Pond Run history. The Duck is the single writer; the Catchment may read it as a fallback /
reconciliation path if live events are lost. Everything is keyed on freshness ``F`` so reads and
re-runs are idempotent.

``end_f`` is authoritative: on Duck restart, the ledger seeds the :class:`WorkerState`, and
``begin_run(F)`` only stamps Ripples whose ``end_f < F`` — so **only incomplete Ripples re-run**, never
ones already finished for that Pond Run.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from .core import NEVER, RippleState
from .worker import WorkerState, new_state

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ripple_run_state (
    ripple_name TEXT PRIMARY KEY,
    start_f     TEXT NOT NULL,
    end_f       TEXT NOT NULL,
    is_running  INTEGER NOT NULL DEFAULT 0,
    is_failed   INTEGER NOT NULL DEFAULT 0  -- last attempt errored (cleared when it next starts)
);
CREATE TABLE IF NOT EXISTS pond_run (
    f           TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running'
);
"""


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


def connect(path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), check_same_thread=False)
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(_SCHEMA)
    con.commit()
    return con


def record_ripple_start(con: sqlite3.Connection, name: str, start_f: datetime) -> None:
    con.execute(
        "INSERT INTO ripple_run_state (ripple_name, start_f, end_f, is_running, is_failed) VALUES (?, ?, ?, 1, 0) "
        "ON CONFLICT(ripple_name) DO UPDATE SET start_f = excluded.start_f, is_running = 1, is_failed = 0",
        (name, _iso(start_f), _iso(NEVER)),
    )
    con.commit()


def record_ripple_failed(con: sqlite3.Connection, name: str) -> None:
    """Mark a Ripple's last attempt as errored (visibility for the immediate-retry path)."""
    con.execute(
        "UPDATE ripple_run_state SET is_running = 0, is_failed = 1 WHERE ripple_name = ?", (name,)
    )
    con.commit()


def record_ripple_complete(con: sqlite3.Connection, name: str, end_f: datetime) -> None:
    con.execute(
        "UPDATE ripple_run_state SET end_f = ?, is_running = 0 WHERE ripple_name = ?",
        (_iso(end_f), name),
    )
    con.commit()


def record_pond_run_start(con: sqlite3.Connection, f: datetime, now: datetime) -> None:
    con.execute(
        "INSERT OR IGNORE INTO pond_run (f, started_at, status) VALUES (?, ?, 'running')",
        (_iso(f), _iso(now)),
    )
    con.commit()


def record_pond_run_finish(con: sqlite3.Connection, f: datetime, now: datetime, status: str = "success") -> None:
    con.execute(
        "UPDATE pond_run SET finished_at = ?, status = ? WHERE f = ?",
        (_iso(now), status, _iso(f)),
    )
    con.commit()


def load_state(con: sqlite3.Connection, parents: dict[str, list[str]], optional=None) -> WorkerState:
    """Build a :class:`WorkerState` for the given topology, seeding per-Ripple freshness from the
    ledger. Ripples absent from the ledger start at NEVER. ``last_completed_f`` = newest succeeded
    Pond Run, so already-reported completions are not re-emitted."""
    s = new_state(parents, optional)
    rows = con.execute("SELECT ripple_name, start_f, end_f, is_running FROM ripple_run_state").fetchall()
    for name, start_f, end_f, _is_running in rows:
        if name in s.states:
            # On reload nothing is actually running — an interrupted Ripple (is_running in the ledger,
            # end_f < its start_f) simply re-runs when re-stamped, so reset is_running to False.
            s.states[name] = RippleState(start_f=_parse(start_f), end_f=_parse(end_f), is_running=False)
    s.last_completed_f = read_pond_end_f(con) or NEVER
    return s


def read_pond_end_f(con: sqlite3.Connection) -> datetime | None:
    """The freshness of the newest successfully completed Pond Run (Catchment fallback)."""
    row = con.execute("SELECT MAX(f) FROM pond_run WHERE status = 'success'").fetchone()
    return _parse(row[0]) if row and row[0] else None


def incomplete_ripples(con: sqlite3.Connection, f: datetime, names: list[str]) -> list[str]:
    """Of ``names``, those not yet complete for an outstanding Pond Run ``F`` (``end_f < F``, treating a
    Ripple with no ledger row as never-run) — what to re-run on recovery. (``begin_run`` already
    enforces this via its ``f > end_f`` guard; exposed for tests.)"""
    done = {name: _parse(end_f) for name, end_f in con.execute("SELECT ripple_name, end_f FROM ripple_run_state")}
    return [name for name in names if done.get(name, NEVER) < f]
