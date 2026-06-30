"""Tier-1/2 **state sync** — durable hot state for an ephemeral / scale-to-zero node.

On a cloud node whose local disk is ephemeral (Databricks Apps, a scale-to-zero container) the data
plane is already durable in object storage (see ``storage.py``), but the *hot state* — ``duck.db`` and
the per-Pond ``pond.db`` ledgers + ``registry.duckdb`` working registries — lives on local disk and is
lost on a redeploy/scale-down. We get durability the Litestream/LiteFS way: keep the live files on local
POSIX disk (SQLite/DuckDB need it) and **sync snapshots out** to ``DUCKSTRING_STATE_BACKUP_URI`` — never
relocate the live files.

Two tiers, by recoverability (``plans/storage-decoupling.md``):

- **Tier 1 — ``duck.db`` (mandatory, small, frequent).** Everything ``Driver.reload`` needs to rebuild
  the engine + run history. Snapshotted via the SQLite **backup API** (safe on a live, WAL-mode DB) and
  pushed on an interval and on graceful shutdown. This is what makes scale-to-zero safe.
- **Tier 2 — ledgers + registries (best-effort warm cache).** DuckDB has no online-backup, so these are
  only safe to copy when **quiescent** — we package them (with a fresh ``duck.db`` snapshot) into a single
  ``state.tar`` object on **graceful shutdown**, after the Ducks have been torn down. On restore they make
  the node warm; if absent (a hard crash left only the Tier-1 ``duck.db``), the engine still restores and
  the registries rebuild on demand — the durable Trickle state is in the data plane, so a missing registry
  is recompute cost, not data loss.

The single load-bearing invariant is the existing "run exactly ONE app process": a serial stop→start is
the only concurrency story, so there is never a competing writer to the backup.
"""

from __future__ import annotations

import io
import re
import sqlite3
import tarfile
import tempfile
from pathlib import Path

_SKIP_SUFFIXES = (".db-wal", ".db-shm")  # subsumed by the SQLite snapshot
_SKIP_NAMES = {"secrets.json", "secrets.json.tmp"}  # the write-only secret store never travels in a bundle
_TIER1 = "duck.db"  # the frequent small Tier-1 object
_TIER2 = "state.tar"  # the on-shutdown warm bundle (ledgers + registries + a duck.db snapshot)


def parse_interval_seconds(text: str | None, default: float = 60.0) -> float:
    """Seconds from a duration like ``30s`` / ``5m`` / ``1h`` (single unit). Falls back to ``default``
    on an empty/invalid value — a checkpoint cadence is operational, not worth failing boot over."""
    if not text:
        return default
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])?\s*", str(text).lower())
    if not m:
        return default
    n = int(m.group(1))
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(m.group(2) or "s", 1)


def _snapshot_sqlite_bytes(path: Path) -> bytes:
    """A consistent point-in-time copy of a (possibly live, WAL-mode) SQLite DB, as bytes."""
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "snap.db"
        src = sqlite3.connect(str(path))
        dst = sqlite3.connect(str(dest))
        try:
            with dst:
                src.backup(dst)
        finally:
            src.close()
            dst.close()
        return dest.read_bytes()


def checkpoint_tier1(root: Path, backup_uri: str | None) -> bool:
    """Push a fresh ``duck.db`` snapshot to the backup. Returns whether it ran (False = no backup
    configured / no DB yet). Best-effort: swallows transport errors so a checkpoint never breaks a run."""
    if not backup_uri:
        return False
    db = Path(root) / "duck.db"
    if not db.exists():
        return False
    from ..storage import get_storage

    try:
        get_storage(backup_uri).write_bytes(_snapshot_sqlite_bytes(db), _TIER1)
        return True
    except Exception as exc:  # pragma: no cover - transport hiccup must not break the loop/shutdown
        import logging

        logging.getLogger(__name__).warning("Tier-1 state checkpoint failed: %s", exc)
        return False


