from __future__ import annotations

import asyncio
import importlib
import sqlite3
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

_import_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def notify(app) -> None:
    app.state.sentinel_queue.put_nowait(None)


async def sentinel_loop(queue, db_path, registry_path, root, executor):
    running_ripples: set[int] = set()

    while True:
        await queue.get()
        # Drain any additional notifications that piled up.
        while not queue.empty():
            queue.get_nowait()

        db = _connect(db_path)
        try:
            changed = True
            while changed:
                changed = False

                for pond_info in _find_startable_ponds(db, running_ripples):
                    _create_pond_run(db, pond_info)
                    _write_pipeline_demand(db, pond_info)
                    db.commit()
                    changed = True

                for task in _find_executable_ripples(db, running_ripples):
                    _create_ripple_run(db, task.ripple_id, task.pond_run_id)
                    running_ripples.add(task.ripple_id)
                    db.commit()
                    asyncio.ensure_future(
                        _dispatch(task, db_path, registry_path, root, executor, queue, running_ripples)
                    )
                    changed = True

                if _propagate_blocked(db):
                    db.commit()
                    changed = True
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Dataclasses
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


@dataclass
class _RippleTask:
    ripple_id: int
    pond_run_id: str
    ripple_name: str
    source_path: str
    pond_name: str
    version: str


# ---------------------------------------------------------------------------
# Sentinel helpers
# ---------------------------------------------------------------------------

def _connect(db_path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), check_same_thread=False)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def _find_startable_ponds(db: sqlite3.Connection, running_ripples: set[int]) -> list[_PondInfo]:
    rows = db.execute("""
        SELECT DISTINCT
            pv.id, p.id, pv.major, p.name, pv.version, pv.source_path
        FROM demand d
        JOIN pond_version pv ON pv.id = d.pond_version_id
        JOIN pond p ON p.id = pv.pond_id
        WHERE pv.is_active = 1
          AND NOT EXISTS (
              SELECT 1 FROM pond_run pr
              WHERE pr.pond_version_id = pv.id AND pr.status = 'running'
          )
    """).fetchall()

    result = []
    for pv_id, pond_id, major, name, version, source_path in rows:
        if not _inter_pond_ready(db, pv_id, pond_id):
            continue

        # Check that at least one ripple in this pond is not currently running.
        ripple_ids = [r[0] for r in db.execute(
            "SELECT id FROM ripple WHERE pond_version_id = ?", (pv_id,)
        ).fetchall()]
        if not ripple_ids:
            continue
        if all(r in running_ripples for r in ripple_ids):
            continue

        next_gen = db.execute("""
            SELECT COALESCE(MAX(pr.generation), 0) + 1
            FROM pond_run pr
            JOIN pond_version pv2 ON pv2.id = pr.pond_version_id
            WHERE pv2.pond_id = ? AND pv2.major = ?
        """, (pond_id, major)).fetchone()[0]

        result.append(_PondInfo(
            pond_version_id=pv_id,
            pond_id=pond_id,
            pond_major=major,
            pond_name=name,
            version=version,
            source_path=source_path,
            next_gen=next_gen,
        ))
    return result


def _inter_pond_ready(db: sqlite3.Connection, pv_id: int, pond_id: int) -> bool:
    """Returns True if inter-pond source readiness is satisfied for this pond_version."""
    sources = db.execute("""
        SELECT p2p.source_pond_id, p2p.source_major, p2p.required
        FROM pond_to_pond p2p
        WHERE p2p.pond_version_id = ?
    """, (pv_id,)).fetchall()

    if not sources:
        return True

    required_sources = [(s, m) for s, m, req in sources if req]
    optional_sources = [(s, m) for s, m, req in sources if not req]

    def _latest_gen(source_pond_id: int, source_major: int) -> int:
        row = db.execute("""
            SELECT COALESCE(MAX(pr.generation), 0)
            FROM pond_run pr
            JOIN pond_version pv ON pv.id = pr.pond_version_id
            WHERE pv.pond_id = ? AND pv.major = ? AND pr.status = 'success'
        """, (source_pond_id, source_major)).fetchone()
        return row[0] if row else 0

    def _watermark(source_pond_id: int, source_major: int) -> int:
        row = db.execute("""
            SELECT generation FROM watermark
            WHERE sink_pond_id = ? AND source_pond_id = ? AND source_major = ?
        """, (pond_id, source_pond_id, source_major)).fetchone()
        return row[0] if row else 0

    if required_sources:
        return all(
            _latest_gen(s, m) > _watermark(s, m)
            for s, m in required_sources
        )
    # No required sources — any optional source with unconsumed changes suffices.
    return any(
        _latest_gen(s, m) > _watermark(s, m)
        for s, m in optional_sources
    )


