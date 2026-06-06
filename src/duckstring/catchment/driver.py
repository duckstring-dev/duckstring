"""The Catchment driver: the freshness brain + Duck coordinator.

Holds the in-memory :class:`~duckstring.engine.EngineState` (full Ponds + Ripples, pull + push),
loaded from SQLite at startup and write-through-persisted per event. It is event-driven:

* trigger calls (``tap``/``pulse``/``wave``/``tide``/``stop``) mutate the engine, then ``_process``
  runs ``sentinel`` and dispatches each emitted ``BeginRun`` to the target Pond's Duck (spawning one
  if needed) as a queued job.
* Duck events (``on_event``) feed ``complete_ripple``, which drives the ripple pull cascade →
  more ``BeginRun``s; run history is written to ``pond_run`` / ``ripple_run``.
* ``scheduler_tick`` (called on a timer) runs ``tick`` for Tide/window clocks.

Ponds are keyed by name in the engine; Ripples by ``"{pond}.{ripple}"``. A ``threading.RLock``
guards all state; SQLite is the durable mirror, the per-Pond ``pond.db`` ledgers the fallback.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from ..engine import (
    EngineState,
    Pond,
    PondState,
    Ripple,
    RippleState,
    Trigger,
    Window,
    complete_ripple,
    drain_begin_runs,
    next_wake,
    pulse_pond,
    sentinel,
    stop_pond,
    tap_pond,
    tick,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class Driver:
    def __init__(self, db, root, base_url: str, launcher):
        self.db = db
        self.root = root
        self.base_url = base_url
        self.launcher = launcher
        self.lock = threading.RLock()
        self.state = EngineState()
        self.meta: dict[str, dict] = {}  # pond_name -> {version_id, version, source_path, ripple_ids}
        self.jobs: dict[str, list[dict]] = {}  # pond_name -> queued Duck commands
        self.reload()

    # ─── Topology load ────────────────────────────────────────────────────────

    def reload(self) -> None:
        """(Re)build the engine + metadata from the database (selected Ponds only)."""
        with self.lock:
            db = self.db
            ponds: dict[str, Pond] = {}
            pond_states: dict[str, PondState] = {}
            ripples: dict[str, Ripple] = {}
            ripple_states: dict[str, RippleState] = {}
            triggers: dict[str, Trigger] = {}
            self.meta = {}
            self._incomplete: list[tuple[str, datetime]] = []  # (pond, F) runs to resume

            name_by_pnid = {r[0]: r[1] for r in db.execute("SELECT id, name FROM pond_name")}
            rows = db.execute("""
                SELECT pn.name, p.id, p.pond_version_id, pv.version, pv.source_path, pn.kind
                FROM pond p JOIN pond_name pn ON pn.id = p.pond_name_id
                JOIN pond_version pv ON pv.id = p.pond_version_id
            """).fetchall()
            deployed = {name for name, *_ in rows}
            pondid_to_name = {pid: nm for nm, pid, *_ in rows}
            for name, pond_id, pv_id, version, source_path, kind in rows:
                self.meta[name] = {"version_id": pv_id, "version": version, "source_path": source_path,
                                   "pond_id": pond_id, "kind": kind, "ripple_ids": {}}

            for name, pond_id, pv_id, _version, _source_path, _kind in rows:
                sources, optional = [], set()
                for snid, required in db.execute(
                    "SELECT source_pond_name_id, required FROM pond_to_pond WHERE pond_id = ?", (pond_id,)
                ):
                    sname = name_by_pnid.get(snid)
                    if sname in deployed:  # only wire sources that are themselves deployed
                        sources.append(sname)
                        if not required:
                            optional.add(sname)
                windows = [
                    Window(cron, timedelta(milliseconds=dur))
                    for cron, dur in db.execute(
                        "SELECT cron, duration_ms FROM pond_window WHERE pond_id = ?", (pond_id,)
                    )
                ]
                ponds[name] = Pond(id=name, name=name, sources=sources, optional_sources=optional, windows=windows)
                pond_states[name] = self._load_pond_state(pond_id)

                rip_rows = db.execute("SELECT id, name FROM ripple WHERE pond_version_id = ?", (pv_id,)).fetchall()
                rid_to_rname = {rid: rname for rid, rname in rip_rows}
                for rid, rname in rip_rows:
                    self.meta[name]["ripple_ids"][rname] = rid
                for rid, rname in rip_rows:
                    parent_rids = [
                        r[0] for r in db.execute("SELECT source_id FROM ripple_to_ripple WHERE sink_id = ?", (rid,))
                    ]
                    eid = f"{name}.{rname}"
                    parents = [f"{name}.{rid_to_rname[p]}" for p in parent_rids if p in rid_to_rname]
                    ripples[eid] = Ripple(id=eid, pond_id=name, name=rname, parents=parents)
                    ripple_states[eid] = RippleState()

                # Restore execution state from run history: gen (run counts), per-Ripple freshness, and
                # any Pond Run that was still 'running' when the Catchment stopped (resumed below).
                ps = pond_states[name]
                ps.runs_started = db.execute(
                    "SELECT COUNT(*) FROM pond_run WHERE pond_version_id = ?", (pv_id,)
                ).fetchone()[0]
                ps.runs_completed = db.execute(
                    "SELECT COUNT(*) FROM pond_run WHERE pond_version_id = ? AND status = 'success'", (pv_id,)
                ).fetchone()[0]
                for rid, rname in rip_rows:
                    row = db.execute(
                        "SELECT MAX(f) FROM ripple_run WHERE pond_version_id = ? AND ripple_id = ? "
                        "AND status = 'success'", (pv_id, rid),
                    ).fetchone()
                    if row and row[0]:
                        ef = datetime.fromisoformat(row[0])
                        ripple_states[f"{name}.{rname}"].start_f = ef
                        ripple_states[f"{name}.{rname}"].end_f = ef
                for (incf,) in db.execute(
                    "SELECT f FROM pond_run WHERE pond_version_id = ? AND status = 'running'", (pv_id,)
                ):
                    self._incomplete.append((name, datetime.fromisoformat(incf)))

            for pond_id, kind, bound_ms in db.execute(
                "SELECT pond_id, kind, bound_ms FROM pond_trigger WHERE status = 'active'"
            ):
                name = pondid_to_name.get(pond_id)
                if name:
                    bound = timedelta(milliseconds=bound_ms) if bound_ms is not None else None
                    triggers[name] = Trigger(pond_id=name, kind=kind, bound=bound)

            self.state = EngineState(
                ponds=ponds, pond_states=pond_states, ripples=ripples,
                ripple_states=ripple_states, triggers=triggers,
            )
            self.jobs = {name: self.jobs.get(name, []) for name in ponds}

    def _load_pond_state(self, pond_id: int) -> PondState:
        row = self.db.execute(
            "SELECT start_f, end_f, d_ms, has_pull, has_received_pull FROM pond_state WHERE pond_id = ?",
            (pond_id,),
        ).fetchone()
        ps = PondState()
        if row:
            sf, ef, d_ms, hp, hrp = row
            from ..engine import NEVER
            ps.start_f = datetime.fromisoformat(sf) if sf else NEVER
            ps.end_f = datetime.fromisoformat(ef) if ef else NEVER
            ps.d = timedelta(milliseconds=d_ms or 0)
            ps.has_pull = bool(hp)
            ps.has_received_pull = bool(hrp)
            ps.targets = [
                datetime.fromisoformat(r[0])
                for r in self.db.execute("SELECT target_f FROM pond_target WHERE pond_id = ?", (pond_id,))
            ]
        return ps

    # ─── Triggers ─────────────────────────────────────────────────────────────

    def tap(self, pond: str) -> None:
        with self.lock:
            self.state = tap_pond(self.state, pond, _now())
            self._process(_now())

    def pulse(self, pond: str) -> None:
        with self.lock:
            self.state = pulse_pond(self.state, pond, _now())
            self._process(_now())

    def wave(self, pond: str) -> None:
        with self.lock:
            self.state.triggers[pond] = Trigger(pond_id=pond, kind="wave")
            self._persist_trigger(pond, "wave", None)
            self._tick_process(_now())

    def tide(self, pond: str, bound: timedelta) -> None:
        with self.lock:
            self.state.triggers[pond] = Trigger(pond_id=pond, kind="tide", bound=bound)
            self._persist_trigger(pond, "tide", int(bound.total_seconds() * 1000))
            self._tick_process(_now())

    def stop(self, pond: str) -> None:
        with self.lock:
            self.state = stop_pond(self.state, pond, _now())
            self.state.triggers.pop(pond, None)
            self.db.execute(
                "DELETE FROM pond_trigger WHERE pond_id = ?", (self.meta[pond]["pond_id"],)
            )
            self.db.commit()
            self._process(_now())

    def remove_trigger(self, pond: str) -> None:
        """Remove the standing Wave/Tide trigger from a Pond. Unlike stop, this leaves existing demand
        to drain naturally — it just stops new runs from being re-tapped/clocked."""
        with self.lock:
            self.state.triggers.pop(pond, None)
            self.db.execute(
                "DELETE FROM pond_trigger WHERE pond_id = ?", (self.meta[pond]["pond_id"],)
            )
            self.db.commit()
            self._process(_now())

    # ─── Duck events ──────────────────────────────────────────────────────────

    def on_event(self, pond: str, payload: dict) -> None:
        with self.lock:
            now = _now()
            kind = payload.get("kind")
            f = payload.get("f")
            if kind == "ripple" and payload.get("status") == "success":
                rname = payload["ripple"]
                eid = f"{pond}.{rname}"
                if eid in self.state.ripple_states:
                    # Trust the Duck's run freshness: stamp start_f from the event so the completion is
                    # recorded correctly even for a resumed run the Catchment didn't model the start of.
                    if f:
                        self.state.ripple_states[eid].start_f = datetime.fromisoformat(f)
                    self.state = complete_ripple(self.state, eid, now)
                    self._record_ripple_run(pond, rname, f, "success", now)
                    self._process(now)
            elif kind == "run_completed":
                self._finish_pond_run(pond, f, now)
                self._process(now)

    def resume_incomplete(self) -> None:
        """Re-dispatch Pond Runs that were in flight when the Catchment stopped, and service any
        restored demand. The Duck reconciles each run against its ledger (re-running only the
        incomplete Ripples) and replays the completions the Catchment missed. Call once at startup."""
        with self.lock:
            now = _now()
            for name, f in self._incomplete:
                self._dispatch_begin_run(name, f, now)
            self._incomplete = []
            self._process(now)

    def take_jobs(self, pond: str) -> list[dict]:
        with self.lock:
            jobs = self.jobs.get(pond, [])
            self.jobs[pond] = []
            return jobs

    # ─── Scheduling ───────────────────────────────────────────────────────────

    def next_wake(self) -> datetime | None:
        with self.lock:
            return next_wake(_now(), self.state)

    def scheduler_tick(self) -> None:
        with self.lock:
            self._tick_process(_now())

    # ─── Core processing ──────────────────────────────────────────────────────

    def _tick_process(self, now: datetime) -> None:
        self.state = tick(now, self.state)
        self._process(now)

    def _process(self, now: datetime) -> None:
        self.state, _started = sentinel(now, self.state)
        for cmd in drain_begin_runs(self.state):
            self._dispatch_begin_run(cmd.pond_id, cmd.f, now)
        self._persist_state()
        self._reap_idle()

    def _dispatch_begin_run(self, pond: str, f: datetime, now: datetime) -> None:
        meta = self.meta[pond]
        self.launcher.ensure(pond, meta["version"], meta["source_path"])
        self.jobs.setdefault(pond, []).append({"kind": "begin_run", "f": _iso(f)})
        self.db.execute(
            "INSERT OR IGNORE INTO pond_run (pond_version_id, f, status) VALUES (?, ?, 'running')",
            (meta["version_id"], _iso(f)),
        )
        self.db.commit()

    def _reap_idle(self) -> None:
        # Keep all Ducks warm while any standing trigger is active (a Wave/Tide will run them again
        # shortly) — reaping mid-cycle would thrash on respawns. Only reap once fully quiescent.
        if self.state.triggers:
            return
        for name in self.state.ponds:
            ps = self.state.pond_states[name]
            busy = any(
                self.state.ripple_states[rid].is_running
                for rid in self.state.ripples
                if self.state.ripples[rid].pond_id == name
            )
            if (not busy and not ps.targets and not ps.has_pull and not self.jobs.get(name)
                    and self.launcher.is_running(name)):
                self.jobs.setdefault(name, []).append({"kind": "shutdown"})

    # ─── History + persistence ────────────────────────────────────────────────

    def _record_ripple_run(self, pond: str, rname: str, f: str, status: str, now: datetime) -> None:
        meta = self.meta[pond]
        rid = meta["ripple_ids"].get(rname)
        if rid is None:
            return
        self.db.execute(
            "INSERT OR REPLACE INTO ripple_run (pond_version_id, f, ripple_id, finished_at, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (meta["version_id"], f, rid, _iso(now), status),
        )
        self.db.commit()

    def _finish_pond_run(self, pond: str, f: str, now: datetime) -> None:
        meta = self.meta[pond]
        self.db.execute(
            "UPDATE pond_run SET finished_at = ?, status = 'success' WHERE pond_version_id = ? AND f = ?",
            (_iso(now), meta["version_id"], f),
        )
        self.db.commit()

    def _persist_trigger(self, pond: str, kind: str, bound_ms: int | None) -> None:
        self.db.execute(
            "INSERT INTO pond_trigger (pond_id, kind, bound_ms) VALUES (?, ?, ?) "
            "ON CONFLICT(pond_id) DO UPDATE SET kind = excluded.kind, bound_ms = excluded.bound_ms, status = 'active'",
            (self.meta[pond]["pond_id"], kind, bound_ms),
        )
        self.db.commit()

    def _persist_state(self) -> None:
        from ..engine import NEVER
        for name, ps in self.state.pond_states.items():
            pond_id = self.meta[name]["pond_id"]
            self.db.execute(
                "INSERT INTO pond_state (pond_id, start_f, end_f, d_ms, has_pull, has_received_pull) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(pond_id) DO UPDATE SET "
                "start_f = excluded.start_f, end_f = excluded.end_f, d_ms = excluded.d_ms, "
                "has_pull = excluded.has_pull, has_received_pull = excluded.has_received_pull",
                (
                    pond_id,
                    _iso(ps.start_f) if ps.start_f != NEVER else None,
                    _iso(ps.end_f) if ps.end_f != NEVER else None,
                    int(ps.d.total_seconds() * 1000),
                    int(ps.has_pull),
                    int(ps.has_received_pull),
                ),
            )
            self.db.execute("DELETE FROM pond_target WHERE pond_id = ?", (pond_id,))
            for t in ps.targets:
                self.db.execute(
                    "INSERT OR IGNORE INTO pond_target (pond_id, target_f) VALUES (?, ?)", (pond_id, _iso(t))
                )
        self.db.commit()

    # ─── Status ───────────────────────────────────────────────────────────────

    def status(self) -> dict:
        with self.lock:
            from ..engine import NEVER, min_target
            ts = lambda dt: _iso(dt) if dt is not None and dt != NEVER else None  # noqa: E731
            ponds = []
            for name in self.state.ponds:
                ps = self.state.pond_states[name]
                busy = any(
                    self.state.ripple_states[rid].is_running
                    for rid in self.state.ripples
                    if self.state.ripples[rid].pond_id == name
                )
                if busy:
                    st = "running"
                elif ps.has_pull or ps.targets:
                    st = "queued"
                else:
                    st = "idle"
                ponds.append({
                    "name": name,
                    "kind": self.meta[name]["kind"],
                    "version": self.meta[name]["version"],
                    "status": st,
                    "gen": ps.runs_started,
                    "has_pull": ps.has_pull,
                    "target_f": ts(min_target(ps.targets)),
                    "start_f": ts(ps.start_f),
                    "end_f": ts(ps.end_f),
                })
            edges = [[s, name] for name, pond in self.state.ponds.items() for s in pond.sources]
            return {"ponds": ponds, "edges": edges}
