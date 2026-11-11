"""The AppSupervisor (plans/apps.md): runs each App's process on the Catchment's own machine.

An App's unit is an **instance**: one process, from start to stop, recorded in ``app_instance``. The first request
after a deploy or an idle stop starts one (the request waits until it is ready); it stops after the App's
``idle_timeout`` without a request, on a redeploy (the next request starts the new version), on a Kill or a
removal, and when the Catchment shuts down. An instance that exits on its own, or never becomes ready, counts
against the App's ``immediate_retries``; past them the App fails (``Driver.fail_app``) until an operator Wake or
Clear, or a redeploy.

The process runs ``[app] command`` through ``/bin/sh`` in the App's deployed directory, with its environment's
``bin`` first on ``PATH`` and ``PORT``, ``DUCKSTRING_APP_PATH``, ``DUCKSTRING_APP_STATE`` and ``DUCKSTRING_APP``
set. Its output goes to ``ponds/{name}/m{major}/app/logs/{instance}.log``. Its state directory,
``apps/{name}/m{major}/state/``, sits outside the line's runtime directory, so a Catchment reset keeps it.

While an instance runs, the App's ``[pond] memory`` is set aside from the budget the Pond Runs' memory slots
divide (``Driver.app_reserve``), and DuckDB's ``memory_limit`` in its handle is that memory.

**Read leases.** A Source keeps the version an App is reading (``Driver._retain_from``). Each request in flight
holds every Source line's published freshness at the moment it arrived: a read inside it resolves to that
version or a newer one, so the publish that would prune it keeps it until the request (and a streamed response
body) ends. A WebSocket connection renews its lease at each message from the browser and keeps the previous
one for ``WS_LEASE_GRACE_S``, so a long session holds a recent version, not the one it opened on. Work outside
a request takes a lease with ``pond.hold()``, a file in the runtime's ``holds/`` the handle writes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..app import ENV, app_stop_message, base_path
from .db import locked
from .registry import pond_major_dir

log = logging.getLogger(__name__)

DEFAULT_START_TIMEOUT_S = 60.0
STOP_GRACE_S = 10.0
WS_LEASE_GRACE_S = 60.0  # how long a WebSocket keeps the lease of its previous message
# Never handed to an App's process: the Catchment's own credentials.
_WITHHELD_ENV = ("DUCKSTRING_API_KEY",)


def app_runtime_dir(root: Path, name: str, major: int) -> Path:
    """An App line's runtime files (logs, the freshness file): part of the line's runtime, like a registry."""
    return pond_major_dir(root, name, major) / "app"


def app_state_dir(root: Path, name: str, major: int) -> Path:
    """An App line's durable state directory (``state = "local"``)."""
    return root / "apps" / name / f"m{major}" / "state"