def _create_pond_run(db: sqlite3.Connection, pond_info: _PondInfo) -> None:
    run_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO pond_run (id, pond_version_id, generation, status) VALUES (?, ?, ?, 'running')",
        (run_id, pond_info.pond_version_id, pond_info.next_gen),
    )
    pond_info._run_id = run_id  # stash for _write_pipeline_demand


def _write_pipeline_demand(db: sqlite3.Connection, pond_info: _PondInfo) -> None:
    sources = db.execute("""
        SELECT pv.id
        FROM pond_to_pond p2p
        JOIN pond_version pv ON pv.pond_id = p2p.source_pond_id
            AND pv.major = p2p.source_major AND pv.is_active = 1
        WHERE p2p.pond_version_id = ?
    """, (pond_info.pond_version_id,)).fetchall()

    for (src_pv_id,) in sources:
        db.execute("""
            INSERT INTO demand (pond_version_id, sink_id)
            SELECT ?, ?
            WHERE NOT EXISTS (SELECT 1 FROM demand WHERE pond_version_id = ?)
        """, (src_pv_id, pond_info.pond_version_id, src_pv_id))


def _find_executable_ripples(db: sqlite3.Connection, running_ripples: set[int]) -> list[_RippleTask]:
    rows = db.execute("""
        SELECT r.id, pr.id, r.name, pv.source_path, p.name, pv.version
        FROM ripple r
        JOIN pond_version pv ON pv.id = r.pond_version_id
        JOIN pond p ON p.id = pv.pond_id
        JOIN pond_run pr ON pr.pond_version_id = pv.id AND pr.status = 'running'
        WHERE
          NOT EXISTS (
              SELECT 1 FROM ripple_run rr
              WHERE rr.ripple_id = r.id AND rr.pond_run_id = pr.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM ripple_to_ripple rtr
              WHERE rtr.sink_id = r.id
                AND rtr.source_id IN (
                    SELECT id FROM ripple WHERE pond_version_id = pv.id
                )
                AND NOT EXISTS (
                    SELECT 1 FROM ripple_run rr2
                    WHERE rr2.ripple_id = rtr.source_id
                      AND rr2.pond_run_id = pr.id
                      AND rr2.status = 'success'
                )
          )
    """).fetchall()

    result = []
    for ripple_id, pond_run_id, ripple_name, source_path, pond_name, version in rows:
        if ripple_id in running_ripples:
            continue

        # Consumed check for root ripples (no intra-pond parents).
        is_root = not db.execute(
            "SELECT 1 FROM ripple_to_ripple WHERE sink_id = ? AND source_id IN "
            "(SELECT id FROM ripple WHERE pond_version_id = ("
            "  SELECT pond_version_id FROM ripple WHERE id = ?))",
            (ripple_id, ripple_id),
        ).fetchone()

        if is_root and not _consumed(db, ripple_id, pond_run_id):
            continue

        result.append(_RippleTask(
            ripple_id=ripple_id,
            pond_run_id=pond_run_id,
            ripple_name=ripple_name,
            source_path=source_path,
            pond_name=pond_name,
            version=version,
        ))
    return result


