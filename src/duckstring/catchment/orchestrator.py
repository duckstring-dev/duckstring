from __future__ import annotations

import asyncio
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _log(event: str, name: str, gen: int | None = None, duration: float | None = None) -> None:
    gen_col = f"gen={gen}" if gen is not None else ""
    dur_col = f"{duration:.2f}s" if duration is not None else ""
    print(f"[{_ts()}] {event:<8} {gen_col:<8} {dur_col:<7} {name}", flush=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def notify(app) -> None:
    app.state.sentinel_queue.put_nowait(None)


async def sentinel_loop(queue, db_path, root, executor):
    try:
        while True:
            await queue.get()
            while not queue.empty():
                queue.get_nowait()

            db = _connect(db_path)
            try:
                changed = True
                while changed:
                    changed = False

                    if _propagate_stops(db):
                        db.commit()
                        changed = True

                    if _activate_stopped_ponds(db):
                        db.commit()
                        changed = True

                    if _process_pending_stops(db):
                        db.commit()
                        changed = True

                    if _propagate_pipeline_demand(db):
                        db.commit()
                        changed = True

                    for pond_info in _find_startable_ponds(db):
                        _create_pond_run(db, pond_info)
                        if not pond_info.is_stop_run:
                            _write_pipeline_demand(db, pond_info)
                        db.commit()
                        _log("queued", f"{pond_info.pond_name} v{pond_info.version}", gen=pond_info.next_gen)
                        asyncio.ensure_future(_dispatch(pond_info, db_path, root, executor, queue))
                        changed = True
            finally:
                db.close()
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class _PondInfo:
    pond_version_id: int
    pond_id: int
    pond_major: int
    pond_name: str
    version: str
    source_path: str
    next_gen: int
    is_stop_run: bool = False
    run_id: str = field(default="")


# ---------------------------------------------------------------------------
# Sentinel helpers
# ---------------------------------------------------------------------------

def _connect(db_path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), check_same_thread=False)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def _find_startable_ponds(db: sqlite3.Connection) -> list[_PondInfo]:
    rows = db.execute("""
        SELECT DISTINCT pv.id, p.id, pv.major, p.name, pv.version, pv.source_path
        FROM demand d
        JOIN pond_version pv ON pv.id = d.pond_version_id
        JOIN pond p ON p.id = pv.pond_id
        WHERE pv.is_active = 1
          AND pv.is_stopped = 0
          AND NOT EXISTS (
              SELECT 1 FROM pond_run pr
              WHERE pr.pond_version_id = pv.id AND pr.status = 'running'
          )
    """).fetchall()

    result = []
    for pv_id, pond_id, major, name, version, source_path in rows:
        if not _inter_pond_ready(db, pv_id, pond_id):
            continue
        next_gen = db.execute("""
            SELECT COALESCE(MAX(pr.generation), 0) + 1
            FROM pond_run pr
            JOIN pond_version pv2 ON pv2.id = pr.pond_version_id
            WHERE pv2.pond_id = ? AND pv2.major = ?
        """, (pond_id, major)).fetchone()[0]
        result.append(_PondInfo(
            pond_version_id=pv_id, pond_id=pond_id, pond_major=major,
            pond_name=name, version=version, source_path=source_path,
            next_gen=next_gen, is_stop_run=_acknowledges_stop(db, pv_id),
        ))
    return result


def _inter_pond_ready(db: sqlite3.Connection, pv_id: int, pond_id: int) -> bool:
    sources = db.execute("""
        SELECT source_pond_id, source_major, required FROM pond_to_pond
        WHERE pond_version_id = ?
    """, (pv_id,)).fetchall()
    if not sources:
        return True

    required = [(s, m) for s, m, r in sources if r]
    optional = [(s, m) for s, m, r in sources if not r]

    def _latest(s, m):
        return db.execute("""
            SELECT COALESCE(MAX(pr.generation), 0)
            FROM pond_run pr JOIN pond_version pv ON pv.id = pr.pond_version_id
            WHERE pv.pond_id = ? AND pv.major = ? AND pr.status = 'success'
        """, (s, m)).fetchone()[0]

    def _wm(s, m):
        r = db.execute("""
            SELECT generation FROM watermark
            WHERE sink_pond_id = ? AND source_pond_id = ? AND source_major = ?
        """, (pond_id, s, m)).fetchone()
        rr = db.execute("""
            SELECT generation FROM retry_watermark
            WHERE sink_pond_id = ? AND source_pond_id = ? AND source_major = ?
        """, (pond_id, s, m)).fetchone()
        return max(r[0] if r else 0, rr[0] if rr else 0)

    if required:
        return all(_latest(s, m) > _wm(s, m) for s, m in required)
    return any(_latest(s, m) > _wm(s, m) for s, m in optional)


