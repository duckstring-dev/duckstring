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
from ..keys import instance_key
from .db import locked
from .registry import pond_major_dir
from .tunnels import TunnelHub

log = logging.getLogger(__name__)

DEFAULT_START_TIMEOUT_S = 60.0
STOP_GRACE_S = 10.0
WS_LEASE_GRACE_S = 60.0  # how long a WebSocket keeps the lease of its previous message
CONNECTION_RESERVE = 10  # PostgreSQL connections kept for Pond readers, Spouts and the Catchment itself
DRAIN_TIMEOUT_S = 600.0  # a draining instance whose sockets stay open stops after this
LOAD_STALE_S = 30.0  # an App's reported load counts this long
# An instance on another machine (plans/app-scaling.md): the machine boots and builds the App's environment first.
DEFAULT_REMOTE_START_TIMEOUT_S = 900.0
REMOTE_SILENCE_S = 60.0  # without its agent this long, a remote instance is taken for gone
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


class RemoteProc:
    """Stands in for the process of an instance on another machine: its agent reports the exit code."""

    pid = None

    def __init__(self):
        self.returncode: int | None = None
        self.gone = False  # ended without a code (its machine went, or its agent fell silent)

    def poll(self) -> int | None:
        return -1 if self.gone and self.returncode is None else self.returncode


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
    requests: int = 0  # HTTP requests in flight (plans/app-scaling.md)
    sockets: dict[int, float] = field(default_factory=dict)  # open WebSockets: ticket -> last message (monotonic)
    draining: bool = False  # takes no new sessions; stops once its work is done
    drain_since: float = 0.0
    started: float = field(default_factory=time.monotonic)
    start_reason: str = ""
    cpu: float | None = None  # share of its cores, from the last sample
    memory: float | None = None  # share of its memory limit
    load: tuple[float, float] | None = None  # (the App's reported load, when)
    ready_event: asyncio.Event | None = None  # set once ready, or once its start failed
    last_used: float = field(default_factory=time.monotonic)
    stopping: bool = False
    # Read leases per request or WebSocket in flight: (monotonic time, {source key: changed_f}), oldest first.
    leases: dict[int, list[tuple[float, dict]]] = field(default_factory=dict)
    next_ticket: int = 0
    memory_limit: str | None = None
    samplers: dict = field(default_factory=dict)  # pid -> psutil.Process, for CPU between samples
    reserve_key: str = ""  # where its memory is set aside (Driver.app_reserved): the App's key, or one per instance
    # On another machine (plans/app-scaling.md): its launcher record, its tunnel, what its agent reported.
    where: str = "catchment"
    launch_key: str = ""
    session: object = None
    config: dict | None = None
    holds: dict = field(default_factory=dict)  # Source name -> versions its process holds (pond.hold())
    sent_freshness: dict | None = None
    error: str | None = None
    remote_ready: bool = False
    adopted: bool = False  # left running by a previous Catchment process: no launcher record, judged by contact

    @property
    def remote(self) -> bool:
        return self.where == "remote"


