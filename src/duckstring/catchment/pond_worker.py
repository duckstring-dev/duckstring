from __future__ import annotations

import importlib
import sqlite3
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_import_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public entry point for ProcessPoolExecutor
# ---------------------------------------------------------------------------

def execute_pond_run(
    run_id: str,
    pv_id: int,
    pond_name: str,
    version: str,
    source_path: str,
    db_path_str: str,
    root_str: str,
) -> None:
    executor = ThreadPoolExecutor(max_workers=8)
    try:
        _execute_run(run_id, pv_id, pond_name, version, source_path, db_path_str, root_str, executor)
    finally:
        executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Run execution
# ---------------------------------------------------------------------------

def _execute_run(
    run_id: str,
    pv_id: int,
    pond_name: str,
    version: str,
    source_path: str,
    db_path_str: str,
    root_str: str,
    executor: ThreadPoolExecutor,
) -> None:
    root = Path(root_str)

    registry_path = root / "ponds" / pond_name / "registry.duckdb"
    registry_path.parent.mkdir(parents=True, exist_ok=True)

    db = _connect(db_path_str)

    # Load ripple topology from SQLite.
    ripple_names: dict[int, str] = {
        r[0]: r[1]
        for r in db.execute(
            "SELECT id, name FROM ripple WHERE pond_version_id = ?", (pv_id,)
        ).fetchall()
    }
    children: dict[int, list[int]] = {rid: [] for rid in ripple_names}
    parents: dict[int, list[int]] = {rid: [] for rid in ripple_names}
    for sink_id, source_id in db.execute(
        "SELECT sink_id, source_id FROM ripple_to_ripple "
        "WHERE sink_id IN (SELECT id FROM ripple WHERE pond_version_id = ?)",
        (pv_id,),
    ).fetchall():
        children[source_id].append(sink_id)
        parents[sink_id].append(source_id)

    # Shared state for the push-style dispatch callbacks.
    completed: set[int] = set()
    failed: list[Exception] = []
    lock = threading.Lock()
    done_event = threading.Event()
    total = len(ripple_names)

    def ripple_done(ripple_id: int, fut) -> None:
        exc = fut.exception()
        with lock:
            if exc:
                failed.append(exc)
                _mark_ripple(db, ripple_id, run_id, "failed")
            else:
                completed.add(ripple_id)
                _mark_ripple(db, ripple_id, run_id, "success")
                # Push-style: dispatch children whose parents are all done.
                for child_id in children[ripple_id]:
                    if child_id not in completed and all(
                        p in completed for p in parents[child_id]
                    ):
                        _dispatch(child_id)
            db.commit()
            if len(completed) + len(failed) == total:
                done_event.set()

    def _dispatch(ripple_id: int) -> None:
        # Always called with lock held.
        _create_ripple_run(db, ripple_id, run_id)
        func = _load_ripple_func(source_path, root_str, ripple_names[ripple_id])
        fut = executor.submit(
            _run_ripple, func, pond_name, version, str(registry_path), root_str
        )
        fut.add_done_callback(lambda f, rid=ripple_id: ripple_done(rid, f))

    with lock:
        roots = [rid for rid in ripple_names if not parents[rid]]
        if not roots:
            done_event.set()
        else:
            for rid in roots:
                _dispatch(rid)

    done_event.wait()
    db.commit()

    if failed:
        db.execute(
            "UPDATE pond_run SET status='failed', finished_at=datetime('now') WHERE id=?",
            (run_id,),
        )
        db.commit()
        db.close()
        raise failed[0]

    # All ripples succeeded — finalise pond_run and advance watermarks.
    db.execute(
        "UPDATE pond_run SET status='success', finished_at=datetime('now') WHERE id=?",
        (run_id,),
    )
    pond_id = db.execute(
        "SELECT pond_id FROM pond_version WHERE id = ?", (pv_id,)
    ).fetchone()[0]
    for src_pond_id, src_major in db.execute(
        "SELECT source_pond_id, source_major FROM pond_to_pond WHERE pond_version_id = ?",
        (pv_id,),
    ).fetchall():
        latest = db.execute("""
            SELECT COALESCE(MAX(pr.generation), 0)
            FROM pond_run pr JOIN pond_version pv ON pv.id = pr.pond_version_id
            WHERE pv.pond_id = ? AND pv.major = ? AND pr.status = 'success'
        """, (src_pond_id, src_major)).fetchone()[0]
        db.execute("""
            INSERT INTO watermark (sink_pond_id, source_pond_id, source_major, generation)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (sink_pond_id, source_pond_id, source_major)
            DO UPDATE SET generation = excluded.generation
        """, (pond_id, src_pond_id, src_major, latest))
    db.execute("DELETE FROM demand WHERE pond_version_id = ?", (pv_id,))
    db.commit()
    db.close()


def _run_ripple(
    func, pond_name: str, version: str, registry_path_str: str, root_str: str
) -> None:
    import duckdb
    from duckstring.core import Pond

    registry = duckdb.connect(registry_path_str)
    try:
        pond_handle = Pond(name=pond_name, version=version, con=registry, root=Path(root_str))
        func(pond_handle)
    finally:
        registry.close()


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def _connect(db_path_str: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path_str, check_same_thread=False)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def _create_ripple_run(db: sqlite3.Connection, ripple_id: int, run_id: str) -> None:
    db.execute(
        "INSERT INTO ripple_run (id, pond_run_id, ripple_id, status) VALUES (?, ?, ?, 'running')",
        (str(uuid.uuid4()), run_id, ripple_id),
    )


def _mark_ripple(db: sqlite3.Connection, ripple_id: int, run_id: str, status: str) -> None:
    db.execute(
        "UPDATE ripple_run SET status = ?, finished_at = datetime('now') "
        "WHERE ripple_id = ? AND pond_run_id = ?",
        (status, ripple_id, run_id),
    )


# ---------------------------------------------------------------------------
# Ripple function loading
# ---------------------------------------------------------------------------

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
            collect_ripples()
            return getattr(mod, ripple_name)
        finally:
            if src in sys.path:
                sys.path.remove(src)
            for k in list(sys.modules):
                if k not in before:
                    sys.modules.pop(k, None)