def checkpoint_full(root: Path, backup_uri: str | None) -> bool:
    """Package the whole **quiescent** state root (a ``duck.db`` snapshot + the ``pond.db`` ledgers +
    ``registry.duckdb`` registries, minus WAL sidecars and the secret store) into a single ``state.tar``
    object, and refresh Tier-1. Call only when the Ducks are stopped (graceful shutdown) — DuckDB
    registries have no live-copy guarantee. Best-effort."""
    if not backup_uri:
        return False
    root = Path(root)
    from ..storage import get_storage

    try:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.name.endswith(_SKIP_SUFFIXES) or path.name in _SKIP_NAMES:
                    continue
                arc = path.relative_to(root).as_posix()
                if path.suffix == ".db":  # SQLite → consistent snapshot
                    data = _snapshot_sqlite_bytes(path)
                    info = tarfile.TarInfo(arc)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
                else:
                    tar.add(path, arcname=arc, recursive=False)
        storage = get_storage(backup_uri)
        storage.write_bytes(buf.getvalue(), _TIER2)
        storage.write_bytes(_snapshot_sqlite_bytes(root / "duck.db"), _TIER1)
        return True
    except Exception as exc:  # pragma: no cover
        import logging

        logging.getLogger(__name__).warning("Tier-2 state checkpoint failed: %s", exc)
        return False


def _pull_state(storage, root: Path) -> bool:
    """Pull state from a backup ``storage`` into ``root``: the warm ``state.tar`` (ledgers + registries)
    if present, else the Tier-1 ``duck.db`` alone. Returns whether anything landed."""
    root.mkdir(parents=True, exist_ok=True)
    if storage.exists(_TIER2):
        with tarfile.open(fileobj=io.BytesIO(storage.read_bytes(_TIER2)), mode="r") as tar:
            try:
                tar.extractall(root, filter="data")
            except TypeError:  # Python without the extraction-filter backport
                tar.extractall(root)
        return True
    if storage.exists(_TIER1):
        (root / "duck.db").write_bytes(storage.read_bytes(_TIER1))
        return True
    return False


def restore_state_if_empty(root: Path, backup_uri: str | None) -> bool:
    """If the state root has no ``duck.db`` (a fresh / scaled-to-zero node) and the backup holds state,
    pull it **before** ``migrate``/``reload``. Prefers the warm ``state.tar`` (ledgers + registries);
    falls back to the Tier-1 ``duck.db`` alone (engine restores; registries rebuild on demand). Returns
    whether anything was restored."""
    if not backup_uri:
        return False
    root = Path(root)
    if (root / "duck.db").exists():
        return False  # state already present — never clobber a live root
    from ..storage import get_storage

    try:
        return _pull_state(get_storage(backup_uri), root)
    except Exception as exc:  # pragma: no cover
        import logging

        logging.getLogger(__name__).warning("state restore skipped: %s", exc)
        return False


def restore_state(root: Path, backup_uri: str) -> bool:
    """Pull state from ``backup_uri`` into ``root`` unconditionally (the explicit inverse of the boot-time
    auto-restore — for seeding a fresh node by hand). Returns whether anything was restored."""
    from ..storage import get_storage

    return _pull_state(get_storage(backup_uri), Path(root))


async def run_checkpoint_worker(root: Path, backup_uri: str | None, interval: str | None) -> None:
    """An async loop (modelled on the egress worker / poller) pushing the Tier-1 ``duck.db`` snapshot to
    the backup every ``interval``. No-op (returns) when no backup is configured. The Tier-2 bundle is
    flushed once on graceful shutdown by the lifespan, not here."""
    if not backup_uri:
        return
    import asyncio

    from fastapi.concurrency import run_in_threadpool

    seconds = parse_interval_seconds(interval)
    while True:
        await asyncio.sleep(max(5.0, seconds))
        await run_in_threadpool(checkpoint_tier1, root, backup_uri)