def _scaling_env(view: dict, instance: int) -> dict[str, str]:
    """What an instance's process learns of its scaling (plans/app-scaling.md): its id (for
    ``duckstring.app.report_load``), and the concurrency and PostgreSQL connections it is sized for."""
    sc = view["scaling"]
    return {"DUCKSTRING_APP_INSTANCE": str(instance), "DUCKSTRING_APP_CONCURRENCY": str(sc["concurrency"]),
            "DUCKSTRING_APP_CONNECTIONS": str(sc["connections"])}


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
        self._instances: dict[str, list[Instance]] = {}  # an App's instances ([app] instances, on this machine)
        self._growing: set[str] = set()  # Apps starting another instance in the background
        self._kicked: set[str] = set()  # Apps with a scaling pass scheduled
        self._grow_after: dict[str, float] = {}  # App key -> when a failed growth may be tried again
        self._decisions: dict[str, list[dict]] = {}  # App key -> recent scaling decisions, newest last
        self.pressed: dict[str, str] = {}  # App key -> the pressure it is held under at its maximum
        from .app_scaling import DefaultScaler, load_scaler

        self.scaler = load_scaler()
        self._fallback_scaler = DefaultScaler()
        self._slots = 0  # names each extra instance's memory slot
        self._starts: dict[str, asyncio.Lock] = {}
        self._crashes: dict[str, int] = {}  # consecutive instances that exited on their own or never got ready
        self._freshness: dict[str, dict] = {}  # last freshness written per App, to write only on change
        self._mu = threading.Lock()
        # Set when the Catchment begins to shut down, before anything stops an instance: its own stops are then
        # never mistaken for an App exiting on its own (see shutdown).
        self.closing = False
        self.start_timeout = float(os.environ.get("DUCKSTRING_APP_START_TIMEOUT", DEFAULT_START_TIMEOUT_S))
        self.remote_start_timeout = float(os.environ.get("DUCKSTRING_APP_REMOTE_START_TIMEOUT",
                                                         DEFAULT_REMOTE_START_TIMEOUT_S))
        self.tunnels = TunnelHub()
        # Instances elsewhere a previous Catchment process left running, by id, until their agents dial in again.
        self._orphans: dict[int, dict] = {}
        self.loop: asyncio.AbstractEventLoop | None = None  # the server's loop, where the tunnels live
        driver.on_app_stop = self.stop_now
        driver.app_leases = self.leases
        driver.app_activity = self.activity
        driver.app_metrics = self.metrics

    # ─── serving a request ──────────────────────────────────────────────────

    async def acquire(self, key: str, prefer: int | None = None) -> Instance:
        """A ready instance of the App ``key``, started if needed (plans/app-scaling.md): ``prefer`` (an instance id,
        from the browser's cookie) while it serves, else the busiest with room, instances elsewhere first. With none
        ready, an App on its own compute with ``min_instances = 0`` starts one here (the Catchment is the fallback);
        with a warm floor it waits for its own. Growth beyond is the scaler's (:meth:`scale_app`). Raises
        :class:`AppUnavailable`."""
        self.loop = asyncio.get_running_loop()
        view = self.driver.app_view(key)
        if view is None:
            raise AppUnavailable(404, "no such App")
        self._check_serving(key, view)
        inst = self._pick(key, view, prefer)
        if inst is not None:
            self._kick(key)
            return inst
        lock = self._starts.setdefault(key, asyncio.Lock())
        async with lock:
            view = self.driver.app_view(key)
            if view is None:
                raise AppUnavailable(404, "no such App")
            self._check_serving(key, view)
            inst = self._pick(key, view, prefer)
            if inst is not None:
                return inst
            for old in self._of(key):
                if old.proc.poll() is not None and not old.stopping:  # it died: count it
                    await asyncio.to_thread(self._stop, old, "exited")
                    self._exited(view, "exited", old.proc.returncode)
                    view = self.driver.app_view(key)
                    if view is None:
                        raise AppUnavailable(404, "no such App")
                    self._check_serving(key, view)
                elif not old.stopping and not _current(old, view):  # an older deploy's instance
                    await asyncio.to_thread(self._stop, old, "redeploy")
            if not view["app"]["command"]:
                raise AppUnavailable(404, "not found")  # a static-only App: nothing to start
            duck = self._placement(view, request=True)
            starting = [i for i in self._of(key) if not i.ready and not i.stopping and i.ready_event is not None
                        and (duck is not None or not i.remote)]
            if starting:  # one is on its way (the warm floor, the scaler): wait for it rather than start another
                await self._await_ready(starting)
                inst = self._pick(key, view, prefer)
                if inst is not None:
                    return inst
            return await self._start(key, view, duck, "a request")

    async def _await_ready(self, starting: list[Instance]) -> None:
        waits = [asyncio.ensure_future(i.ready_event.wait()) for i in starting]
        try:
            await asyncio.wait(waits, timeout=max(self.remote_start_timeout, self.start_timeout),
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waits:
                w.cancel()

    def _placement(self, view: dict, request: bool = False) -> dict | None:
        """Where the next instance starts: the App's compute elsewhere (its ``duck`` config), or ``None`` for this
        machine. A request finding nothing ready starts here when the App keeps no warm floor."""
        duck = self._remote_duck(view)
        if duck is not None and request and view["scaling"]["min_instances"] == 0:
            return None
        return duck

    async def _start(self, key: str, view: dict, duck: dict | None = None, reason: str = "") -> Instance:
        """Start one more instance of ``key`` and wait for it to be ready: on ``duck``'s compute, else here."""
        if duck is not None:
            return await self._start_remote(key, view, duck, reason)
        slot = key if not self._of(key) else f"{key}#{self._next_slot()}"
        try:
            memory_limit = self.driver.app_reserve(key, slot)
        except ValueError as e:
            raise AppUnavailable(503, str(e)) from None
        try:
            inst = await asyncio.to_thread(self._spawn, view, memory_limit, slot, reason)
        except BaseException:
            self.driver.app_release(slot)
            raise
        await self._wait_ready(inst, view)
        return inst

    # ─── scaling (plans/app-scaling.md) ─────────────────────────────────────

    def _kick(self, key: str) -> None:
        """A scaling pass for ``key`` soon (after a request), besides the loop's."""
        if key in self._kicked or self.closing:
            return
        self._kicked.add(key)

        async def run() -> None:
            try:
                await self.scale_app(key)
            except Exception:
                log.exception("scaling %s", key)
            finally:
                self._kicked.discard(key)

        asyncio.ensure_future(run())

    def _active(self, inst: Instance, sc: dict, now: float | None = None) -> int:
        """Active work: HTTP requests in flight and WebSockets with a message within ``socket_idle``."""
        now = time.monotonic() if now is None else now
        idle = sc["scale"]["socket_idle_s"]
        return inst.requests + sum(1 for t in list(inst.sockets.values()) if now - t <= idle)

    def _instance_view(self, inst: Instance, sc: dict, now: float):
        from .app_scaling import InstanceView

        load = inst.load[0] if inst.load and now - inst.load[1] <= LOAD_STALE_S else None
        return InstanceView(
            id=inst.id, where=inst.where, ready=inst.ready, starting=not inst.ready, draining=inst.draining,
            requests=inst.requests, active_sockets=self._active(inst, sc, now) - inst.requests,
            open_sockets=len(inst.sockets), cpu=inst.cpu, memory=inst.memory, load=load,
            age_s=now - inst.started, idle_s=0.0 if inst.active > 0 else now - inst.last_used)

    def app_view_for_scaler(self, key: str, view: dict):
        from .app_scaling import AppView

        sc = view["scaling"]
        now = time.monotonic()
        insts = [i for i in self._of(key) if not i.stopping and _current(i, view) and i.proc.poll() is None]
        return AppView(key=key, now=now, min_instances=sc["min_instances"], max_instances=sc["instances"],
                       concurrency=sc["concurrency"], thresholds=dict(sc["scale"]), hold=sc.get("hold"),
                       instances=[self._instance_view(i, sc, now) for i in insts],
                       decisions=list(self._decisions.get(key, ())))

    async def scale_app(self, key: str) -> None:
        """One scaling pass for the App ``key``: ask the scaler, then start one instance (never more at once) or mark
        instances to drain, within the settings, the warm floor and the connection budget."""
        view = self.driver.app_view(key)
        if (view is None or self.closing or view["is_killed"] or view["is_failed"] or not view["app"]["command"]
                or self._data_blocked(view)):
            return
        sc = view["scaling"]
        app = self.app_view_for_scaler(key, view)
        insts = [i for i in self._of(key) if not i.stopping and _current(i, view) and i.proc.poll() is None]
        live = [i for i in insts if not i.draining]
        hold = sc.get("hold")
        if hold:  # the operator's count: the scaler isn't asked
            from .app_scaling import Decision

            count = max(0, min(sc["instances"], int(hold["count"])))
            # Drained first: the Catchment's (routing empties it first too), then the least busy, the newest.
            extra = sorted(live, key=lambda i: (i.remote, self._active(i, sc), -i.id))[:max(0, len(live) - count)]
            decision = Decision(desired=count, drain=[i.id for i in extra], reason="the operator's count")
            if hold.get("until") is None and len(live) == count and all(i.ready for i in live):
                await asyncio.to_thread(self.driver.set_app_hold, key, None)  # reached: scaling resumes
            sc = {**sc, "min_instances": min(count, sc["min_instances"])}
        else:
            try:
                decision = self.scaler.decide(app)
            except Exception:
                log.exception("the App scaler failed for %s: the default rule decides", key)
                decision = self._fallback_scaler.decide(app)
        for iid in decision.drain or ():  # never below the warm floor
            inst = next((i for i in live if i.id == iid), None)
            if inst is not None and len(live) > sc["min_instances"]:
                inst.draining, inst.drain_since = True, time.monotonic()
                live.remove(inst)
                self._decide(key, len(live), f"draining #{inst.id}: {decision.reason or 'the scaler'}")
        desired = max(sc["min_instances"], min(sc["instances"], int(decision.desired)))
        if decision.desired > sc["instances"] and decision.reason:
            self.pressed[key] = decision.reason
        elif len(live) < sc["instances"] or not decision.reason:
            self.pressed.pop(key, None)
        if len(live) >= desired or any(not i.ready for i in live) or key in self._growing:
            return
        if time.monotonic() < self._grow_after.get(key, 0.0):
            return
        why = decision.reason if len(live) >= sc["min_instances"] else "the warm floor (min_instances)"
        if not self._connections_allow(view):
            self.pressed[key] = f"{why}; the PostgreSQL connection budget is spent"
            return
        self._growing.add(key)
        try:
            self._decide(key, len(live) + 1, why)
            await self._start(key, view, self._placement(view), why)
        except AppUnavailable as e:
            self._grow_after[key] = time.monotonic() + 60.0
            log.warning("another instance of %s didn't start: %s", key, e.message)
        finally:
            self._growing.discard(key)

    def _data_blocked(self, view: dict) -> bool:
        """An upgrade's cutover or a move of Records or Media is under way: nothing new starts meanwhile."""
        lifecycle = getattr(self.driver, "lifecycle", None)
        if lifecycle is not None and lifecycle.cutting_over(view["name"]):
            return True
        moves = getattr(self.driver, "moves", None)
        return moves is not None and moves.blocks(view["app"])

    def _decide(self, key: str, count: int, reason: str) -> None:
        """Record a scaling decision: the App's recent ones, the log, and an ``app_scale`` alert for channels that
        ask for it (outside ``all``)."""
        at = _now_iso()
        entries = self._decisions.setdefault(key, [])
        entries.append({"at": at, "count": count, "reason": reason})
        del entries[:-20]
        log.info("%s: %d instance(s): %s", key, count, reason)
        name, _, major = key.rpartition("@")
        try:
            with locked(self.driver.db):
                self.driver._emit_alert("app_scale", scope_pond=name, scope_major=int(major), severity="info",
                                        title=f"{name}@{major}: {count} instance(s)", message=reason, f=at,
                                        detail={"instances": count})
        except Exception:  # pragma: no cover - alerting never stops scaling
            pass

    def _connections_allow(self, view: dict) -> bool:
        """Whether one more instance of this App fits the Records PostgreSQL's connections (plans/app-scaling.md)."""
        if not view["app"].get("records"):
            return True
        records = getattr(self.driver, "records", None)
        if records is None:
            return True
        try:
            total = records.max_connections()
        except Exception:
            return True
        used = 0
        for key, insts in list(self._instances.items()):
            other = self.driver.app_view(key)
            if other is None or not other["app"].get("records"):
                continue
            used += sum(1 for i in insts if not i.stopping) * other["scaling"]["connections"]
        return used + view["scaling"]["connections"] <= total - CONNECTION_RESERVE

    def drain(self, key: str, instance: int) -> bool:
        """Stop sending new sessions to one instance; it stops once its work is done (``app drain``)."""
        view = self.driver.app_view(key)
        for inst in self._of(key):
            if inst.id == instance and not inst.stopping:
                if not inst.draining:
                    inst.draining, inst.drain_since = True, time.monotonic()
                    if view is not None:
                        self._decide(key, self._live_count(key, view), f"draining #{instance}: the operator")
                return True
        return False

    def scale_status(self, key: str) -> dict:
        """For ``app scale`` and the App panel: the effective settings and their origins, the running instances with
        what the scaler sees, recent decisions, and the pressure the App is held under at its maximum."""
        view = self.driver.app_view(key)
        if view is None:
            return {}
        sc = view["scaling"]
        now = time.monotonic()
        insts = [i for i in self._of(key) if not i.stopping]
        rows = []
        for inst in insts:
            v = self._instance_view(inst, sc, now)
            rows.append({"id": v.id, "where": v.where, "ready": v.ready, "draining": v.draining,
                         "requests": v.requests, "active_sockets": v.active_sockets, "open_sockets": v.open_sockets,
                         "active": v.active, "cpu": v.cpu, "memory": v.memory, "load": v.load,
                         "age_s": round(v.age_s, 1), "start_reason": inst.start_reason or None})
        return {"settings": {k: sc[k] for k in ("instances", "min_instances", "concurrency", "connections", "scale")},
                "origin": sc["origin"], "hold": sc.get("hold"), "compute": "elsewhere" if self._remote_duck(view)
                else "catchment", "instances": rows, "decisions": list(self._decisions.get(key, ())),
                "pressed": self.pressed.get(key)}

    def report_load(self, inst: Instance, value: float) -> None:
        """The App's own load (0 to 1), from its ``X-Duckstring-Load`` header or ``duckstring.app.report_load``."""
        inst.load = (max(0.0, min(1.0, float(value))), time.monotonic())

    def _sample(self, inst: Instance, view: dict) -> None:
        """CPU and memory of a local instance's process tree, and the load it reported by file."""
        try:
            import psutil
        except ImportError:  # pragma: no cover - psutil is a dependency
            return
        try:
            root = inst.samplers.get(inst.proc.pid) or psutil.Process(inst.proc.pid)
            procs = [root, *root.children(recursive=True)]
            cpu, rss, seen = 0.0, 0, {}
            for proc in procs:
                proc = inst.samplers.get(proc.pid, proc)
                seen[proc.pid] = proc
                cpu += proc.cpu_percent(None)
                rss += proc.memory_info().rss
            inst.samplers = seen
            inst.cpu = cpu / 100.0 / (psutil.cpu_count() or 1)
            limit = view["app"].get("memory")
            inst.memory = rss / limit if limit else None
        except (psutil.Error, OSError):
            pass
        path = inst.log_path.parent.parent / "load" / str(inst.id)
        try:
            value = float(path.read_text().strip())
            when = path.stat().st_mtime
            inst.load = (max(0.0, min(1.0, value)), time.monotonic() - max(0.0, time.time() - when))
        except (OSError, ValueError):
            pass

    def _next_slot(self) -> int:
        with self._mu:
            self._slots += 1
            return self._slots

    def _of(self, key: str) -> list[Instance]:
        with self._mu:
            return list(self._instances.get(key, ()))

    def _all(self) -> list[Instance]:
        with self._mu:
            return [i for insts in self._instances.values() for i in insts]

    def _check_serving(self, key: str, view: dict) -> None:
        if view["is_killed"]:
            raise AppUnavailable(503, "the App was killed; an operator can wake it with `duckstring control wake`")
        if view["is_failed"]:
            raise AppUnavailable(503, "the App failed; see `duckstring app logs`, then wake it with "
                                      "`duckstring control wake`")
        # Not failed: any earlier run of exits was cleared (by a Wake, a Clear or a redeploy).
        if self._crashes.get(key, 0) > view["immediate_retries"]:
            self._crashes[key] = 0

    def _pick(self, key: str, view: dict, prefer: int | None = None) -> Instance | None:
        """Packed routing (plans/app-scaling.md): the cookie's instance while it serves, else the busiest with room
        (under ``concurrency`` and the memory threshold), instances elsewhere before this machine's, so surplus ones
        empty and idle out; with no room anywhere, the least busy."""
        ready = [i for i in self._of(key)
                 if i.ready and not i.stopping and not i.draining and i.proc.poll() is None and _current(i, view)]
        if not ready:
            return None
        for inst in ready:
            if inst.id == prefer:
                return inst
        sc = view["scaling"]
        now = time.monotonic()
        room = [i for i in ready if self._active(i, sc, now) < sc["concurrency"]
                and (i.memory is None or i.memory < sc["scale"]["memory"])]
        if room:
            return max(room, key=lambda i: (i.remote, self._active(i, sc, now), -i.id))
        return min(ready, key=lambda i: (self._active(i, sc, now), i.id))

    def begin(self, inst: Instance, socket: bool = False) -> int:
        """A request or WebSocket connection starts: it keeps the instance from idling and takes a read lease.
        Returns its ticket for :meth:`renew` and :meth:`end`."""
        lease = self.driver.app_sources_now(inst.key, persisted=inst.remote)
        now = time.monotonic()
        with self._mu:
            inst.active += 1
            inst.last_used = now
            inst.next_ticket += 1
            inst.leases[inst.next_ticket] = [(now, lease)]
            if socket:
                inst.sockets[inst.next_ticket] = now
            else:
                inst.requests += 1
            return inst.next_ticket

    def touch(self, inst: Instance, ticket: int) -> None:
        """A message on a WebSocket, either way: it is active (plans/app-scaling.md)."""
        now = time.monotonic()
        inst.last_used = now
        if ticket in inst.sockets:
            inst.sockets[ticket] = now

    def renew(self, inst: Instance, ticket: int) -> None:
        """A WebSocket message from the browser: lease the Sources' versions now, keep the previous lease for
        ``WS_LEASE_GRACE_S`` (the work it started may still be reading), and drop older ones."""
        lease = self.driver.app_sources_now(inst.key, persisted=inst.remote)
        now = time.monotonic()
        with self._mu:
            inst.last_used = now
            if ticket in inst.sockets:
                inst.sockets[ticket] = now
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
            if inst.sockets.pop(ticket, None) is None:
                inst.requests -= 1

    def leases(self, key: str, source_key: str, source_name: str) -> list[datetime]:
        """The versions of the Source line ``source_key`` the App ``key`` is reading: the leases of its requests
        and WebSockets in flight, and its explicit holds (``pond.hold()``). For ``Driver._retain_from``."""
        with self._mu:
            insts = list(self._instances.get(key, ()))
            if not insts:
                return []
            out = [lease[source_key] for inst in insts for entries in inst.leases.values() for _, lease in entries
                   if source_key in lease]
            for inst in insts:  # holds an agent reported (an instance on another machine)
                for f in inst.holds.get(source_name, ()):
                    try:
                        out.append(datetime.fromisoformat(f))
                    except (TypeError, ValueError):
                        pass
            holds = insts[0].log_path.parent.parent / "holds"
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
            insts = [i for i in self._instances.get(key, ()) if not i.stopping]
            if not insts:
                return None
            active, last_used = sum(max(i.active, 0) for i in insts), max(i.last_used for i in insts)
            elsewhere = sum(1 for i in insts if i.remote)
        idle_at = None
        if idle_timeout_s > 0 and active <= 0:
            left = max(0.0, idle_timeout_s - (time.monotonic() - last_used))
            idle_at = (datetime.now(timezone.utc) + timedelta(seconds=left)).isoformat()
        return {"active": active, "idle_at": idle_at, "count": len(insts), "elsewhere": elsewhere,
                "pressed": self.pressed.get(key)}

    def metrics(self) -> list[dict]:
        """For ``/metrics``: per App line, its instances by place, active work, mean CPU, memory and reported load,
        and whether it is held at its maximum under pressure."""
        out = []
        now = time.monotonic()
        for key, insts in list(self._instances.items()):
            view = self.driver.app_view(key)
            if view is None:
                continue
            live = [i for i in insts if not i.stopping]
            sc = view["scaling"]

            def mean(values):
                values = [v for v in values if v is not None]
                return sum(values) / len(values) if values else None

            out.append({"name": view["name"], "major": view["major"],
                        "catchment": sum(1 for i in live if not i.remote), "elsewhere": sum(1 for i in live if i.remote),
                        "active": sum(self._active(i, sc, now) for i in live),
                        "cpu": mean(i.cpu for i in live), "memory": mean(i.memory for i in live),
                        "load": mean(i.load[0] for i in live if i.load and now - i.load[1] <= LOAD_STALE_S),
                        "at_maximum": key in self.pressed})
        return out

    # ─── starting ───────────────────────────────────────────────────────────

    def _spawn(self, view: dict, memory_limit: str | None = None, slot: str | None = None,
               reason: str = "") -> Instance:
        from ..environments import ensure_env

        name, major = view["name"], view["major"]
        cfg = view["app"]
        data_env = self._app_data_env(view)  # before the instance row: a failure here starts nothing
        runtime = app_runtime_dir(self.root, name, major)
        (runtime / "logs").mkdir(parents=True, exist_ok=True)
        port = _free_port()
        db = self.driver.db
        with locked(db):
            cur = db.execute(
                "INSERT INTO app_instance (pond_version_id, started_at, port, log_path, start_reason) "
                "VALUES (?, ?, ?, '', ?)", (view["version_id"], _now_iso(), port, reason or None))
            iid = cur.lastrowid
            log_path = runtime / "logs" / f"{iid}.log"
            db.execute("UPDATE app_instance SET log_path = ? WHERE id = ?",
                       (str(log_path.relative_to(self.root)), iid))
            db.commit()

        # Its environment for this Catchment's Duckstring, built now when the Catchment's has changed since
        # (an App has no Duck to switch itself, as a Pond's does).
        python = ensure_env(Path(self.root), Path(self.root) / view["source_path"], check_lock=False)
        env = {k: v for k, v in os.environ.items() if k not in _WITHHELD_ENV}
        env["PATH"] = str(Path(python).parent) + os.pathsep + env.get("PATH", "")
        env["PORT"] = str(port)
        env["DUCKSTRING_APP_PATH"] = base_path(name, major)
        env.update(_scaling_env(view, iid))
        env["PYTHONUNBUFFERED"] = "1"
        if cfg["state"] == "local":
            state = app_state_dir(self.root, name, major)
            state.mkdir(parents=True, exist_ok=True)
            env["DUCKSTRING_APP_STATE"] = str(state)
        env.update(data_env)
        env[ENV] = json.dumps({
            "name": name, "major": major, "version": view["version"], "root": str(self.root),
            "data_root": self.data_root, "sources": list(view["sources"]), "source_majors": view["sources"],
            "runtime": str(runtime), "memory_limit": memory_limit,
        })
        if not any(not i.stopping for i in self._of(view["key"])):
            shutil.rmtree(runtime / "holds", ignore_errors=True)  # a previous instance's (none runs beside it)
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
                        proc=proc, port=port, log_path=log_path, memory_limit=memory_limit,
                        reserve_key=slot or view["key"], start_reason=reason, ready_event=asyncio.Event())
        with self._mu:
            self._instances.setdefault(view["key"], []).append(inst)
        return inst

    # ─── instances on other machines (plans/app-scaling.md) ──────────────────

    def _remote_duck(self, view: dict) -> dict | None:
        """The compute an instance of this App starts on when that isn't this machine, else ``None``."""
        on_here = getattr(self.driver.launcher, "on_this_machine", None)
        if on_here is None:
            return None
        try:
            duck = self.driver.duck_config(view["key"])
        except Exception:  # pragma: no cover - an App with no compute config runs here
            return None
        if not duck.get("remote"):  # no compute declared: this machine
            return None
        return None if on_here(duck) else duck

    def _remote_data_env(self, view: dict) -> tuple[dict[str, str], str | None]:
        """The line's Records and Media as an instance elsewhere reaches them, directly: Records at their reachable
        address over verified TLS (with the self-signed certificate to trust, when it is one), Media in an object
        store. Raises :class:`AppUnavailable` naming what to change when it can't."""
        from ..app import RECORDS_ENV
        from .records import RecordsError, line_role

        cfg = view["app"]
        if cfg["state"] != "none":
            raise AppUnavailable(503, "the App runs on other machines, so it can't keep a private state directory: "
                                      'set [app] state = "none" (Records and Media hold what it keeps)')
        if cfg.get("media") and self.driver.media.is_local:
            raise AppUnavailable(503, "the App runs on other machines, which can't reach Media on the Catchment's "
                                      "disk: move it to an object store with `duckstring catchment media move URI`")
        env = self._app_data_env(view)
        ca = None
        if cfg.get("records"):
            records = self.driver.records
            try:
                env[RECORDS_ENV] = records.url(line_role(view["name"], view["major"]), remote=True, verify=True)
                ca = records.remote_ca()
            except RecordsError as e:
                raise AppUnavailable(503, f"the App runs on other machines, which can't reach its Records: {e}") from None
        return env, ca

    def _remote_config(self, view: dict, data_env: dict[str, str], ca: str | None) -> dict:
        """What the agent needs to start the App: the environment a Catchment instance gets, minus this machine's
        paths (the agent adds its own root and runtime)."""
        cfg = view["app"]
        env = dict(data_env)
        env["DUCKSTRING_APP_PATH"] = base_path(view["name"], view["major"])
        return {
            "type": "config", "command": cfg["command"], "health": cfg.get("health"),
            "health_prefix": "" if cfg["strip_path"] else base_path(view["name"], view["major"]),
            "start_timeout": self.start_timeout, "stop_grace": STOP_GRACE_S, "env": env, "records_ca": ca,
            "freshness": self.driver.app_freshness(view["key"], persisted=True),
            "app": {"name": view["name"], "major": view["major"], "version": view["version"],
                    "data_root": self.data_root, "sources": list(view["sources"]), "source_majors": view["sources"],
                    "memory_limit": None},
        }

    async def _start_remote(self, key: str, view: dict, duck: dict, reason: str = "") -> Instance:
        """Start an instance on ``duck``'s compute through its launcher, and wait for its agent to report it ready."""
        data_env, ca = await asyncio.to_thread(self._remote_data_env, view)
        name, major = view["name"], view["major"]
        runtime = app_runtime_dir(self.root, name, major)
        (runtime / "logs").mkdir(parents=True, exist_ok=True)
        db = self.driver.db
        with locked(db):
            iid = db.execute("INSERT INTO app_instance (pond_version_id, started_at, port, log_path, place, start_reason) "
                             "VALUES (?, ?, 0, '', 'remote', ?)", (view["version_id"], _now_iso(), reason or None)
                             ).lastrowid
            log_path = runtime / "logs" / f"{iid}.log"
            db.execute("UPDATE app_instance SET log_path = ? WHERE id = ?",
                       (str(log_path.relative_to(self.root)), iid))
            db.commit()
        inst = Instance(key=key, id=iid, version_id=view["version_id"], deployed_at=view["deployed_at"],
                        proc=RemoteProc(), port=0, log_path=log_path, reserve_key=f"{key}#remote{iid}",
                        where="remote", launch_key=instance_key(name, major, iid), start_reason=reason,
                        ready_event=asyncio.Event())
        inst.config = self._remote_config(view, data_env, ca)
        inst.config["env"].update(_scaling_env(view, iid))
        session = await self.tunnels.create(iid, lambda message: self._remote_control(inst, message))
        inst.session, inst.port = session, session.port
        with open(log_path, "ab") as out:
            out.write(f"[duckstring] starting {name}@{major} {view['version']} on {duck.get('target') or 'remote'} "
                      f"compute (instance {iid})\n".encode())
        with self._mu:
            self._instances.setdefault(key, []).append(inst)
        try:
            await asyncio.to_thread(self.driver.launcher.ensure, inst.launch_key, view["version"],
                                    view["source_path"], {**duck, "app_instance": iid})
        except Exception as e:
            await asyncio.to_thread(self._stop, inst, "start_failed")
            raise AppUnavailable(503, f"the App's compute didn't start: {e}") from None
        await self._wait_remote_ready(inst, view)
        return inst

    def _remote_control(self, inst: Instance, message: dict) -> None:
        """A control message from an instance's agent (in the server's loop)."""
        kind = message.get("type")
        if kind == "hello" and inst.config is not None:
            asyncio.ensure_future(inst.session.control(inst.config))
            if message.get("state") == "running" and inst.ready:
                return  # a reconnect: it carries on
        elif kind == "ready":
            inst.remote_ready = True
        elif kind == "log":
            try:
                with open(inst.log_path, "a", encoding="utf-8") as out:
                    out.write(message.get("text") or "")
            except OSError:
                pass
        elif kind == "holds":
            inst.holds = message.get("sources") or {}
        elif kind == "metrics":
            inst.cpu, inst.memory = message.get("cpu"), message.get("memory")
            if message.get("load") is not None:
                inst.load = (float(message["load"]), time.monotonic())
        elif kind == "failed":
            inst.error = message.get("error")
        elif kind == "exited":
            code = message.get("code")
            inst.proc.returncode = int(code) if code is not None else -1

    async def _wait_remote_ready(self, inst: Instance, view: dict) -> None:
        launcher = self.driver.launcher
        deadline = time.monotonic() + self.remote_start_timeout
        while time.monotonic() < deadline:
            if inst.remote_ready:
                inst.ready = True
                inst.last_used = time.monotonic()
                inst.ready_event.set()
                with locked(self.driver.db):
                    self.driver.db.execute("UPDATE app_instance SET ready_at = ? WHERE id = ?",
                                           (_now_iso(), inst.id))
                    self.driver.db.commit()
                return
            failed = inst.error or (launcher.launch_error(inst.launch_key)
                                    if hasattr(launcher, "launch_error") else None)
            if inst.proc.poll() is not None or failed or not launcher.is_running(inst.launch_key):
                await asyncio.to_thread(self._stop, inst, "start_failed")
                self._exited(view, "start_failed", inst.proc.returncode)
                raise AppUnavailable(503, f"the App's instance elsewhere didn't start: "
                                          f"{failed or 'see `duckstring app logs`'}")
            await asyncio.sleep(0.2)
        await asyncio.to_thread(self._stop, inst, "start_failed")
        self._exited(view, "start_failed", None)
        raise AppUnavailable(503, f"the App's instance elsewhere wasn't ready within {self.remote_start_timeout:.0f}s; "
                                  "see `duckstring app logs`")

    def _in_loop(self, coro, timeout: float = 5.0):
        """Run ``coro`` on the server's loop from a worker thread (where ``_stop`` runs), and wait for it."""
        loop = self.loop
        if loop is None or loop.is_closed():
            coro.close()
            return None
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:  # on the loop itself: schedule it, never block the loop
            asyncio.ensure_future(coro)
            return None
        try:
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)
        except Exception:
            return None

    def _signal_ready(self, inst: Instance) -> None:
        event, loop = inst.ready_event, self.loop
        if event is None or event.is_set():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is None or running is loop:
            event.set()
        elif not loop.is_closed():
            loop.call_soon_threadsafe(event.set)

    def _launcher_running(self, inst: Instance) -> bool:
        try:
            return bool(self.driver.launcher.is_running(inst.launch_key))
        except Exception:
            return False

    def _stop_remote(self, inst: Instance) -> None:
        """Ask the agent to stop its App (the grace applies there), then end its machine through the launcher."""
        proc = inst.proc
        if proc.poll() is None and inst.session is not None:
            if self._in_loop(inst.session.control({"type": "stop", "grace": STOP_GRACE_S})):
                deadline = time.monotonic() + STOP_GRACE_S + 5
                while proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.1)
                # Its agent exits once it has reported: let its machine end on its own before ending it.
                deadline = time.monotonic() + 10
                while self._launcher_running(inst) and time.monotonic() < deadline:
                    time.sleep(0.1)
        try:
            self.driver.launcher.terminate(inst.launch_key)
        except Exception:
            log.exception("ending the machine of App instance %s", inst.id)
        if proc.poll() is None:
            proc.gone = True
        self._in_loop(self.tunnels.close(inst.id))

    def _app_data_env(self, view: dict) -> dict[str, str]:
        """The line's Records and Media, made ready (plans/records-and-media.md): its schema and role in the
        Catchment's PostgreSQL (started if need be) and its Media directory, recorded in ``app_data``, as the
        environment variables the App reads them through. Raises :class:`AppUnavailable` when they can't be."""
        from ..app import MEDIA_ENV, RECORDS_ENV
        from .app_lifecycle import LifecycleError
        from .media import MediaError
        from .records import RecordsError

        cfg = view["app"]
        name, major = view["name"], view["major"]
        env: dict[str, str] = {}
        if not (cfg.get("records") or cfg.get("media")):
            return env
        lifecycle = self.driver.lifecycle
        if lifecycle.cutting_over(name):
            raise AppUnavailable(503, "the App is being upgraded; try again in a moment")
        moves = getattr(self.driver, "moves", None)
        if moves is not None and moves.blocks(cfg):
            raise AppUnavailable(503, "the App's data is being moved; try again in a moment")
        try:
            state = lifecycle.prepare(view)  # a new line: live, or a trial migrated from the live one
        except LifecycleError as e:
            raise AppUnavailable(503, f"the App's data isn't ready: {e}") from None
        if state == "retired":
            raise AppUnavailable(410, "a newer major of this App took over")
        try:
            if cfg.get("records"):
                self.driver.records.provision(name, major)
                sc = view["scaling"]  # its instances' connections, and two for a migrate or the operator
                self.driver.records.set_connection_limit(name, major, sc["instances"] * sc["connections"] + 2)
                env[RECORDS_ENV] = self.driver.records.line_url(name, major)
            if cfg.get("media"):
                self.driver.media.ensure_line(name, major)
                env[MEDIA_ENV] = self.driver.media.location(name, major)
        except (RecordsError, MediaError) as e:
            raise AppUnavailable(503, f"the App's data isn't available: {e}") from None
        self.driver.note_app_data(name, major, records=bool(cfg.get("records")), media=bool(cfg.get("media")),
                                  state=state)
        return env

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
                    inst.ready_event.set()
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
        """An instance ended on its own: fail the App once its restarts are spent. Never while the Catchment is
        shutting down, when an instance's end is the Catchment's doing."""
        if self.closing:
            return
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
        self._signal_ready(inst)  # anyone waiting for it to start: it won't
        proc = inst.proc
        if inst.remote:
            self._stop_remote(inst)
        elif proc.poll() is None:
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
            insts = self._instances.get(inst.key, [])
            if inst in insts:
                insts.remove(inst)
            last = not insts
            if last:
                self._instances.pop(inst.key, None)
            inst.leases.clear()
        if last:
            shutil.rmtree(inst.log_path.parent.parent / "holds", ignore_errors=True)
        self.driver.app_release(inst.reserve_key or inst.key, process=process)
        db = self.driver.db
        with locked(db):
            db.execute("UPDATE app_instance SET stopped_at = ?, stop_reason = ?, exit_code = ? "
                       "WHERE id = ? AND stopped_at IS NULL", (_now_iso(), reason, proc.returncode, inst.id))
            db.commit()

    def stop_now(self, key: str, reason: str, process: bool = False) -> None:
        """Stop ``key``'s instance at once: the Driver's hook for a Kill or a removal (``process=False``, as the
        Driver processes the engine itself), and a same-version redeploy's, before its directory is replaced."""
        for inst in self._of(key):
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
        """Stop every instance: the Catchment is shutting down. :meth:`reconcile` runs in a thread that cancelling
        its loop doesn't stop, so one pass could be under way while the instances are stopped here; it found the
        process gone, recorded it as exited and failed the App (the transit showcase, one restart in three). The
        ``closing`` flag, which the Catchment's shutdown sets before it cancels the loop (and this sets again), makes
        such a pass leave everything alone."""
        self.closing = True
        for inst in self._all():
            self._stop(inst, "shutdown")

    # ─── the loop ───────────────────────────────────────────────────────────

    def reconcile(self) -> None:
        """One pass: record instances that exited on their own, stop idle and outdated ones, and refresh
        each running App's freshness file."""
        instances = self._all()
        now = time.monotonic()
        if self._orphans:
            self._expire_orphans()
        for inst in instances:
            if self.closing:
                return  # shutdown stops them (see shutdown)
            if inst.stopping or not inst.ready:
                continue  # a start in progress is _wait_ready's to judge
            view = self.driver.app_view(inst.key)
            if inst.remote and inst.proc.poll() is None and self._remote_lost(inst):
                inst.proc.gone = True
            if inst.proc.poll() is not None:
                if inst.stopping or self.closing:
                    continue  # stopped since the check above: the stopper records it
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
            elif inst.draining and (inst.active <= 0 or now - inst.drain_since >= DRAIN_TIMEOUT_S):
                self._stop(inst, "drained")
            elif inst.remote and not inst.draining and self._near_time_limit(inst, view, now):
                inst.draining, inst.drain_since = True, now  # its replacement starts as for any shortfall
                self._decide(inst.key, self._live_count(inst.key, view),
                             f"replacing #{inst.id} before its compute's time limit")
            elif (view["app"]["idle_timeout_s"] > 0 and inst.active <= 0
                  and now - inst.last_used >= view["app"]["idle_timeout_s"]
                  and self._live_count(inst.key, view) > view["scaling"]["min_instances"]
                  and not (view["scaling"].get("hold") or {}).get("until")):
                self._stop(inst, "idle")
                self._crashes.pop(inst.key, None)
            elif inst.remote:
                self._send_freshness(inst)
            else:
                self._sample(inst, view)
                self._write_freshness(inst.key, app_runtime_dir(self.root, view["name"], view["major"]))

    def _near_time_limit(self, inst: Instance, view: dict, now: float) -> bool:
        """Whether a remote instance is within an hour of its backend's execution limit (Cloud Run and Container
        Apps jobs run 7 days at most)."""
        if inst.adopted:
            return False
        if not hasattr(inst, "_limit"):
            limit = None
            max_lifetime = getattr(self.driver.launcher, "max_lifetime", None)
            if max_lifetime is not None:
                try:
                    limit = max_lifetime(self.driver.duck_config(inst.key))
                except Exception:
                    limit = None
            inst._limit = limit
        return inst._limit is not None and now - inst.started >= max(inst._limit - 3600.0, inst._limit * 0.9)

    def _live_count(self, key: str, view: dict) -> int:
        """Instances of ``key`` running the current deploy and not on their way out."""
        return sum(1 for i in self._of(key) if not i.stopping and not i.draining and _current(i, view)
                   and i.proc.poll() is None)

    def _remote_lost(self, inst: Instance) -> bool:
        """Whether an instance elsewhere is gone: its launcher no longer runs it, or its agent has been silent for
        ``REMOTE_SILENCE_S``."""
        session = inst.session
        if session is None:
            return True
        if not inst.adopted:
            try:
                if not self.driver.launcher.is_running(inst.launch_key):
                    return True
            except Exception:
                pass
        loop = self.loop
        if loop is None:
            return False
        return loop.time() - session.last_contact > REMOTE_SILENCE_S

    def _send_freshness(self, inst: Instance) -> None:
        fresh = self.driver.app_freshness(inst.key, persisted=True)
        if fresh != inst.sent_freshness and inst.session is not None:
            if self._in_loop(inst.session.control({"type": "freshness", "data": fresh})):
                inst.sent_freshness = fresh

    async def adopt(self, iid: int, name: str, major: int):
        """An agent dialled in for an instance this Catchment process didn't start: one a previous process left
        running. Adopt it as a ready instance when it runs the deploy its App now selects (its tunnel session, or
        ``None`` to refuse it, which tells the agent to stop)."""
        self.loop = asyncio.get_running_loop()
        orphan = self._orphans.pop(iid, None)
        if orphan is None:
            return None
        key = f"{name}@{major}"
        view = self.driver.app_view(key)
        if view is None or view["version_id"] != orphan["version_id"] or view["is_killed"]:
            self._close_orphan(iid, "redeploy" if view is not None else "removed")
            return None
        inst = Instance(key=key, id=iid, version_id=view["version_id"], deployed_at=view["deployed_at"],
                        proc=RemoteProc(), port=0, log_path=self.root / orphan["log_path"],
                        reserve_key=f"{key}#remote{iid}", where="remote", launch_key=instance_key(name, major, iid),
                        ready=True, remote_ready=True, adopted=True, ready_event=asyncio.Event())
        inst.ready_event.set()
        session = await self.tunnels.create(iid, lambda message: self._remote_control(inst, message))
        inst.session, inst.port = session, session.port
        with self._mu:
            self._instances.setdefault(key, []).append(inst)
        log.info("adopted %s instance %s, left running by the previous Catchment process", key, iid)
        return session

    def _close_orphan(self, iid: int, reason: str) -> None:
        db = self.driver.db
        with locked(db):
            db.execute("UPDATE app_instance SET stopped_at = ?, stop_reason = ? WHERE id = ? AND stopped_at IS NULL",
                       (_now_iso(), reason, iid))
            db.commit()

    def _expire_orphans(self) -> None:
        """Instances elsewhere whose agents never dialled in again: recorded as shut down."""
        now = time.monotonic()
        for iid, orphan in list(self._orphans.items()):
            if now - orphan["since"] > REMOTE_SILENCE_S * 2:
                self._orphans.pop(iid, None)
                self._close_orphan(iid, "shutdown")

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
            rows = db.execute("SELECT ai.id, ai.pid, ai.place, ai.pond_version_id, ai.log_path "
                              "FROM app_instance ai WHERE ai.stopped_at IS NULL").fetchall()
            for iid, pid, place, version_id, log_path in rows:
                if place == "remote":  # its agent may dial in again: adopt it then (adopt), else it stops
                    self._orphans[iid] = {"version_id": version_id, "log_path": log_path, "since": time.monotonic()}
                    continue
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
    supervisor.loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(supervisor.reconcile)
            for key in supervisor.driver.app_keys():  # warm floors and growth (plans/app-scaling.md)
                await supervisor.scale_app(key)
            records = getattr(supervisor.driver, "records", None)
            if records is not None:  # Records' PostgreSQL back after a restart or a crash
                await asyncio.to_thread(records.keep_alive)
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
            f"ai.log_path, ai.place, ai.start_reason FROM app_instance ai JOIN pond_version pv ON pv.id = ai.pond_version_id "
            f"WHERE ai.pond_version_id IN ({marks}) ORDER BY ai.id DESC LIMIT ?", (*version_ids, limit)).fetchall()
    return [{"id": r[0], "version": r[1], "started_at": r[2], "ready_at": r[3], "stopped_at": r[4],
             "stop_reason": r[5], "exit_code": r[6], "log_path": r[7], "place": r[8], "start_reason": r[9]}
            for r in rows]


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
    from ..environments import ensure_env

    name, major = view["name"], view["major"]
    python = ensure_env(Path(root), Path(root) / view["source_path"], check_lock=False)
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


