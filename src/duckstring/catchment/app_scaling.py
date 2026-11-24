"""How many instances an App runs (plans/app-scaling.md): its settings, the operator's overrides, and the scaler.

**Settings.** ``[app]`` declares ``instances`` (the maximum), ``min_instances``, ``concurrency`` (the active requests
and sockets one instance takes), ``connections`` (PostgreSQL connections one instance may hold) and ``[app.scale]``
(the default scaler's thresholds). The operator overrides any of them per line (``duckstring app scale``, the
``app_scale`` table), kept across redeploys; :func:`effective` says what applies and where it came from.

**The scaler** decides, each tick, how many instances an App should have and which to drain. The supervisor enforces
what must hold whatever it says: the minimum and maximum, the connection budget, one start at a time, drains
finishing before stops. ``DUCKSTRING_APP_SCALER=module:Class`` replaces the default, which grows by one when, for
``after`` seconds, the ready instances' active work per instance reaches ``requests`` of ``concurrency``, or CPU
``cpu`` of their cores, or memory ``memory`` of their limit, or the App's own reported load ``load``. It never drains:
an instance stops after ``idle_timeout`` without work, and routing packs sessions so surplus instances empty.
"""

from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass, field

from ..app import scale_settings

log = logging.getLogger(__name__)

# The override columns and the settings they replace.
OVERRIDES = {"min_instances": "min_instances", "max_instances": "instances", "concurrency": "concurrency",
             "connections": "connections", "requests": "scale.requests", "cpu": "scale.cpu",
             "memory": "scale.memory", "load": "scale.load"}


def effective(cfg: dict, override: dict | None) -> dict:
    """The App's scaling settings with the operator's overrides applied, and ``origin``: for each setting whether it
    is the ``override``, ``pond.toml``'s or the ``default``. ``hold`` is a pinned count (``app scale --to``)."""
    out = scale_settings(cfg)
    defaults = scale_settings({})
    declared_scale = cfg.get("scale") or {}
    origin = {key: "pond.toml" if out[key] != defaults[key] else "default"
              for key in ("instances", "min_instances", "concurrency")}
    origin["connections"] = "pond.toml" if cfg.get("connections") is not None else "default"
    for key in ("requests", "cpu", "memory", "load", "after_s", "socket_idle_s"):
        origin[f"scale.{key}"] = "pond.toml" if key in declared_scale else "default"
    o = override or {}
    explicit_connections = o.get("connections") is not None or cfg.get("connections") is not None
    for column, setting in OVERRIDES.items():
        value = o.get(column)
        if value is None:
            continue
        if setting.startswith("scale."):
            out["scale"][setting[6:]] = float(value)
        else:
            out[setting] = int(value)
        origin[setting] = "override"
    if not explicit_connections:
        out["connections"] = out["concurrency"]  # follows the effective concurrency
    out["min_instances"] = min(out["min_instances"], out["instances"])
    out["origin"] = origin
    out["hold"] = {"count": o.get("hold_count"), "until": o.get("hold_until")} if o.get("hold_count") else None
    return out


@dataclass
class InstanceView:
    id: int
    where: str  # "catchment" | "remote"
    ready: bool
    starting: bool
    draining: bool
    requests: int  # HTTP requests in flight
    active_sockets: int  # WebSockets with a message within socket_idle
    open_sockets: int
    cpu: float | None  # share of its cores, 0 to 1
    memory: float | None  # share of its memory limit, 0 to 1 (None without a limit)
    load: float | None  # the App's own report, 0 to 1
    age_s: float
    idle_s: float

    @property
    def active(self) -> int:
        return self.requests + self.active_sockets


@dataclass
class AppView:
    key: str
    now: float  # monotonic seconds
    min_instances: int
    max_instances: int
    concurrency: int
    thresholds: dict  # requests, cpu, memory, load, after_s, socket_idle_s
    hold: dict | None
    instances: list[InstanceView] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)  # recent ones, newest last: {at, desired, reason}


@dataclass
class Decision:
    desired: int
    drain: list[int] = field(default_factory=list)
    reason: str = ""


class DefaultScaler:
    """Grow by one under sustained pressure; never drain (idle stops and packed routing shrink)."""

    def __init__(self):
        self._since: dict[str, float] = {}  # App key -> when its current pressure began

    def pressure(self, app: AppView) -> str | None:
        """What is over its threshold across the ready instances, or ``None``."""
        ready = [i for i in app.instances if i.ready and not i.draining]
        if not ready:
            return None
        t = app.thresholds
        per = sum(i.active for i in ready) / len(ready)
        if per >= t["requests"] * app.concurrency:
            return f"{per:.1f} active per instance (concurrency {app.concurrency})"
        for name, values in (("cpu", [i.cpu for i in ready]), ("memory", [i.memory for i in ready]),
                             ("load", [i.load for i in ready])):
            known = [v for v in values if v is not None]
            if known and len(known) == len(ready) and min(known) >= t[name]:
                return f"{name} {round(min(known) * 100)}% on every instance"
            if known and len(known) < len(ready) and sum(known) / len(ready) >= t[name]:
                return f"{name} {round(sum(known) / len(ready) * 100)}% on average"
        return None

    def decide(self, app: AppView) -> Decision:
        live = [i for i in app.instances if not i.draining]
        count = len(live)
        if app.hold and app.hold.get("count"):
            return Decision(desired=max(count, int(app.hold["count"])), reason="held by the operator")
        why = self.pressure(app)
        if why is None:
            self._since.pop(app.key, None)
            return Decision(desired=count, reason="")
        since = self._since.setdefault(app.key, app.now)
        if any(i.starting for i in live) or app.now - since < app.thresholds["after_s"]:
            return Decision(desired=count, reason=why)
        self._since[app.key] = app.now  # the next one waits its own `after`
        return Decision(desired=count + 1, reason=why)


def load_scaler():
    """The scaler ``DUCKSTRING_APP_SCALER`` names (``module:Class``), else the default."""
    spec = os.environ.get("DUCKSTRING_APP_SCALER")
    if not spec:
        return DefaultScaler()
    try:
        module, _, name = spec.partition(":")
        return getattr(importlib.import_module(module), name)()
    except Exception:
        log.exception("DUCKSTRING_APP_SCALER=%s can't be loaded: the default scaler decides", spec)
        return DefaultScaler()