def _create_pond_run(db: sqlite3.Connection, pond_info: _PondInfo) -> None:
    run_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO pond_run (id, pond_version_id, generation, status) VALUES (?, ?, ?, 'running')",
        (run_id, pond_info.pond_version_id, pond_info.next_gen),
    )
    pond_info.run_id = run_id


def _write_pipeline_demand(db: sqlite3.Connection, pond_info: _PondInfo) -> None:
    sources = db.execute("""
        SELECT pv.id, p.name, pv.version FROM pond_to_pond p2p
        JOIN pond_version pv ON pv.pond_id = p2p.source_pond_id
            AND pv.major = p2p.source_major AND pv.is_active = 1
        JOIN pond p ON p.id = pv.pond_id
        WHERE p2p.pond_version_id = ?
    """, (pond_info.pond_version_id,)).fetchall()
    for src_pv_id, src_name, src_ver in sources:
        rows = db.execute("""
            INSERT INTO demand (pond_version_id, sink_id)
            SELECT ?, ?
            WHERE NOT EXISTS (SELECT 1 FROM demand WHERE pond_version_id = ? AND sink_id = ?)
        """, (src_pv_id, pond_info.pond_version_id, src_pv_id, pond_info.pond_version_id)).rowcount
        if rows:
            _log("demand", f"{src_name} v{src_ver}")


def _acknowledges_stop(db: sqlite3.Connection, pv_id: int) -> bool:
    """True if the pond has demand and every demand row has a matching stop row."""
    has_demand = db.execute(
        "SELECT 1 FROM demand WHERE pond_version_id = ?", (pv_id,)
    ).fetchone()
    if not has_demand:
        return False
    unmatched = db.execute("""
        SELECT 1 FROM demand d WHERE d.pond_version_id = ?
        AND NOT EXISTS (
            SELECT 1 FROM stop s
            WHERE s.pond_version_id = d.pond_version_id AND s.sink_id IS d.sink_id
        )
    """, (pv_id,)).fetchone()
    return unmatched is None


def _all_sinks_acknowledge_stop(db: sqlite3.Connection, src_pond_id: int) -> bool:
    """True if every active sink of src_pond_id acknowledges stop."""
    sink_pv_ids = [row[0] for row in db.execute("""
        SELECT pv.id FROM pond_to_pond p2p
        JOIN pond_version pv ON pv.id = p2p.pond_version_id AND pv.is_active = 1
        WHERE p2p.source_pond_id = ?
    """, (src_pond_id,)).fetchall()]
    if not sink_pv_ids:
        return False
    return all(_acknowledges_stop(db, sid) for sid in sink_pv_ids)


def _propagate_stops(db: sqlite3.Connection) -> bool:
    """Immediately propagate stop records upstream from any pond that acknowledges stop.

    A stop is forwarded to a source only when ALL active sinks of that source also
    acknowledge stop (unanimous-sinks rule). Runs before demand propagation so the
    stop signal travels the chain independently.
    """
    acknowledging = db.execute("""
        SELECT DISTINCT pv.id, p.id FROM demand d
        JOIN pond_version pv ON pv.id = d.pond_version_id
        JOIN pond p ON p.id = pv.pond_id
        WHERE pv.is_active = 1
    """).fetchall()
    changed = False
    for pv_id, _ in acknowledging:
        if not _acknowledges_stop(db, pv_id):
            continue
        sources = db.execute("""
            SELECT pv2.id, p2.name, pv2.version, p2.id AS src_pond_id
            FROM pond_to_pond p2p
            JOIN pond_version pv2 ON pv2.pond_id = p2p.source_pond_id
                AND pv2.major = p2p.source_major AND pv2.is_active = 1 AND pv2.is_stopped = 0
            JOIN pond p2 ON p2.id = pv2.pond_id
            WHERE p2p.pond_version_id = ?
        """, (pv_id,)).fetchall()
        for src_pv_id, src_name, src_ver, src_pond_id in sources:
            if not _all_sinks_acknowledge_stop(db, src_pond_id):
                continue
            rows = db.execute("""
                INSERT INTO stop (pond_version_id, sink_id)
                SELECT ?, ?
                WHERE NOT EXISTS (SELECT 1 FROM stop WHERE pond_version_id = ? AND sink_id IS ?)
            """, (src_pv_id, pv_id, src_pv_id, pv_id)).rowcount
            if rows:
                _log("stop", f"{src_name} v{src_ver}")
                changed = True
    return changed


