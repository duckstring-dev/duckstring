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
    NEVER,
    EngineState,
    Pond,
    PondState,
    Ripple,
    RippleState,
    Trigger,
    Window,
    clear_pond,
    complete_ripple,
    drain_begin_runs,
    fail_pond,
    fail_ripple,
    next_wake,
    pulse_pond,
    sentinel,
    start_pond,
    stop_pond,
    tap_pond,
    tick,
)

# A Duck is presumed dead if it holds an in-flight Run but hasn't contacted the Catchment within this
# window (the secondary, transport-level signal; process-liveness is the primary one). Comfortably
# above the Duck's long-poll timeout so a healthy hold is never mistaken for death.
_DUCK_DEAD_AFTER = timedelta(seconds=60)


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
        self.last_seen: dict[str, datetime] = {}  # pond_name -> last Duck contact (jobs poll / event)
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
                windows = [self._row_to_window(r) for r in db.execute(
                    "SELECT start_anchor, duration_seconds, freq_unit, freq_interval, valid_days, until_time "
                    "FROM pond_window WHERE pond_id = ?", (pond_id,)
                )]
                retry = db.execute(
                    "SELECT immediate_retries, source_retries FROM pond_retry WHERE pond_id = ?", (pond_id,)
                ).fetchone()
                imm, onc = retry if retry else (0, 0)
                ponds[name] = Pond(
                    id=name, name=name, sources=sources, optional_sources=optional, windows=windows,
                    retry_immediately=imm, retry_on_change=onc,
                )
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
            "SELECT start_f, end_f, d_ms, has_pull, has_received_pull, is_failed, is_blocked, failed_f, "
            "failures FROM pond_state WHERE pond_id = ?",
            (pond_id,),
        ).fetchone()
        ps = PondState()
        if row:
            sf, ef, d_ms, hp, hrp, is_failed, is_blocked, failed_f, failures = row
            ps.start_f = datetime.fromisoformat(sf) if sf else NEVER
            ps.end_f = datetime.fromisoformat(ef) if ef else NEVER
            ps.d = timedelta(milliseconds=d_ms or 0)
            ps.has_pull = bool(hp)
            ps.has_received_pull = bool(hrp)
            ps.is_failed = bool(is_failed)
            ps.is_blocked = bool(is_blocked)
            ps.failed_f = datetime.fromisoformat(failed_f) if failed_f else NEVER
            ps.failures = failures or 0
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

    def start(self, pond: str) -> None:
        with self.lock:
            self.state = start_pond(self.state, pond, _now())
            self._process(_now())

    def clear(self, pond: str) -> None:
        """Operator acknowledgement: clear a Pond's failure/block (no run). Downstream Ponds blocked
        only by this failure re-derive and unblock on their own."""
        with self.lock:
            self.state = clear_pond(self.state, pond, _now())
            self._process(_now())

    def clear_on_redeploy(self, pond: str) -> None:
        """Called after a (re)deploy: if the Pond was failed, clear it — a fresh artifact presumably
        fixes the cause — so it (and anything blocked downstream) can resume without a manual clear.
        Only clears a Pond's *own* failure; one merely blocked by a still-failed Source stays blocked."""
        with self.lock:
            ps = self.state.pond_states.get(pond)
            if ps is not None and ps.is_failed:
                self.state = clear_pond(self.state, pond, _now())
                self._process(_now())

    def set_retry(self, pond: str, immediate_retries: int, source_retries: int) -> None:
        """Set the live retry budgets on a Pond (persisted to pond_retry; owned by the operator)."""
        with self.lock:
            pond_id = self.meta[pond]["pond_id"]
            self.db.execute(
                "INSERT INTO pond_retry (pond_id, immediate_retries, source_retries) VALUES (?, ?, ?) "
                "ON CONFLICT(pond_id) DO UPDATE SET immediate_retries = excluded.immediate_retries, "
                "source_retries = excluded.source_retries",
                (pond_id, immediate_retries, source_retries),
            )
            self.db.commit()
            p = self.state.ponds[pond]
            p.retry_immediately = immediate_retries
            p.retry_on_change = source_retries

    def retry_config(self, pond: str) -> dict:
        p = self.state.ponds[pond]
        return {"immediate_retries": p.retry_immediately, "source_retries": p.retry_on_change}

    def stop(self, pond: str, upstream: bool = False) -> None:
        with self.lock:
            self.state = stop_pond(self.state, pond, _now(), upstream=upstream)
            # Cancel any standing Wave/Tide trigger on every Pond the stop reached, so it can't re-tap.
            for name in self._stop_set(pond, upstream):
                if self.state.triggers.pop(name, None) is not None:
                    self.db.execute("DELETE FROM pond_trigger WHERE pond_id = ?", (self.meta[name]["pond_id"],))
            self.db.commit()
            self._process(_now())

    def _stop_set(self, pond: str, upstream: bool) -> set[str]:
        """The Ponds a stop reaches: just the target, or the whole upstream ancestry."""
        seen: set[str] = set()
        queue = [pond]
        while queue:
            cur = queue.pop(0)
            if cur in seen:
                continue
            seen.add(cur)
            if upstream:
                queue.extend(sp for sp in self.state.ponds[cur].sources if sp not in seen)
        return seen

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

    # ─── Windows (batch-availability on Inlets) ─────────────────────────────────

    def _row_to_window(self, row) -> Window:
        sa, dur, unit, interval, days, until = row
        return Window(
            start_anchor=datetime.fromisoformat(sa),
            duration=timedelta(seconds=dur),
            freq_unit=unit,
            freq_interval=interval,
            valid_days=frozenset(days.split(",")) if days else None,
            until=datetime.fromisoformat(until) if until else None,
        )

    def add_window(self, pond: str, name: str, start_anchor: str, duration_seconds: int,
                   freq_unit: str, freq_interval: int, valid_days: str | None = None,
                   until_time: str | None = None) -> None:
        """Add a recurring window to a Pond. Raises ValueError on a duplicate name or an overlap with
        an existing window (windows on a Pond must form a non-overlapping supply timeline)."""
        with self.lock:
            pond_id = self.meta[pond]["pond_id"]
            if self.db.execute(
                "SELECT 1 FROM pond_window WHERE pond_id = ? AND name = ?", (pond_id, name)
            ).fetchone():
                raise ValueError(f"A window named '{name}' already exists on '{pond}'")
            new_w = self._row_to_window(
                (start_anchor, duration_seconds, freq_unit, freq_interval, valid_days, until_time)
            )
            self._assert_no_overlap(pond, name, new_w)
            self.db.execute(
                "INSERT INTO pond_window (pond_id, name, start_anchor, duration_seconds, freq_unit, "
                "freq_interval, valid_days, until_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pond_id, name, start_anchor, duration_seconds, freq_unit, freq_interval, valid_days, until_time),
            )
            self.db.commit()
            self.reload()

    def _assert_no_overlap(self, pond: str, name: str, new_w: Window) -> None:
        h0 = new_w.start_anchor
        h1 = h0 + timedelta(days=366)
        new_wins = new_w.occurrences(h0, h1, cap=500)
        for ew in self.state.ponds[pond].windows:
            for es, ee in ew.occurrences(h0, h1, cap=500):
                for ns, ne in new_wins:
                    if max(ns, es) < min(ne, ee):
                        raise ValueError(
                            f"Window '{name}' overlaps an existing window on '{pond}' "
                            f"({ns.isoformat()} – {ne.isoformat()})"
                        )

    def list_windows(self, pond: str) -> list[dict]:
        with self.lock:
            pond_id = self.meta[pond]["pond_id"]
            rows = self.db.execute(
                "SELECT name, start_anchor, duration_seconds, freq_unit, freq_interval, valid_days, "
                "until_time FROM pond_window WHERE pond_id = ? ORDER BY name", (pond_id,)
            ).fetchall()
            return [
                {"name": n, "start_anchor": sa, "duration_seconds": d, "freq_unit": u,
                 "freq_interval": i, "valid_days": vd, "until_time": ut}
                for (n, sa, d, u, i, vd, ut) in rows
            ]

    def remove_window(self, pond: str, name: str) -> bool:
        with self.lock:
            pond_id = self.meta[pond]["pond_id"]
            cur = self.db.execute(
                "DELETE FROM pond_window WHERE pond_id = ? AND name = ?", (pond_id, name)
            )
            self.db.commit()
            self.reload()
            return cur.rowcount > 0

    # ─── Duck events ──────────────────────────────────────────────────────────

    def on_event(self, pond: str, payload: dict) -> None:
        with self.lock:
            now = _now()
            self.last_seen[pond] = now  # any event proves the Duck is alive
            kind = payload.get("kind")
            f = payload.get("f")
            status = payload.get("status", "success")
            if kind == "ripple":
                rname = payload["ripple"]
                eid = f"{pond}.{rname}"
                if eid in self.state.ripple_states:
                    # Trust the Duck's run freshness: stamp start_f from the event so the completion is
                    # recorded correctly even for a resumed run the Catchment didn't model the start of.
                    if f:
                        self.state.ripple_states[eid].start_f = datetime.fromisoformat(f)
                    if status == "success":
                        self.state = complete_ripple(self.state, eid, now)
                    # A "failed" ripple event is a within-budget immediate retry: record the attempt for
                    # history; the engine keeps modelling the Ripple as in-flight (the Duck relaunched it).
                    self._record_ripple_run(
                        pond, rname, f, status,
                        started_at=payload.get("started_at"),
                        finished_at=payload.get("finished_at") or _iso(now),
                        retry=payload.get("retry", 0),
                    )
                    self._process(now)
            elif kind == "failed":
                # The Pond Run gave up at this Ripple's freshness: fail the Pond (and block downstream).
                rname = payload["ripple"]
                eid = f"{pond}.{rname}"
                if eid in self.state.ripple_states:
                    if f:
                        self.state.ripple_states[eid].start_f = datetime.fromisoformat(f)
                    self.state = fail_ripple(self.state, eid, now)
                    self._fail_pond_run(pond, f, now)  # upsert the pond_run row first (ripple_run FK)
                    self._record_ripple_run(
                        pond, rname, f, "failed",
                        started_at=payload.get("started_at"),
                        finished_at=payload.get("finished_at") or _iso(now),
                        retry=payload.get("retry", 0),
                    )
                    self._process(now)
            elif kind == "run_completed":
                self._finish_pond_run(pond, f, now)
                self._process(now)
            elif kind == "pond_failed":
                # A Duck-level error (e.g. a failed ledger write): fail the whole Pond at its most
                # recently started Run. The Duck exits after reporting; liveness will not double-fail.
                self._fail_whole_pond(pond, now)

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
            self.last_seen[pond] = _now()  # the Duck is alive — it just polled
            jobs = self.jobs.get(pond, [])
            self.jobs[pond] = []
            return jobs

    # ─── Scheduling ───────────────────────────────────────────────────────────

    def next_wake(self) -> datetime | None:
        with self.lock:
            return next_wake(_now(), self.state)

    def scheduler_tick(self) -> None:
        with self.lock:
            now = _now()
            self._check_liveness(now)
            self._tick_process(now)

    def _check_liveness(self, now: datetime) -> None:
        """Fail any Pond whose Duck has died (process gone) or fallen silent (no contact) while a Run
        is in flight, attributing it to that Run (``start_f``). Only for launchers that own real Duck
        processes — the NoopLauncher (tests) has nothing to watch."""
        if not self.launcher.manages_processes:
            return
        for pond in list(self.state.ponds):
            ps = self.state.pond_states[pond]
            if ps.is_blocked:
                continue
            # In flight, and fresher than any recorded failure — so a retry-on-change Run draws a fresh
            # liveness check, but an already-failed Run is not re-failed.
            if not (ps.start_f > ps.end_f and ps.start_f > ps.failed_f):
                continue
            last = self.last_seen.get(pond)
            dead = not self.launcher.is_running(pond)
            silent = last is not None and (now - last) > _DUCK_DEAD_AFTER
            if dead or silent:
                self._fail_whole_pond(pond, now)

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
        self.last_seen[pond] = now  # grace clock: a freshly (re)spawned Duck isn't immediately stale
        self.jobs.setdefault(pond, []).append({
            "kind": "begin_run", "f": _iso(f),
            "immediate_retries": self.state.ponds[pond].retry_immediately,  # live budget, per Run
        })
        # Write started_at as tz-aware ISO (UTC) to match finished_at; the SQLite `datetime('now')`
        # default is naive and would be misread as local time by the UI.
        self.db.execute(
            "INSERT OR IGNORE INTO pond_run (pond_version_id, f, started_at, status) VALUES (?, ?, ?, 'running')",
            (meta["version_id"], _iso(f), _iso(now)),
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

    def _record_ripple_run(
        self, pond: str, rname: str, f: str, status: str, started_at: str | None, finished_at: str,
        retry: int = 0,
    ) -> None:
        meta = self.meta[pond]
        rid = meta["ripple_ids"].get(rname)
        if rid is None:
            return
        # Keyed on (pond_version, f, ripple, retry): each attempt is its own row — the retry trace.
        self.db.execute(
            "INSERT OR REPLACE INTO ripple_run (pond_version_id, f, ripple_id, retry, started_at, finished_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (meta["version_id"], f, rid, retry, started_at, finished_at, status),
        )
        self.db.commit()

    def _finish_pond_run(self, pond: str, f: str, now: datetime) -> None:
        meta = self.meta[pond]
        self.db.execute(
            "UPDATE pond_run SET finished_at = ?, status = 'success' WHERE pond_version_id = ? AND f = ?",
            (_iso(now), meta["version_id"], f),
        )
        self.db.commit()

    def _fail_whole_pond(self, pond: str, now: datetime) -> None:
        """Fail a Pond with no single culprit Ripple (dead/silent Duck, or a reported Duck-level
        error): mark its most recently started Run failed and run the cascade (which may re-dispatch a
        retry-on-change Run, respawning a Duck). No-op if nothing is in flight."""
        ps = self.state.pond_states[pond]
        if ps.start_f <= ps.end_f:
            return
        f = _iso(ps.start_f)
        self.state = fail_pond(self.state, pond, now)
        self._fail_pond_run(pond, f, now)
        self._process(now)

    def _fail_pond_run(self, pond: str, f: str, now: datetime) -> None:
        meta = self.meta[pond]
        self.db.execute(
            "INSERT INTO pond_run (pond_version_id, f, started_at, finished_at, status) "
            "VALUES (?, ?, ?, ?, 'failed') ON CONFLICT(pond_version_id, f) DO UPDATE SET "
            "finished_at = excluded.finished_at, status = 'failed'",
            (meta["version_id"], f, _iso(now), _iso(now)),
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
        for name, ps in self.state.pond_states.items():
            pond_id = self.meta[name]["pond_id"]
            self.db.execute(
                "INSERT INTO pond_state (pond_id, start_f, end_f, d_ms, has_pull, has_received_pull, "
                "is_failed, is_blocked, failed_f, failures) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(pond_id) DO UPDATE SET "
                "start_f = excluded.start_f, end_f = excluded.end_f, d_ms = excluded.d_ms, "
                "has_pull = excluded.has_pull, has_received_pull = excluded.has_received_pull, "
                "is_failed = excluded.is_failed, is_blocked = excluded.is_blocked, "
                "failed_f = excluded.failed_f, failures = excluded.failures",
                (
                    pond_id,
                    _iso(ps.start_f) if ps.start_f != NEVER else None,
                    _iso(ps.end_f) if ps.end_f != NEVER else None,
                    int(ps.d.total_seconds() * 1000),
                    int(ps.has_pull),
                    int(ps.has_received_pull),
                    int(ps.is_failed),
                    int(ps.is_blocked),
                    _iso(ps.failed_f) if ps.failed_f != NEVER else None,
                    ps.failures,
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

            def _demand_status(rs, running: bool) -> str:
                if running:
                    return "running"
                if rs.has_pull or rs.targets:
                    return "queued"
                return "idle"

            ponds = []
            for name in self.state.ponds:
                ps = self.state.pond_states[name]
                # Ripples belonging to this Pond, with their live per-Ripple state and intra-Pond edges.
                ripples = []
                ripple_edges = []
                for rid, rip in self.state.ripples.items():
                    if rip.pond_id != name:
                        continue
                    rs = self.state.ripple_states[rid]
                    ripples.append({
                        "name": rip.name,
                        "status": _demand_status(rs, rs.is_running),
                        "gen": rs.runs_started,
                        "runs_completed": rs.runs_completed,
                        "has_pull": rs.has_pull,
                        "target_f": ts(min_target(rs.targets)),
                        "start_f": ts(rs.start_f),
                        "end_f": ts(rs.end_f),
                    })
                    for parent in rip.parents:
                        psrc = self.state.ripples.get(parent)
                        if psrc is not None and psrc.pond_id == name:
                            ripple_edges.append([psrc.name, rip.name])

                busy = any(r["status"] == "running" for r in ripples)
                # Failure/block take precedence over demand state so a stalled Pond reads truthfully.
                if ps.is_failed:
                    st = "failed"
                elif ps.is_blocked:
                    st = "blocked"
                else:
                    st = _demand_status(ps, busy)

                trig = self.state.triggers.get(name)
                trigger = None
                if trig is not None:
                    trigger = {
                        "kind": trig.kind,
                        "bound_ms": int(trig.bound.total_seconds() * 1000) if trig.bound is not None else None,
                    }

                ponds.append({
                    "name": name,
                    "kind": self.meta[name]["kind"],
                    "version": self.meta[name]["version"],
                    "status": st,
                    "gen": ps.runs_started,
                    "runs_completed": ps.runs_completed,
                    "has_pull": ps.has_pull,
                    "target_f": ts(min_target(ps.targets)),
                    "start_f": ts(ps.start_f),
                    "end_f": ts(ps.end_f),
                    "d_ms": int(ps.d.total_seconds() * 1000),
                    "trigger": trigger,
                    "is_failed": ps.is_failed,
                    "is_blocked": ps.is_blocked,
                    "failed_f": ts(ps.failed_f),
                    "failures": ps.failures,
                    "immediate_retries": self.state.ponds[name].retry_immediately,
                    "source_retries": self.state.ponds[name].retry_on_change,
                    "ripples": ripples,
                    "ripple_edges": ripple_edges,
                })
            edges = [[s, name] for name, pond in self.state.ponds.items() for s in pond.sources]
            return {"ponds": ponds, "edges": edges}

    def _ancestors(self, name: str) -> set[str]:
        """``name`` plus all upstream (source) Pond names reachable from it (BFS over engine sources)."""
        seen = {name}
        queue = [name]
        while queue:
            n = queue.pop()
            pond = self.state.ponds.get(n)
            if pond is None:
                continue
            for src in pond.sources:
                if src not in seen:
                    seen.add(src)
                    queue.append(src)
        return seen

    def run_history(self, pond: str | None, lineage: bool, ripples: bool, limit: int) -> list[dict]:
        """Recent Pond Runs (newest first), optionally filtered to ``pond`` and — when ``lineage`` —
        its upstream sources. Ripple Runs are nested under each Pond Run only when ``ripples`` is set."""
        with self.lock:
            params: list = []
            where = ""
            if pond is not None:
                names = self._ancestors(pond) if lineage else {pond}
                where = f"WHERE pn.name IN ({','.join('?' * len(names))})"
                params.extend(sorted(names))
            rows = self.db.execute(
                "SELECT pn.name, pv.version, pr.pond_version_id, pr.f, pr.started_at, pr.finished_at, pr.status "
                "FROM pond_run pr "
                "JOIN pond_version pv ON pv.id = pr.pond_version_id "
                "JOIN pond_name pn ON pn.id = pv.pond_name_id "
                f"{where} ORDER BY pr.started_at DESC, pr.f DESC LIMIT ?",
                (*params, limit),
            ).fetchall()

            runs = []
            for pname, version, pv_id, f, started_at, finished_at, status in rows:
                run = {
                    "pond": pname, "version": version, "f": f,
                    "started_at": started_at, "finished_at": finished_at, "status": status,
                }
                if ripples:
                    rrows = self.db.execute(
                        "SELECT r.name, rr.started_at, rr.finished_at, rr.status, rr.retry "
                        "FROM ripple_run rr JOIN ripple r ON r.id = rr.ripple_id "
                        "WHERE rr.pond_version_id = ? AND rr.f = ? "
                        "ORDER BY COALESCE(rr.finished_at, rr.started_at), rr.retry",
                        (pv_id, f),
                    ).fetchall()
                    run["ripples"] = [
                        {"ripple": rn, "started_at": rsa, "finished_at": rfa, "status": rst, "retry": rt}
                        for (rn, rsa, rfa, rst, rt) in rrows
                    ]
                runs.append(run)
            return runs
