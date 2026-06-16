"""Catchment-level endpoints: health, and the state download (`usage` + `archive`).

The archive is a tar stream of the whole Catchment root — the database, deployed artifacts,
exported data, registries, and ledgers. SQLite files (`duck.db`, the Duck ledgers) are added as
consistent snapshots via the backup API; live WAL sidecars are skipped (the snapshot subsumes
them). DuckDB registries are copied as-is, so download while the Catchment is quiescent if you
need the registries to be coherent.
"""

from __future__ import annotations

import io
import queue
import sqlite3
import tarfile
import tempfile
import threading
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter()

_SKIP_SUFFIXES = (".db-wal", ".db-shm")  # subsumed by the SQLite snapshot


def _db(request: Request) -> sqlite3.Connection:
    return request.app.state.db


@router.get("/health")
def health(request: Request):
    _db(request).execute("SELECT 1")
    return {"status": "ok"}


@router.get("/catchment/identity")
def identity(request: Request):
    """This Catchment's stable id + optional display name — how a downstream resolves cross-mesh
    identity (which upstream a duct points at, and cutting cycles in the recursive lineage view)."""
    rows = dict(_db(request).execute("SELECT key, value FROM catchment_meta").fetchall())
    return {"id": rows.get("id"), "name": rows.get("name")}


def _root_files(root: Path) -> list[tuple[Path, str]]:
    """Every regular file in the root as (path, root-relative arcname), WAL sidecars skipped."""
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.endswith(_SKIP_SUFFIXES):
            continue
        files.append((path, path.relative_to(root).as_posix()))
    return files


def _tar_size(file_sizes: list[int]) -> int:
    """The size of an uncompressed tar of files with these sizes (512-byte header + block-padded
    content per file, 1024-byte end marker) — lets the client show a real progress total."""
    total = sum(512 + ((size + 511) // 512) * 512 for size in file_sizes)
    return total + 1024


@router.get("/catchment/usage")
def usage(request: Request):
    """The root's total state size — what `catchment download` would pull. ``archive_bytes`` is a
    close estimate of the tar the archive endpoint streams (SQLite snapshots and long path headers
    can shift it slightly) — good enough for a progress total."""
    files = _root_files(Path(request.app.state.root))
    sizes = [p.stat().st_size for p, _ in files]
    return {"total_bytes": sum(sizes), "file_count": len(files), "archive_bytes": _tar_size(sizes)}


def _sqlite_snapshot(path: Path, tmpdir: str) -> Path:
    """A consistent point-in-time copy of a (possibly live, WAL-mode) SQLite database."""
    dest = Path(tmpdir) / f"{abs(hash(str(path)))}-{path.name}"
    src = sqlite3.connect(str(path))
    dst = sqlite3.connect(str(dest))
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    return dest


class _QueueWriter(io.RawIOBase):
    """File-like adapter: tarfile writes blocks, the response generator drains them."""

    def __init__(self, q: queue.Queue):
        self.q = q

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        self.q.put(bytes(b))
        return len(b)


@router.get("/catchment/archive")
def archive(request: Request):
    """Stream the Catchment root as an uncompressed tar (no server-side temp copy of the data;
    SQLite files are snapshotted one at a time)."""
    root = Path(request.app.state.root)
    files = _root_files(root)
    q: queue.Queue = queue.Queue(maxsize=64)  # bounded: production blocks until the client drains

    def produce() -> None:
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                with tarfile.open(fileobj=_QueueWriter(q), mode="w|") as tar:
                    for path, arcname in files:
                        src = _sqlite_snapshot(path, tmpdir) if path.suffix == ".db" else path
                        tar.add(src, arcname=arcname, recursive=False)
        finally:
            q.put(None)

    threading.Thread(target=produce, daemon=True).start()

    def stream():
        while (chunk := q.get()) is not None:
            yield chunk

    return StreamingResponse(
        stream(),
        media_type="application/x-tar",
        headers={"Content-Disposition": 'attachment; filename="catchment.tar"'},
    )