def _activate_stopped_ponds(db: sqlite3.Connection) -> bool:
    """Propagate demand upstream for stopped ponds that have received demand, then unstop them.

    Stop propagation is handled entirely by _propagate_stops; this function only
    propagates demand so sources have work to do.
    """
    rows = db.execute("""
        SELECT DISTINCT pv.id FROM demand d
        JOIN pond_version pv ON pv.id = d.pond_version_id
        WHERE pv.is_active = 1 AND pv.is_stopped = 1
    """).fetchall()
    changed = False
    for (pv_id,) in rows:
        sources = db.execute("""
            SELECT pv2.id, p2.name, pv2.version FROM pond_to_pond p2p
            JOIN pond_version pv2 ON pv2.pond_id = p2p.source_pond_id
                AND pv2.major = p2p.source_major AND pv2.is_active = 1
            JOIN pond p2 ON p2.id = pv2.pond_id
            WHERE p2p.pond_version_id = ?
        """, (pv_id,)).fetchall()
        for src_pv_id, src_name, src_ver in sources:
            inserted = db.execute("""
                INSERT INTO demand (pond_version_id, sink_id)
                SELECT ?, ?
                WHERE NOT EXISTS (SELECT 1 FROM demand WHERE pond_version_id = ? AND sink_id = ?)
            """, (src_pv_id, pv_id, src_pv_id, pv_id)).rowcount
            if inserted:
                _log("demand", f"{src_name} v{src_ver}")
                changed = True
        db.execute("UPDATE pond_version SET is_stopped = 0 WHERE id = ?", (pv_id,))
        changed = True
    return changed


def _process_pending_stops(db: sqlite3.Connection) -> bool:
    """Mark idle ponds stopped immediately when they hold a stop-only record (no demand)."""
    rows = db.execute("""
        SELECT DISTINCT pv.id FROM stop s
        JOIN pond_version pv ON pv.id = s.pond_version_id
        WHERE pv.is_active = 1 AND pv.is_stopped = 0
          AND NOT EXISTS (SELECT 1 FROM demand d WHERE d.pond_version_id = pv.id)
          AND NOT EXISTS (
              SELECT 1 FROM pond_run pr
              WHERE pr.pond_version_id = pv.id AND pr.status = 'running'
          )
    """).fetchall()
    changed = False
    for (pv_id,) in rows:
        db.execute("UPDATE pond_version SET is_stopped = 1 WHERE id = ?", (pv_id,))
        db.execute("DELETE FROM stop WHERE pond_version_id = ?", (pv_id,))
        changed = True
    return changed