def _consumed(db: sqlite3.Connection, ripple_id: int, current_pond_run_id: str) -> bool:
    """True if the ripple's immediate intra-pond children have all been dispatched
    in every older still-running pond_run for this pond_version.

    Vacuously true if the ripple has no intra-pond children (leaf = root single-ripple pond).
    Also vacuously true if there are no older in-flight runs.
    """
    children = db.execute(
        "SELECT sink_id FROM ripple_to_ripple WHERE source_id = ? "
        "AND sink_id IN (SELECT id FROM ripple WHERE pond_version_id = ("
        "  SELECT pond_version_id FROM ripple WHERE id = ?))",
        (ripple_id, ripple_id),
    ).fetchall()

    if not children:
        return True

    child_ids = [c[0] for c in children]

    # Get older in-flight pond_runs for the same pond_version.
    pv_row = db.execute(
        "SELECT pond_version_id FROM ripple WHERE id = ?", (ripple_id,)
    ).fetchone()
    if not pv_row:
        return True
    pv_id = pv_row[0]

    older_runs = db.execute("""
        SELECT pr.id FROM pond_run pr
        WHERE pr.pond_version_id = ? AND pr.status = 'running' AND pr.id != ?
    """, (pv_id, current_pond_run_id)).fetchall()

    for (run_id,) in older_runs:
        for child_id in child_ids:
            if not db.execute(
                "SELECT 1 FROM ripple_run WHERE ripple_id = ? AND pond_run_id = ?",
                (child_id, run_id),
            ).fetchone():
                return False
    return True


def _create_ripple_run(db: sqlite3.Connection, ripple_id: int, pond_run_id: str) -> None:
    run_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO ripple_run (id, pond_run_id, ripple_id, status) VALUES (?, ?, ?, 'running')",
        (run_id, pond_run_id, ripple_id),
    )


def _propagate_blocked(db: sqlite3.Connection) -> bool:
    """For ponds with demand that are not inter-pond ready, write demand to their sources."""
    blocked = db.execute("""
        SELECT DISTINCT pv.id, p.id
        FROM demand d
        JOIN pond_version pv ON pv.id = d.pond_version_id
        JOIN pond p ON p.id = pv.pond_id
        WHERE pv.is_active = 1
    """).fetchall()

    inserted = False
    for pv_id, pond_id in blocked:
        if _inter_pond_ready(db, pv_id, pond_id):
            continue
        sources = db.execute("""
            SELECT pv2.id
            FROM pond_to_pond p2p
            JOIN pond_version pv2 ON pv2.pond_id = p2p.source_pond_id
                AND pv2.major = p2p.source_major AND pv2.is_active = 1
            WHERE p2p.pond_version_id = ?
        """, (pv_id,)).fetchall()

        for (src_pv_id,) in sources:
            rows = db.execute("""
                INSERT INTO demand (pond_version_id, sink_id)
                SELECT ?, ?
                WHERE NOT EXISTS (SELECT 1 FROM demand WHERE pond_version_id = ?)
            """, (src_pv_id, pv_id, src_pv_id)).rowcount
            if rows:
                inserted = True
    return inserted


# ---------------------------------------------------------------------------
# Async dispatch
# ---------------------------------------------------------------------------

async def _dispatch(task: _RippleTask, db_path, registry_path, root, executor, queue, running_ripples: set[int]):
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            executor,
            _execute_ripple,
            task.ripple_id,
            task.pond_run_id,
            task.ripple_name,
            task.source_path,
            task.pond_name,
            task.version,
            str(db_path),
            str(registry_path),
            str(root),
        )
    finally:
        running_ripples.discard(task.ripple_id)
        queue.put_nowait(None)


# ---------------------------------------------------------------------------
# Worker (runs in executor process)
# ---------------------------------------------------------------------------

