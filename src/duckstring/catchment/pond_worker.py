from __future__ import annotations

import importlib
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _log(event: str, name: str, gen: int | None = None, duration: float | None = None) -> None:
    gen_col = f"gen={gen}" if gen is not None else ""
    dur_col = f"{duration:.2f}s" if duration is not None else ""
    print(f"[{_ts()}] {event:<8} {gen_col:<8} {dur_col:<7} {name}", flush=True)

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
    gen: int = 0,
) -> None:
    name_ver = f"{pond_name} v{version}"
    _log("start", name_ver, gen=gen)
    t0 = time.monotonic()
    executor = ThreadPoolExecutor(max_workers=8)
    try:
        _execute_run(run_id, pv_id, pond_name, version, source_path, db_path_str, root_str, executor)
        _log("done", name_ver, gen=gen, duration=time.monotonic() - t0)
    except Exception:
        _log("failed", name_ver, gen=gen, duration=time.monotonic() - t0)
        traceback.print_exc()
        raise
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

    pond_id, major = db.execute(
        "SELECT pond_id, major FROM pond_version WHERE id = ?", (pv_id,)
    ).fetchone()

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
                traceback.print_exception(type(exc), exc, exc.__traceback__)
            else:
                completed.add(ripple_id)
                _mark_ripple(db, ripple_id, run_id, "success")
                # Push-style: dispatch children whose parents are all done.
                for child_id in children[ripple_id]:
                    if child_id not in completed and all(
                        p in completed for p in parents[child_id]
                    ):
                        _dispatch(child_id)
            db.commit()  # commit mark + any dispatched ripple_run inserts together
            if len(completed) + len(failed) == total:
                done_event.set()

    def _dispatch(ripple_id: int) -> None:
        # Always called with lock held.
        _create_ripple_run(db, ripple_id, run_id)
        # Load the ripple function before releasing the lock, but the write
        # transaction must be committed first — module loading involves file I/O
        # and can take long enough to hit the SQLite busy timeout in other processes.
        db.commit()
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

        immediate_retries, source_retries = db.execute(
            "SELECT immediate_retries, source_retries FROM pond_version WHERE id = ?", (pv_id,)
        ).fetchone()

        fail_count = db.execute("""
            SELECT COUNT(*) FROM pond_run pr
            JOIN pond_version pv ON pv.id = pr.pond_version_id
            WHERE pv.pond_id = ? AND pv.major = ? AND pr.status = 'failed'
            AND pr.started_at > COALESCE((
                SELECT MAX(pr2.finished_at) FROM pond_run pr2
                JOIN pond_version pv2 ON pv2.id = pr2.pond_version_id
                WHERE pv2.pond_id = ? AND pv2.major = ? AND pr2.status = 'success'
            ), '1900-01-01')
        """, (pond_id, major, pond_id, major)).fetchone()[0]

        has_sources = db.execute(
            "SELECT COUNT(*) FROM pond_to_pond WHERE pond_version_id = ?", (pv_id,)
        ).fetchone()[0] > 0

        if fail_count <= immediate_retries:
            pass  # keep demand, retry immediately on next sentinel cycle
        elif has_sources and fail_count <= immediate_retries + source_retries:
            # Advance retry_watermark to current source generation; pond waits for new data.
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
                    INSERT INTO retry_watermark (sink_pond_id, source_pond_id, source_major, generation)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (sink_pond_id, source_pond_id, source_major)
                    DO UPDATE SET generation = excluded.generation
                """, (pond_id, src_pond_id, src_major, latest))
        else:
            # Retries exhausted — go silent and send stop upstream.
            db.execute("UPDATE pond_version SET is_stopped = 1 WHERE id = ?", (pv_id,))
            db.execute("DELETE FROM demand WHERE pond_version_id = ?", (pv_id,))
            _send_stop_upstream(db, pv_id, pond_id)
            _log("blocked", f"{pond_name} v{version}")

        db.commit()
        db.close()
        raise failed[0]

    # All ripples succeeded — export tables to Parquet for cross-pond consumption,
    # then finalise pond_run and advance watermarks.
    _export_parquet(registry_path)
    db.execute(
        "UPDATE pond_run SET status='success', finished_at=datetime('now') WHERE id=?",
        (run_id,),
    )

    # Re-check stop acknowledgment at completion time (stop-only records may have
    # arrived mid-run). Clear retry history and stop records, then re-enter stopped
    # state if this was a stop-acknowledged run.
    is_stop_run = _acknowledges_stop(db, pv_id)
    db.execute("DELETE FROM retry_watermark WHERE sink_pond_id = ?", (pond_id,))
    db.execute("DELETE FROM stop WHERE pond_version_id = ?", (pv_id,))
    if is_stop_run:
        db.execute("UPDATE pond_version SET is_stopped = 1 WHERE id = ?", (pv_id,))

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
# Stop helpers (duplicated from orchestrator — pond_worker runs in a separate process)
# ---------------------------------------------------------------------------

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


def _send_stop_upstream(db: sqlite3.Connection, pv_id: int, pond_id: int) -> None:
    """Send stop-only records to sources when retries are exhausted.

    Subject to the unanimous-sinks rule: stop is only forwarded to a source if
    all active sinks of that source are stopped.
    """
    sources = db.execute("""
        SELECT pv2.id, p2.name, pv2.version, p2.id AS src_pond_id
        FROM pond_to_pond p2p
        JOIN pond_version pv2 ON pv2.pond_id = p2p.source_pond_id
            AND pv2.major = p2p.source_major AND pv2.is_active = 1
        JOIN pond p2 ON p2.id = pv2.pond_id
        WHERE p2p.pond_version_id = ?
    """, (pv_id,)).fetchall()
    for src_pv_id, src_name, src_ver, src_pond_id in sources:
        unstopped_sink = db.execute("""
            SELECT 1 FROM pond_to_pond p2p2
            JOIN pond_version pv3 ON pv3.id = p2p2.pond_version_id AND pv3.is_active = 1
            WHERE p2p2.source_pond_id = ? AND pv3.is_stopped = 0
        """, (src_pond_id,)).fetchone()
        if unstopped_sink:
            continue
        db.execute("""
            INSERT INTO stop (pond_version_id, sink_id)
            SELECT ?, ?
            WHERE NOT EXISTS (SELECT 1 FROM stop WHERE pond_version_id = ? AND sink_id IS ?)
        """, (src_pv_id, pv_id, src_pv_id, pv_id))
        _log("stop", f"{src_name} v{src_ver}")


# ---------------------------------------------------------------------------
# Parquet export
# ---------------------------------------------------------------------------

def _export_parquet(registry_path: Path) -> None:
    import duckdb

    data_dir = registry_path.parent / "data"
    data_dir.mkdir(exist_ok=True)
    con = duckdb.connect(str(registry_path), read_only=True)
    try:
        tables = [row[0] for row in con.execute("SHOW TABLES").fetchall()]
        for table in tables:
            dest = data_dir / f"{table}.parquet"
            tmp = data_dir / f"{table}.parquet.tmp"
            con.execute(f'COPY "{table}" TO \'{tmp}\' (FORMAT PARQUET)')
            tmp.replace(dest)
    finally:
        con.close()


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