class AppUnavailable(Exception):
    """The App can't serve this request: ``status`` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Instance:
    key: str
    id: int
    version_id: int
    deployed_at: str | None  # with version_id, the deploy it runs: a same-version redeploy replaces it
    proc: subprocess.Popen
    port: int
    log_path: Path
    ready: bool = False
    active: int = 0  # requests and WebSocket connections in flight
    last_used: float = field(default_factory=time.monotonic)
    stopping: bool = False
    # Read leases per request or WebSocket in flight: (monotonic time, {source key: changed_f}), oldest first.
    leases: dict[int, list[tuple[float, dict]]] = field(default_factory=dict)
    next_ticket: int = 0
    memory_limit: str | None = None


def _current(inst: Instance, view: dict) -> bool:
    """Whether ``inst`` runs the deploy ``view`` selects: its version, and that version's latest deploy."""
    return inst.version_id == view["version_id"] and inst.deployed_at == view["deployed_at"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AppSupervisor:
    def __init__(self, driver, root: Path, data_root: str | None = None):
        self.driver = driver
        self.root = Path(root)
        self.data_root = data_root
        self._instances: dict[str, Instance] = {}
        self._starts: dict[str, asyncio.Lock] = {}
        self._crashes: dict[str, int] = {}  # consecutive instances that exited on their own or never got ready
        self._freshness: dict[str, dict] = {}  # last freshness written per App, to write only on change
        self._mu = threading.Lock()
        self.start_timeout = float(os.environ.get("DUCKSTRING_APP_START_TIMEOUT", DEFAULT_START_TIMEOUT_S))
        driver.on_app_stop = self.stop_now
        driver.app_leases = self.leases
        driver.app_activity = self.activity

    # ─── serving a request ──────────────────────────────────────────────────

    async def acquire(self, key: str) -> Instance:
        """A ready instance of the App ``key``, started if needed. Raises :class:`AppUnavailable`."""
        view = self.driver.app_view(key)
        if view is None:
            raise AppUnavailable(404, "no such App")
        self._check_serving(key, view)
        inst = self._ready(key, view)
        if inst is not None:
            return inst
        lock = self._starts.setdefault(key, asyncio.Lock())
        async with lock:
            view = self.driver.app_view(key)
            if view is None:
                raise AppUnavailable(404, "no such App")
            self._check_serving(key, view)
            inst = self._ready(key, view)
            if inst is not None:
                return inst
            old = self._instances.get(key)
            if old is not None and old.proc.poll() is not None and not old.stopping:  # it died: count it
                await asyncio.to_thread(self._stop, old, "exited")
                self._exited(view, "exited", old.proc.returncode)
                view = self.driver.app_view(key)
                if view is None:
                    raise AppUnavailable(404, "no such App")
                self._check_serving(key, view)
            elif old is not None:  # an older deploy's instance
                await asyncio.to_thread(self._stop, old, "redeploy")
            if not view["app"]["command"]:
                raise AppUnavailable(404, "not found")  # a static-only App: nothing to start
            try:
                memory_limit = self.driver.app_reserve(key)
            except ValueError as e:
                raise AppUnavailable(503, str(e)) from None
            try:
                inst = await asyncio.to_thread(self._spawn, view, memory_limit)
            except BaseException:
                self.driver.app_release(key)
                raise
            await self._wait_ready(inst, view)
            return inst

    def _check_serving(self, key: str, view: dict) -> None:
        if view["is_killed"]:
            raise AppUnavailable(503, "the App was killed; an operator can wake it with `duckstring control wake`")
        if view["is_failed"]:
            raise AppUnavailable(503, "the App failed; see `duckstring app logs`, then wake it with "
                                      "`duckstring control wake`")
        # Not failed: any earlier run of exits was cleared (by a Wake, a Clear or a redeploy).
        if self._crashes.get(key, 0) > view["immediate_retries"]:
            self._crashes[key] = 0

    def _ready(self, key: str, view: dict) -> Instance | None:
        inst = self._instances.get(key)
        if (inst is not None and inst.ready and not inst.stopping and inst.proc.poll() is None
                and _current(inst, view)):
            return inst
        return None

    def begin(self, inst: Instance) -> int:
        """A request or WebSocket connection starts: it keeps the instance from idling and takes a read lease.
        Returns its ticket for :meth:`renew` and :meth:`end`."""
        lease = self.driver.app_sources_now(inst.key)
        now = time.monotonic()
        with self._mu:
            inst.active += 1
            inst.last_used = now
            inst.next_ticket += 1
            inst.leases[inst.next_ticket] = [(now, lease)]
            return inst.next_ticket

    def renew(self, inst: Instance, ticket: int) -> None:
        """A WebSocket message from the browser: lease the Sources' versions now, keep the previous lease for
        ``WS_LEASE_GRACE_S`` (the work it started may still be reading), and drop older ones."""
        lease = self.driver.app_sources_now(inst.key)
        now = time.monotonic()
        with self._mu:
            inst.last_used = now
            entries = inst.leases.get(ticket)
            if entries is None:
                return
            if entries[-1][1] != lease:
                entries.append((now, lease))
            # An entry is superseded when the next one is taken, and ends WS_LEASE_GRACE_S after that.
            keep = [e for e, nxt in zip(entries, entries[1:], strict=False) if now - nxt[0] < WS_LEASE_GRACE_S]
            inst.leases[ticket] = keep + [entries[-1]]

    def end(self, inst: Instance, ticket: int | None = None) -> None:
        with self._mu:
            inst.active -= 1
            inst.last_used = time.monotonic()
            inst.leases.pop(ticket, None)

    def leases(self, key: str, source_key: str, source_name: str) -> list[datetime]:
        """The versions of the Source line ``source_key`` the App ``key`` is reading: the leases of its requests
        and WebSockets in flight, and its explicit holds (``pond.hold()``). For ``Driver._retain_from``."""
        with self._mu:
            inst = self._instances.get(key)
            if inst is None:
                return []
            out = [lease[source_key] for entries in inst.leases.values() for _, lease in entries
                   if source_key in lease]
            holds = inst.log_path.parent.parent / "holds"
        for path in holds.glob("*.json") if holds.is_dir() else ():
            try:
                pid = int(path.stem.split("-")[0])
                os.kill(pid, 0)  # a hold of a process that has gone (a restarted worker) holds nothing
                f = json.loads(path.read_text()).get("sources", {}).get(source_name)
            except (ValueError, OSError):
                continue
            if f:
                out.append(datetime.fromisoformat(f))
        return out

    def activity(self, key: str, idle_timeout_s: float) -> dict | None:
        """For ``/api/status``: the requests in flight on ``key``'s instance, and when it will stop for idleness
        (``None`` while busy, or with no idle timeout). ``None`` with no instance."""
        with self._mu:
            inst = self._instances.get(key)
            if inst is None or inst.stopping:
                return None
            active, last_used = inst.active, inst.last_used
        idle_at = None
        if idle_timeout_s > 0 and active <= 0:
            left = max(0.0, idle_timeout_s - (time.monotonic() - last_used))
            idle_at = (datetime.now(timezone.utc) + timedelta(seconds=left)).isoformat()
        return {"active": max(active, 0), "idle_at": idle_at}

    # ─── starting ───────────────────────────────────────────────────────────

    def _spawn(self, view: dict, memory_limit: str | None = None) -> Instance:
        from ..environments import python_for

        name, major = view["name"], view["major"]
        cfg = view["app"]
        runtime = app_runtime_dir(self.root, name, major)
        (runtime / "logs").mkdir(parents=True, exist_ok=True)
        port = _free_port()
        db = self.driver.db
        with locked(db):
            cur = db.execute(
                "INSERT INTO app_instance (pond_version_id, started_at, port, log_path) VALUES (?, ?, ?, '')",
                (view["version_id"], _now_iso(), port))
            iid = cur.lastrowid
            log_path = runtime / "logs" / f"{iid}.log"
            db.execute("UPDATE app_instance SET log_path = ? WHERE id = ?",
                       (str(log_path.relative_to(self.root)), iid))
            db.commit()

        python = python_for(self.root, view["source_path"])
        env = {k: v for k, v in os.environ.items() if k not in _WITHHELD_ENV}
        env["PATH"] = str(Path(python).parent) + os.pathsep + env.get("PATH", "")
        env["PORT"] = str(port)
        env["DUCKSTRING_APP_PATH"] = base_path(name, major)
        env["PYTHONUNBUFFERED"] = "1"
        if cfg["state"] == "local":
            state = app_state_dir(self.root, name, major)
            state.mkdir(parents=True, exist_ok=True)
            env["DUCKSTRING_APP_STATE"] = str(state)
        env[ENV] = json.dumps({
            "name": name, "major": major, "version": view["version"], "root": str(self.root),
            "data_root": self.data_root, "sources": list(view["sources"]), "source_majors": view["sources"],
            "runtime": str(runtime), "memory_limit": memory_limit,
        })
        shutil.rmtree(runtime / "holds", ignore_errors=True)  # a previous instance's
        self._write_freshness(view["key"], runtime, force=True)
        with open(log_path, "ab") as log:
            log.write(f"[duckstring] starting {name}@{major} {view['version']} on port {port}\n".encode())
            log.flush()
            proc = subprocess.Popen(
                ["/bin/sh", "-c", "exec " + cfg["command"]], cwd=self.root / view["source_path"], env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,  # its own process group, so a stop reaches what the shell starts
            )
        with locked(db):
            db.execute("UPDATE app_instance SET pid = ? WHERE id = ?", (proc.pid, iid))
            db.commit()
        inst = Instance(key=view["key"], id=iid, version_id=view["version_id"], deployed_at=view["deployed_at"],
                        proc=proc, port=port,
                        log_path=log_path, memory_limit=memory_limit)
        with self._mu:
            self._instances[view["key"]] = inst
        return inst

    async def _wait_ready(self, inst: Instance, view: dict) -> None:
        """Wait until the instance answers its health path (2xx), or accepts connections when it declares none."""
        import httpx

        health = view["app"].get("health")
        prefix = "" if view["app"]["strip_path"] else base_path(view["name"], view["major"])
        url = f"http://127.0.0.1:{inst.port}{prefix}{health}" if health else None
        deadline = time.monotonic() + self.start_timeout
        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.monotonic() < deadline:
                if inst.proc.poll() is not None:
                    await asyncio.to_thread(self._stop, inst, "start_failed")
                    self._exited(view, "start_failed", inst.proc.returncode)
                    raise AppUnavailable(503, "the App exited while starting; see `duckstring app logs`")
                if await self._answers(client, inst.port, url):
                    inst.ready = True
                    inst.last_used = time.monotonic()
                    with locked(self.driver.db):
                        self.driver.db.execute("UPDATE app_instance SET ready_at = ? WHERE id = ?",
                                               (_now_iso(), inst.id))
                        self.driver.db.commit()
                    return
                await asyncio.sleep(0.1)
        await asyncio.to_thread(self._stop, inst, "start_failed")
        self._exited(view, "start_failed", None)
        raise AppUnavailable(503, f"the App didn't become ready within {self.start_timeout:.0f}s; "
                                  "see `duckstring app logs`")

    @staticmethod
    async def _answers(client, port: int, url: str | None) -> bool:
        try:
            if url is None:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                return True
            r = await client.get(url)
            return 200 <= r.status_code < 300
        except (OSError, Exception):  # not listening yet, or not answering yet
            return False

    def _exited(self, view: dict, reason: str, exit_code: int | None) -> None:
        """An instance ended on its own: fail the App once its restarts are spent."""
        key = view["key"]
        self._crashes[key] = self._crashes.get(key, 0) + 1
        if self._crashes[key] > view["immediate_retries"]:
            self.driver.fail_app(key, app_stop_message(reason, exit_code))

    # ─── stopping ───────────────────────────────────────────────────────────

    def _stop(self, inst: Instance, reason: str, process: bool = True) -> None:
        """End ``inst`` (SIGTERM to its process group, then SIGKILL after a grace), record it and give its
        memory back to the slots. ``process=False`` when the Driver called in (a Kill or a removal) and processes the
        engine itself."""
        inst.stopping = True
        proc = inst.proc
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                proc.wait()
        with self._mu:
            if self._instances.get(inst.key) is inst:
                del self._instances[inst.key]
            inst.leases.clear()
        shutil.rmtree(inst.log_path.parent.parent / "holds", ignore_errors=True)
        self.driver.app_release(inst.key, process=process)
        db = self.driver.db
        with locked(db):
            db.execute("UPDATE app_instance SET stopped_at = ?, stop_reason = ?, exit_code = ? "
                       "WHERE id = ? AND stopped_at IS NULL", (_now_iso(), reason, proc.returncode, inst.id))
            db.commit()

    def stop_now(self, key: str, reason: str, process: bool = False) -> None:
        """Stop ``key``'s instance at once: the Driver's hook for a Kill or a removal (``process=False``, as the
        Driver processes the engine itself), and a same-version redeploy's, before its directory is replaced."""
        with self._mu:
            inst = self._instances.get(key)
        if inst is not None:
            self._stop(inst, reason, process=process)
        self._crashes.pop(key, None)

    async def restart(self, key: str) -> None:
        """Stop ``key``'s instance and start a fresh one. The Driver has already cleared a failure or a Kill.
        A start that fails counts like any other."""
        await asyncio.to_thread(self.stop_now, key, "restart", True)
        try:
            await self.acquire(key)
        except AppUnavailable as exc:
            log.warning("restarting %s: %s", key, exc.message)
        except Exception:
            log.exception("restarting %s", key)

    def shutdown(self) -> None:
        with self._mu:
            instances = list(self._instances.values())
        for inst in instances:
            self._stop(inst, "shutdown")

    # ─── the loop ───────────────────────────────────────────────────────────

    def reconcile(self) -> None:
        """One pass: record instances that exited on their own, stop idle and outdated ones, and refresh
        each running App's freshness file."""
        with self._mu:
            instances = list(self._instances.values())
        now = time.monotonic()
        for inst in instances:
            if inst.stopping or not inst.ready:
                continue  # a start in progress is _wait_ready's to judge
            view = self.driver.app_view(inst.key)
            if inst.proc.poll() is not None:
                self._stop(inst, "exited")
                if view is not None:
                    self._exited(view, "exited", inst.proc.returncode)
                continue
            if view is None:
                self._stop(inst, "removed")
            elif view["is_killed"]:
                self._stop(inst, "killed")
            elif not _current(inst, view):
                self._stop(inst, "redeploy")
                self._crashes.pop(inst.key, None)
            elif (view["app"]["idle_timeout_s"] > 0 and inst.active <= 0
                  and now - inst.last_used >= view["app"]["idle_timeout_s"]):
                self._stop(inst, "idle")
                self._crashes.pop(inst.key, None)
            else:
                self._write_freshness(inst.key, app_runtime_dir(self.root, view["name"], view["major"]))

    def _write_freshness(self, key: str, runtime: Path, force: bool = False) -> None:
        fresh = self.driver.app_freshness(key)
        if not force and self._freshness.get(key) == fresh:
            return
        runtime.mkdir(parents=True, exist_ok=True)
        tmp = runtime / "freshness.json.tmp"
        tmp.write_text(json.dumps(fresh))
        tmp.replace(runtime / "freshness.json")
        self._freshness[key] = fresh

    def close_orphans(self) -> None:
        """At startup: an instance still open in the database belongs to a previous Catchment process, which
        took it down with it (or left it running, outside our control). Record it as shut down."""
        db = self.driver.db
        with locked(db):
            for iid, pid in db.execute("SELECT id, pid FROM app_instance WHERE stopped_at IS NULL").fetchall():
                if pid:
                    try:
                        os.killpg(pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                db.execute("UPDATE app_instance SET stopped_at = ?, stop_reason = 'shutdown' WHERE id = ?",
                           (_now_iso(), iid))
            db.commit()


async def run_app_supervisor(supervisor: AppSupervisor, interval: float = 1.0) -> None:
    """The supervisor's loop: :meth:`AppSupervisor.reconcile` every ``interval`` seconds."""
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(supervisor.reconcile)
        except Exception as exc:  # keep the loop alive
            print(f"[catchment] app supervisor error: {exc}", flush=True)


def instance_rows(db, version_ids: list[int], limit: int = 20) -> list[dict]:
    """Recent instances of the given versions, newest first, for the CLI and API."""
    if not version_ids:
        return []
    marks = ",".join("?" * len(version_ids))
    with locked(db):
        rows = db.execute(
            f"SELECT ai.id, pv.version, ai.started_at, ai.ready_at, ai.stopped_at, ai.stop_reason, ai.exit_code, "
            f"ai.log_path FROM app_instance ai JOIN pond_version pv ON pv.id = ai.pond_version_id "
            f"WHERE ai.pond_version_id IN ({marks}) ORDER BY ai.id DESC LIMIT ?", (*version_ids, limit)).fetchall()
    return [{"id": r[0], "version": r[1], "started_at": r[2], "ready_at": r[3], "stopped_at": r[4],
             "stop_reason": r[5], "exit_code": r[6], "log_path": r[7]} for r in rows]


# ─── downloads ────────────────────────────────────────────────────────────────
#
# What an App keeps is its own business, so the Catchment never copies an App's files on its own: its state
# directory and runtime directory stay out of `catchment download` and the state backup. An App that wants
# its state carried declares `[app] download`, a command that writes into `$DUCKSTRING_APP_DOWNLOAD` what its
# state directory should hold after a restore (a consistent copy of a database, an export, without secrets).

DOWNLOAD_TIMEOUT_S = 600.0


class AppDownloadError(Exception):
    """An App's ``download`` command failed or timed out; the message names the App and ends with its output."""


def is_app_file(rel: tuple[str, ...]) -> bool:
    """Whether a root-relative path (as parts) belongs to an App: its state (``apps/...``) or its runtime
    directory (``ponds/{name}/m{major}/app/...``: logs, holds, the freshness file)."""
    if rel[:1] == ("apps",):
        return True
    return len(rel) > 3 and rel[0] == "ponds" and rel[2].startswith("m") and rel[3] == "app"


def run_download(root: Path, view: dict, out: Path, timeout: float | None = None) -> None:
    """Run an App's ``download`` command in its environment, writing into ``out``. Raises
    :class:`AppDownloadError`."""
    from ..environments import python_for

    name, major = view["name"], view["major"]
    python = python_for(root, view["source_path"])
    state = app_state_dir(root, name, major)
    state.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in _WITHHELD_ENV}
    env["PATH"] = str(Path(python).parent) + os.pathsep + env.get("PATH", "")
    env["DUCKSTRING_APP_STATE"] = str(state)
    env["DUCKSTRING_APP_DOWNLOAD"] = str(out)
    try:
        done = subprocess.run(
            ["/bin/sh", "-c", "exec " + view["app"]["download"]], cwd=root / view["source_path"], env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout or DOWNLOAD_TIMEOUT_S, start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        tail = (exc.stdout or b"").decode(errors="replace")[-2000:]
        raise AppDownloadError(f"App '{name}' (v{major}): its download command didn't finish within "
                               f"{timeout or DOWNLOAD_TIMEOUT_S:.0f}s\n{tail}") from None
    if done.returncode != 0:
        tail = done.stdout.decode(errors="replace")[-2000:]
        raise AppDownloadError(f"App '{name}' (v{major}): its download command exited with code "
                               f"{done.returncode}\n{tail}")


def snapshot_apps(driver, root: Path) -> tuple[Path | None, list[tuple[Path, str]]]:
    """Run every deployed App's ``download`` command into a fresh temporary directory, and return it (the
    caller removes it) with the files to archive, as ``(path, arcname)`` under ``apps/{name}/m{major}/state/``.
    Raises :class:`AppDownloadError` on the first failure, having removed the directory."""
    import tempfile

    views = [v for v in (driver.app_view(k) for k in sorted(driver.app_keys())) if v and v["app"].get("download")]
    if not views:
        return None, []
    tmp = Path(tempfile.mkdtemp(prefix="duckstring-app-download-"))
    files: list[tuple[Path, str]] = []
    try:
        for view in views:
            out = tmp / view["name"] / f"m{view['major']}"
            out.mkdir(parents=True)
            run_download(Path(root), view, out)
            arc = f"apps/{view['name']}/m{view['major']}/state"
            for path in sorted(out.rglob("*")):
                if path.is_file() or path.is_symlink():
                    files.append((path, f"{arc}/{path.relative_to(out).as_posix()}"))
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return tmp, files


def version_ids_of(db, name: str, major: int) -> list[int]:
    with locked(db):
        return [r[0] for r in db.execute(
            "SELECT pv.id FROM pond_version pv JOIN pond_name pn ON pn.id = pv.pond_name_id "
            "WHERE pn.name = ? AND pv.major = ? AND pv.app IS NOT NULL", (name, major)).fetchall()]