def _execute_ripple(
    ripple_id: int,
    pond_run_id: str,
    ripple_name: str,
    source_path: str,
    pond_name: str,
    version: str,
    db_path_str: str,
    registry_path_str: str,
    root_str: str,
) -> None:
    import duckdb

    db = _connect(db_path_str)
    registry = duckdb.connect(registry_path_str)

    try:
        db.execute(
            "UPDATE ripple_run SET started_at = datetime('now') "
            "WHERE ripple_id = ? AND pond_run_id = ?",
            (ripple_id, pond_run_id),
        )
        db.commit()

        root = Path(root_str)
        from duckstring.core import Pond
        pond_handle = Pond(name=pond_name, version=version, con=registry, root=root)

        func = _load_ripple_func(source_path, root_str, ripple_name)
        func(pond_handle)

        db.execute(
            "UPDATE ripple_run SET status = 'success', finished_at = datetime('now') "
            "WHERE ripple_id = ? AND pond_run_id = ?",
            (ripple_id, pond_run_id),
        )

        # Determine the pond_version_id for this pond_run.
        (pv_id,) = db.execute(
            "SELECT pond_version_id FROM pond_run WHERE id = ?", (pond_run_id,)
        ).fetchone()

        total_ripples = db.execute(
            "SELECT COUNT(*) FROM ripple WHERE pond_version_id = ?", (pv_id,)
        ).fetchone()[0]
        done_ripples = db.execute(
            "SELECT COUNT(*) FROM ripple_run WHERE pond_run_id = ? AND status = 'success'",
            (pond_run_id,),
        ).fetchone()[0]

        if total_ripples == done_ripples:
            db.execute(
                "UPDATE pond_run SET status = 'success', finished_at = datetime('now') WHERE id = ?",
                (pond_run_id,),
            )
            (pond_id, major) = db.execute(
                "SELECT p.id, pv.major FROM pond_version pv JOIN pond p ON p.id = pv.pond_id WHERE pv.id = ?",
                (pv_id,),
            ).fetchone()
            gen = db.execute(
                "SELECT generation FROM pond_run WHERE id = ?", (pond_run_id,)
            ).fetchone()[0]

            # Advance watermarks for each inter-pond source.
            sources = db.execute(
                "SELECT source_pond_id, source_major FROM pond_to_pond WHERE pond_version_id = ?",
                (pv_id,),
            ).fetchall()
            for src_pond_id, src_major in sources:
                latest = db.execute("""
                    SELECT COALESCE(MAX(pr.generation), 0)
                    FROM pond_run pr
                    JOIN pond_version pv ON pv.id = pr.pond_version_id
                    WHERE pv.pond_id = ? AND pv.major = ? AND pr.status = 'success'
                """, (src_pond_id, src_major)).fetchone()[0]
                db.execute("""
                    INSERT INTO watermark (sink_pond_id, source_pond_id, source_major, generation)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (sink_pond_id, source_pond_id, source_major)
                    DO UPDATE SET generation = excluded.generation
                """, (pond_id, src_pond_id, src_major, latest))

            db.execute(
                "DELETE FROM demand WHERE pond_version_id = ?", (pv_id,)
            )

        db.commit()

    except Exception:
        db.execute(
            "UPDATE ripple_run SET status = 'failed', finished_at = datetime('now') "
            "WHERE ripple_id = ? AND pond_run_id = ?",
            (ripple_id, pond_run_id),
        )
        db.execute(
            "UPDATE pond_run SET status = 'failed', finished_at = datetime('now') WHERE id = ?",
            (pond_run_id,),
        )
        db.commit()
        raise
    finally:
        registry.close()
        db.close()


def _load_ripple_func(source_path: str, root: str, ripple_name: str):
    from duckstring.core import collect_ripples

    src = str(Path(root) / source_path / "src")
    with _import_lock:
        before = set(sys.modules.keys())
        sys.path.insert(0, src)
        try:
            sys.modules.pop("pond", None)
            importlib.invalidate_caches()
            mod = importlib.import_module("pond")
            collect_ripples()  # drain global registry
            return getattr(mod, ripple_name)
        finally:
            if src in sys.path:
                sys.path.remove(src)
            for k in list(sys.modules):
                if k not in before:
                    sys.modules.pop(k, None)