def _propagate_pipeline_demand(db: sqlite3.Connection) -> bool:
    """Write pipeline demand upstream for active ponds that are blocked on inter-pond readiness.

    Enables continuous wave operation: when a pond has demand but sources haven't yet produced
    a new generation, signal those sources so they start the next generation. No-op for ponds
    that are already startable (handled by _find_startable_ponds) or stopped (is_stopped=1).
    """
    rows = db.execute("""
        SELECT DISTINCT pv.id, pv.pond_id FROM demand d
        JOIN pond_version pv ON pv.id = d.pond_version_id
        WHERE pv.is_active = 1 AND pv.is_stopped = 0
          AND NOT EXISTS (
              SELECT 1 FROM pond_run pr
              WHERE pr.pond_version_id = pv.id AND pr.status = 'running'
          )
    """).fetchall()
    changed = False
    for pv_id, pond_id in rows:
        if _inter_pond_ready(db, pv_id, pond_id):
            continue
        sources = db.execute("""
            SELECT pv2.id, p2.name, pv2.version FROM pond_to_pond p2p
            JOIN pond_version pv2 ON pv2.pond_id = p2p.source_pond_id
                AND pv2.major = p2p.source_major AND pv2.is_active = 1 AND pv2.is_stopped = 0
            JOIN pond p2 ON p2.id = pv2.pond_id
            WHERE p2p.pond_version_id = ?
        """, (pv_id,)).fetchall()
        for src_pv_id, src_name, src_ver in sources:
            inserted = db.execute("""
                INSERT INTO demand (pond_version_id, sink_id)
                SELECT ?, ?
                WHERE NOT EXISTS (SELECT 1 FROM demand WHERE pond_version_id = ? AND sink_id = ?)
            """, (src_pv_id, pv_id, src_pv_id, pv_id)).rowcount
            if inserted:
                _log("demand", f"{src_name} v{src_ver}")
                changed = True
    return changed


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def tide_loop(queue, db_path, sentinel_queue) -> None:
    from datetime import datetime, timezone

    from croniter import croniter

    def _now(local: bool) -> datetime:
        return datetime.now() if local else datetime.now(timezone.utc).replace(tzinfo=None)

    def _next_fire(schedule: str, local: bool) -> datetime:
        return croniter(schedule, _now(local)).get_next(datetime)

    def _secs_until(fire: datetime, local: bool) -> float:
        return max(0.0, (fire - _now(local)).total_seconds())

    try:
        while True:
            while not queue.empty():
                queue.get_nowait()

            db = _connect(db_path)
            try:
                tides = db.execute("""
                    SELECT pt.pond_id, p.name, pt.major, pt.schedule, pt.local
                    FROM pond_trigger pt JOIN pond p ON p.id = pt.pond_id
                    WHERE pt.status = 'active'
                """).fetchall()
            finally:
                db.close()

            if not tides:
                await queue.get()
                continue

            tides_with_fire = [(t, _next_fire(t[3], bool(t[4]))) for t in tides]
            wait = min(_secs_until(nf, bool(t[4])) for t, nf in tides_with_fire)

            try:
                await asyncio.wait_for(queue.get(), timeout=wait)
                continue  # schedule changed — recalculate
            except asyncio.TimeoutError:
                pass

            db = _connect(db_path)
            try:
                fired = False
                for (pond_id, name, major, _schedule, local), fire_time in tides_with_fire:
                    if _secs_until(fire_time, bool(local)) > 1.0:
                        continue
                    row = db.execute("""
                        SELECT pv.id, pv.version FROM pond_version pv
                        WHERE pv.pond_id = ? AND pv.major = ? AND pv.is_active = 1
                          AND pv.is_stopped = 1
                    """, (pond_id, major)).fetchone()
                    if not row:
                        _log("skip", f"{name} (not stopped)")
                        continue
                    pv_id, ver = row
                    db.execute(
                        "INSERT INTO demand (pond_version_id, sink_id) SELECT ?, NULL "
                        "WHERE NOT EXISTS (SELECT 1 FROM demand WHERE pond_version_id = ? AND sink_id IS NULL)",
                        (pv_id, pv_id),
                    )
                    db.execute(
                        "INSERT INTO stop (pond_version_id, sink_id) SELECT ?, NULL "
                        "WHERE NOT EXISTS (SELECT 1 FROM stop WHERE pond_version_id = ? AND sink_id IS NULL)",
                        (pv_id, pv_id),
                    )
                    _log("tide", f"{name} v{ver}")
                    fired = True
                db.commit()
            finally:
                db.close()

            if fired:
                sentinel_queue.put_nowait(None)
    except asyncio.CancelledError:
        pass


async def _dispatch(pond_info: _PondInfo, db_path, root, executor, queue) -> None:
    from .pond_worker import execute_pond_run

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            executor,
            execute_pond_run,
            pond_info.run_id,
            pond_info.pond_version_id,
            pond_info.pond_name,
            pond_info.version,
            pond_info.source_path,
            str(db_path),
            str(root),
            pond_info.next_gen,
        )
    except (asyncio.CancelledError, Exception):
        pass
    finally:
        try:
            queue.put_nowait(None)
        except Exception:
            pass
