"""The Catchment driver: the freshness brain + Duck coordinator.

Holds the in-memory :class:`~duckstring.engine.EngineState` (full Ponds + Ripples, pull + push),
loaded from SQLite at startup and write-through-persisted per event. It is event-driven:

* trigger calls (``tap``/``pulse``/``wave``/``tide``/``stop``) mutate the engine, then ``_process``
  runs ``sentinel`` and dispatches each emitted ``BeginRun`` to the target Pond's Duck (spawning one
  if needed) as a queued job.
* Duck events (``on_event``) feed ``complete_ripple``, which drives the ripple pull cascade →
  more ``BeginRun``s; run history is written to ``pond_run`` / ``ripple_run``.
* ``scheduler_tick`` (called on a timer) runs ``tick`` for Tide/window clocks.

Ponds are keyed by ``"{name}@{major}"`` in the engine — each deployed major line is an independent
Pond instance — and Ripples by ``"{pond_key}.{ripple}"``. A ``threading.RLock`` guards all state;
SQLite is the durable mirror, the per-Pond ``pond.db`` ledgers the fallback.
"""

from __future__ import annotations

import json
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
    derive_blocked,
    drain_begin_runs,
    fail_pond,
    fail_ripple,
    force_pond,
    kill_pond,
    next_wake,
    pulse_pond,
    sentinel,
    sleep_pond,
    tap_pond,
    tick,
    wake_pond,
)
from ..keys import pond_key

# A Duck is presumed dead if it holds an in-flight Run but hasn't contacted the Catchment within this
# window (the secondary, transport-level signal; process-liveness is the primary one). Comfortably
# above the Duck's long-poll timeout so a healthy hold is never mistaken for death.
_DUCK_DEAD_AFTER = timedelta(seconds=60)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class Driver:
    def __init__(self, db, root, base_url: str | None, launcher):
        self.db = db
        self.root = root
        self.base_url = base_url
        self.launcher = launcher
        self.lock = threading.RLock()
        self.state = EngineState()
        # All dicts below are keyed by the pond key "{name}@{major}" — one entry per major line.
        self.meta: dict[str, dict] = {}  # key -> {name, major, version_id, version, source_path, ...}
        self.jobs: dict[str, list[dict]] = {}  # key -> queued Duck commands
        self.last_seen: dict[str, datetime] = {}  # key -> last Duck contact (jobs poll / event)
        # Pond Draw transfers awaiting the poller: (pond_key, F). A Draw run is not dispatched to a
        # Duck — the poller performs the parquet fetch out-of-lock, then reports completion.
        self._pending_transfers: list[tuple[str, datetime]] = []
        # Set by the app to a thread-safe callback that wakes the duct poller. Called from _process on
        # demand-bearing operations (tap/pulse/wave/…/Duck events) so a Draw solicits its upstream
        # immediately, not on the next poll. NOT called from the poller's own observe/transfer paths.
        self._notify_cb = None
        self.reload()

    def set_notify(self, cb) -> None:
        self._notify_cb = cb

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
                SELECT pn.name, p.major, p.id, p.pond_version_id, pv.version, pv.source_path, pn.kind,
                       p.is_draw
                FROM pond p JOIN pond_name pn ON pn.id = p.pond_name_id
                JOIN pond_version pv ON pv.id = p.pond_version_id
            """).fetchall()
            deployed = {pond_key(name, major) for name, major, *_ in rows}
            pondid_to_key = {pid: pond_key(nm, mj) for nm, mj, pid, *_ in rows}
            for name, major, pond_id, pv_id, version, source_path, kind, is_draw in rows:
                self.meta[pond_key(name, major)] = {
                    "name": name, "major": major, "version_id": pv_id, "version": version,
                    "source_path": source_path, "pond_id": pond_id, "kind": kind,
                    "is_draw": bool(is_draw), "ripple_ids": {},
                }

            for name, major, pond_id, pv_id, _version, _source_path, _kind, is_draw in rows:
                key = pond_key(name, major)
                sources, optional, missing = [], set(), []
                for snid, smajor, required in db.execute(
                    "SELECT source_pond_name_id, source_major, required FROM pond_to_pond WHERE pond_id = ?",
                    (pond_id,),
                ):
                    skey = pond_key(name_by_pnid.get(snid, ""), smajor)
                    if skey in deployed:  # only wire sources whose (name, major) line is deployed
                        sources.append(skey)
                        if not required:
                            optional.add(skey)
                    else:
                        # A declared Source (required or optional) is absent from this Catchment —
                        # not deployed and not drawn over a duct. Hard-block until it is present.
                        missing.append(skey)
                has_missing_source = bool(missing)
                self.meta[key]["missing_sources"] = missing
                windows = [self._row_to_window(r) for r in db.execute(
                    "SELECT start_anchor, duration_seconds, freq_unit, freq_interval, valid_days, until_time "
                    "FROM pond_window WHERE pond_id = ?", (pond_id,)
                )]
                retry = db.execute(
                    "SELECT immediate_retries, source_retries FROM pond_retry WHERE pond_id = ?", (pond_id,)
                ).fetchone()
                imm, onc = retry if retry else (0, 0)
                ponds[key] = Pond(
                    id=key, name=key, sources=sources, optional_sources=optional, windows=windows,
                    retry_immediately=imm, retry_on_change=onc, is_draw=bool(is_draw),
                    has_missing_source=has_missing_source,
                )
                pond_states[key] = self._load_pond_state(pond_id)

                rip_rows = db.execute("SELECT id, name FROM ripple WHERE pond_version_id = ?", (pv_id,)).fetchall()
                rid_to_rname = {rid: rname for rid, rname in rip_rows}
                for rid, rname in rip_rows:
                    self.meta[key]["ripple_ids"][rname] = rid
                for rid, rname in rip_rows:
                    parent_rids = [
                        r[0] for r in db.execute("SELECT source_id FROM ripple_to_ripple WHERE sink_id = ?", (rid,))
                    ]
                    eid = f"{key}.{rname}"
                    parents = [f"{key}.{rid_to_rname[p]}" for p in parent_rids if p in rid_to_rname]
                    ripples[eid] = Ripple(id=eid, pond_id=key, name=rname, parents=parents)
                    ripple_states[eid] = RippleState()

                # Restore execution state from run history: gen (run counts), per-Ripple freshness, and
                # any Pond Run that was still 'running' when the Catchment stopped (resumed below).
                ps = pond_states[key]
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
                        ripple_states[f"{key}.{rname}"].start_f = ef
                        ripple_states[f"{key}.{rname}"].end_f = ef
                for (incf,) in db.execute(
                    "SELECT f FROM pond_run WHERE pond_version_id = ? AND status = 'running'", (pv_id,)
                ):
                    self._incomplete.append((key, datetime.fromisoformat(incf)))

            for pond_id, kind, bound_ms in db.execute(
                "SELECT pond_id, kind, bound_ms FROM pond_trigger WHERE status = 'active'"
            ):
                key = pondid_to_key.get(pond_id)
                if key:
                    bound = timedelta(milliseconds=bound_ms) if bound_ms is not None else None
                    triggers[key] = Trigger(pond_id=key, kind=kind, bound=bound)

            self.state = EngineState(
                ponds=ponds, pond_states=pond_states, ripples=ripples,
                ripple_states=ripple_states, triggers=triggers,
            )
            # Recompute blocked from the freshly-loaded topology: a Source that is absent now (or has
            # since become present, e.g. a duct was added) flips has_missing_source, so the persisted
            # is_blocked may be stale. Re-derive for every Pond (propagates to Sinks).
            for pid in self.state.pond_states:
                derive_blocked(self.state, pid)
            self.jobs = {key: self.jobs.get(key, []) for key in ponds}

    def _load_pond_state(self, pond_id: int) -> PondState:
        row = self.db.execute(
            "SELECT start_f, end_f, d_ms, has_pull, has_received_pull, is_failed, is_blocked, failed_f, "
            "failures, is_killed, pull_local, pull_m FROM pond_state WHERE pond_id = ?",
            (pond_id,),
        ).fetchone()
        ps = PondState()
        if row:
            (sf, ef, d_ms, hp, hrp, is_failed, is_blocked, failed_f, failures, is_killed, pull_local,
             pull_m) = row
            ps.is_killed = bool(is_killed)
            ps.pull_local = bool(pull_local)
            ps.pull_m = datetime.fromisoformat(pull_m) if pull_m else NEVER
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

    # ─── Pond resolution ──────────────────────────────────────────────────────

    def resolve(self, name: str, major: int | None = None, version: str | None = None) -> str:
        """Resolve a Pond reference to its engine key ``"{name}@{major}"``.

        Default is the highest deployed major line. ``version`` targets that version's major line
        and must be the currently *selected* artifact for it (only selected versions execute).
        Raises KeyError (unknown pond / major line) or ValueError (conflicting / unselected version).
        """
        with self.lock:
            majors = {m["major"]: k for k, m in self.meta.items() if m["name"] == name}
            if not majors:
                raise KeyError(f"Pond '{name}' not found")
            if version is not None:
                vmajor = int(version.split(".")[0])
                if major is not None and major != vmajor:
                    raise ValueError(f"major {major} conflicts with version {version} (major {vmajor})")
                key = majors.get(vmajor)
                if key is None:
                    raise KeyError(f"No deployed major {vmajor} of Pond '{name}'")
                selected = self.meta[key]["version"]
                if selected != version:
                    raise ValueError(
                        f"Version {version} of '{name}' is not the selected version for major {vmajor} "
                        f"(selected: {selected}) — deploy it to select it"
                    )
                return key
            if major is not None:
                key = majors.get(major)
                if key is None:
                    raise KeyError(f"No deployed major {major} of Pond '{name}'")
                return key
            return majors[max(majors)]

    # ─── Triggers ─────────────────────────────────────────────────────────────

    def tap(self, pond: str, m: datetime | None = None) -> None:
        """One pull. ``m`` (a duct forwarding the downstream's demand epoch) is the freshness an Inlet
        it reaches will mint; defaults to now."""
        with self.lock:
            self.state = tap_pond(self.state, pond, _now(), m)
            self._process(_now())

    def pulse(self, pond: str, at: datetime | None = None) -> None:
        """Push a target freshness. ``at`` (a duct forwarding the downstream's target) is the demand
        epoch; defaults to now."""
        with self.lock:
            self.state = pulse_pond(self.state, pond, at or _now())
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

    def wake(self, pond: str) -> None:
        """Wake — a one-shot non-propagating pull (run on fresh input; clears failure/kill)."""
        with self.lock:
            self.state = wake_pond(self.state, pond, _now())
            self._process(_now())

    def force(self, pond: str) -> None:
        """Force — recompute now at the current freshness, even with no upstream change."""
        with self.lock:
            self.state = force_pond(self.state, pond, _now())
            self._process(_now())

    def kill(self, pond: str) -> None:
        """Kill — terminate the Duck and park the Pond in a terminal killed state (cancels its Run)."""
        with self.lock:
            now = _now()
            ps = self.state.pond_states[pond]
            in_flight = ps.start_f if ps.start_f > ps.end_f else None
            self.state = kill_pond(self.state, pond, now)
            self.launcher.terminate(pond)  # cancel the Duck's running Ripples (kills the process)
            self.jobs[pond] = []
            if in_flight is not None:
                self._kill_pond_run(pond, _iso(in_flight), now)
            self._process(now)

    def clear(self, pond: str) -> None:
        """Operator acknowledgement: clear a Pond's failure/block (no run). Downstream Ponds blocked
        only by this failure re-derive and unblock on their own."""
        with self.lock:
            self.state = clear_pond(self.state, pond, _now())
            self._process(_now())

    def clear_on_redeploy(self, name: str, major: int) -> None:
        """Called after a (re)deploy: if the Pond was failed, clear it — a fresh artifact presumably
        fixes the cause — so it (and anything blocked downstream) can resume without a manual clear.
        Only clears a Pond's *own* failure; one merely blocked by a still-failed Source stays blocked."""
        with self.lock:
            ps = self.state.pond_states.get(pond_key(name, major))
            if ps is not None and ps.is_failed:
                self.state = clear_pond(self.state, pond_key(name, major), _now())
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

    def sleep(self, pond: str, upstream: bool = False) -> None:
        with self.lock:
            self.state = sleep_pond(self.state, pond, _now(), upstream=upstream)
            # Cancel any standing Wave/Tide trigger on every Pond the sleep reached, so it can't re-tap.
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
                        error=payload.get("error"), traceback=payload.get("traceback"),
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
                    err, tb = payload.get("error"), payload.get("traceback")
                    self._fail_pond_run(pond, f, now, err, tb)  # upsert the pond_run row first (ripple_run FK)
                    self._record_ripple_run(
                        pond, rname, f, "failed",
                        started_at=payload.get("started_at"),
                        finished_at=payload.get("finished_at") or _iso(now),
                        retry=payload.get("retry", 0),
                        error=err, traceback=tb,
                    )
                    self._process(now)
            elif kind == "run_completed":
                self._finish_pond_run(pond, f, now)
                self._process(now)
            elif kind == "pond_failed":
                # A Duck-level error (e.g. a failed ledger write): fail the whole Pond at its most
                # recently started Run. The Duck exits after reporting; liveness will not double-fail.
                self._fail_whole_pond(pond, now, payload.get("error"), payload.get("traceback"))

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

    # ─── Pond Draws (cross-Catchment) ───────────────────────────────────────────

    def draws(self) -> list[dict]:
        """Every Pond Draw, for the poller: its key/name/major and whether downstream demand wants
        the upstream solicited (a pull/push is pending but the upstream hasn't offered it yet)."""
        with self.lock:
            out = []
            for key, m in self.meta.items():
                if not m.get("is_draw"):
                    continue
                ps = self.state.pond_states[key]
                real_targets = [t for t in ps.targets if t > NEVER]
                if ps.remote_down:
                    target = pull_m = None  # blocked upstream: solicit nothing
                else:
                    # Forward the draw's outstanding demand upstream carrying its epoch, so the upstream
                    # Inlet mints the SAME freshness: the max push target, and the pull epoch.
                    target = _iso(max(real_targets)) if real_targets else None
                    pull_m = _iso(ps.pull_m) if (ps.has_pull and ps.pull_m > NEVER) else None
                out.append({
                    "key": key, "name": m["name"], "major": m["major"],
                    "target": target, "pull_m": pull_m,
                })
            return out

    def observe_remote(
        self, pond: str, remote_f: datetime | None, *, down: bool = False,
    ) -> None:
        """The poller reports an upstream Pond's freshness + reachability for a Draw. Mirror them and
        run the cascade — a transfer starts if there is downstream demand and the upstream is fresher."""
        with self.lock:
            ps = self.state.pond_states.get(pond)
            if ps is None or not self.meta.get(pond, {}).get("is_draw"):
                return
            if remote_f is not None:
                ps.remote_f = remote_f
            if ps.remote_down != down:
                ps.remote_down = down
                derive_blocked(self.state, pond)
            self._process(_now(), notify=False)  # poller-driven; transfers handled in this cycle

    def pond_observation(self, pond: str) -> dict:
        """A Pond's freshness + down-state, for the producer's ``…/wait`` long-poll (a downstream
        Catchment blocks on this until its drawn Pond advances)."""
        with self.lock:
            ps = self.state.pond_states.get(pond)
            if ps is None:
                return {"end_f": None, "down": False}
            down = ps.is_failed or ps.is_killed or ps.is_blocked
            return {"end_f": _iso(ps.end_f) if ps.end_f != NEVER else None, "down": down}

    def take_transfers(self) -> list[dict]:
        """Drain the Pond Draw transfers the poller should perform (fetch + land the parquet)."""
        with self.lock:
            out = []
            for key, f in self._pending_transfers:
                m = self.meta.get(key)
                if m is not None:
                    out.append({"key": key, "name": m["name"], "major": m["major"], "f": _iso(f)})
            self._pending_transfers = []
            return out

    def complete_draw_transfer(self, pond: str, f: str) -> None:
        """The poller finished landing a Draw's parquet at freshness ``f``: complete its transfer
        ripple (advancing the Draw's freshness, which cascades to downstream Sinks)."""
        with self.lock:
            now = _now()
            eid = f"{pond}.draw"
            rs = self.state.ripple_states.get(eid)
            if rs is None:
                return
            started = _iso(rs.started_at) if rs.started_at else _iso(now)
            rs.start_f = datetime.fromisoformat(f)
            self.state = complete_ripple(self.state, eid, now)
            self._record_ripple_run(pond, "draw", f, "success", started_at=started, finished_at=_iso(now))
            self._finish_pond_run(pond, f, now)
            self._process(now, notify=False)  # poller-driven

    def fail_draw_transfer(self, pond: str, f: str, error: str) -> None:
        """The poller could not land a Draw's parquet: fail the transfer (blocks downstream until the
        next successful poll/transfer)."""
        with self.lock:
            now = _now()
            eid = f"{pond}.draw"
            rs = self.state.ripple_states.get(eid)
            if rs is None:
                return
            started = _iso(rs.started_at) if rs.started_at else _iso(now)
            rs.start_f = datetime.fromisoformat(f)
            self.state = fail_ripple(self.state, eid, now)
            self._fail_pond_run(pond, f, now, error, None)
            self._record_ripple_run(pond, "draw", f, "failed", started_at=started,
                                    finished_at=_iso(now), error=error)
            self._process(now, notify=False)  # poller-driven

    # ─── Producer exposure (open / tap-on-get) ──────────────────────────────────

    def set_pond_open(self, pond: str, tap_on_get: bool) -> None:
        """Mark a Pond open (accepts demand from any source). Under single-level auth this is a no-op
        gate; its live effect is ``tap_on_get`` (a read on the query route fires a Tap)."""
        with self.lock:
            pid = self.meta[pond]["pond_id"]
            self.db.execute(
                "INSERT INTO pond_open (pond_id, tap_on_get) VALUES (?, ?) "
                "ON CONFLICT(pond_id) DO UPDATE SET tap_on_get = excluded.tap_on_get",
                (pid, int(tap_on_get)),
            )
            self.db.commit()

    def unset_pond_open(self, pond: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM pond_open WHERE pond_id = ?", (self.meta[pond]["pond_id"],))
            self.db.commit()

    def pond_tap_on_get(self, pond: str) -> bool:
        with self.lock:
            m = self.meta.get(pond)
            if m is None:
                return False
            row = self.db.execute(
                "SELECT tap_on_get FROM pond_open WHERE pond_id = ?", (m["pond_id"],)
            ).fetchone()
            return bool(row and row[0])

    # ─── Ducts (consumer side) ───────────────────────────────────────────────────

    def create_duct(self, origin: str, remote_url: str, auth_headers: dict | None) -> None:
        """Register (or update) a conduit from an upstream Catchment. ``auth_headers`` are the request
        headers to attach when dialling it — a secret at rest (duck.db is 0600)."""
        with self.lock:
            self.db.execute(
                "INSERT INTO duct (origin_catchment, remote_url, auth_json) VALUES (?, ?, ?) "
                "ON CONFLICT(origin_catchment) DO UPDATE SET remote_url = excluded.remote_url, "
                "auth_json = excluded.auth_json",
                (origin, remote_url, json.dumps(auth_headers) if auth_headers else None),
            )
            self.db.commit()

    def destroy_duct(self, origin: str) -> bool:
        with self.lock:
            row = self.db.execute("SELECT id FROM duct WHERE origin_catchment = ?", (origin,)).fetchone()
            if row is None:
                return False
            duct_id = row[0]
            for src_name, major in self.db.execute(
                "SELECT source_pond_name, major FROM duct_to_pond WHERE duct_id = ?", (duct_id,)
            ).fetchall():
                self._destroy_draw(src_name, major)
            self.db.execute("DELETE FROM duct_to_pond WHERE duct_id = ?", (duct_id,))
            self.db.execute("DELETE FROM duct WHERE id = ?", (duct_id,))
            self.db.commit()
            self.reload()
            return True

    def add_duct_pond(self, origin: str, pond_name: str, major: int, incremental: bool = False) -> None:
        with self.lock:
            row = self.db.execute("SELECT id FROM duct WHERE origin_catchment = ?", (origin,)).fetchone()
            if row is None:
                raise KeyError(f"No duct from '{origin}' — create it first")
            self._create_draw(pond_name, major)  # raises ValueError on a local-Pond collision
            self.db.execute(
                "INSERT OR REPLACE INTO duct_to_pond (duct_id, source_pond_name, major, incremental) "
                "VALUES (?, ?, ?, ?)",
                (row[0], pond_name, major, int(incremental)),
            )
            self.db.commit()
            self.reload()

    def remove_duct_pond(self, origin: str, pond_name: str, major: int) -> bool:
        with self.lock:
            row = self.db.execute("SELECT id FROM duct WHERE origin_catchment = ?", (origin,)).fetchone()
            if row is None:
                return False
            cur = self.db.execute(
                "DELETE FROM duct_to_pond WHERE duct_id = ? AND source_pond_name = ? AND major = ?",
                (row[0], pond_name, major),
            )
            self._destroy_draw(pond_name, major)
            self.db.commit()
            self.reload()
            return cur.rowcount > 0

    def list_ducts(self) -> list[dict]:
        """Ducts + their drawn Ponds, for the CLI/API (auth redacted)."""
        with self.lock:
            out = []
            for did, origin, url in self.db.execute(
                "SELECT id, origin_catchment, remote_url FROM duct ORDER BY origin_catchment"
            ).fetchall():
                members = [
                    {"pond": n, "major": mj, "incremental": bool(inc)}
                    for n, mj, inc in self.db.execute(
                        "SELECT source_pond_name, major, incremental FROM duct_to_pond "
                        "WHERE duct_id = ? ORDER BY source_pond_name, major", (did,)
                    )
                ]
                out.append({"origin": origin, "remote_url": url, "ponds": members})
            return out

    def duct_targets(self) -> list[dict]:
        """Ducts with auth resolved — for the poller only (never serialised to a client)."""
        with self.lock:
            out = []
            for did, origin, url, auth_json in self.db.execute(
                "SELECT id, origin_catchment, remote_url, auth_json FROM duct"
            ).fetchall():
                members = []
                for n, mj in self.db.execute(
                    "SELECT source_pond_name, major FROM duct_to_pond WHERE duct_id = ?", (did,)
                ):
                    ps = self.state.pond_states.get(pond_key(n, mj))
                    rf = ps.remote_f if ps is not None else NEVER
                    members.append({
                        "name": n, "major": mj,
                        "remote_f": _iso(rf) if rf != NEVER else None,  # the poller's wait baseline
                    })
                out.append({
                    "origin": origin, "remote_url": url,
                    "auth": json.loads(auth_json) if auth_json else {},
                    "members": members,
                })
            return out

    def _create_draw(self, name: str, major: int) -> None:
        """Materialise a Pond Draw's identity rows (caller holds the lock and reloads). Real but
        synthetic: kind='inlet', is_draw=1, a single immutable pond_version + one ``"draw"`` ripple."""
        db = self.db
        db.execute("INSERT OR IGNORE INTO pond_name (name, kind) VALUES (?, 'inlet')", (name,))
        db.execute("UPDATE pond_name SET kind = 'inlet' WHERE name = ?", (name,))
        (pn_id,) = db.execute("SELECT id FROM pond_name WHERE name = ?", (name,)).fetchone()

        existing = db.execute(
            "SELECT is_draw FROM pond WHERE pond_name_id = ? AND major = ?", (pn_id, major)
        ).fetchone()
        if existing is not None and not existing[0]:
            raise ValueError(f"A local Pond '{name}@{major}' already exists — cannot draw it over a duct")

        version = f"{major}.0.0"
        db.execute(
            "INSERT OR IGNORE INTO pond_version (pond_name_id, version, major, source_path) "
            "VALUES (?, ?, ?, ?)",
            (pn_id, version, major, f"draw://{name}@{major}"),
        )
        (pv_id,) = db.execute(
            "SELECT id FROM pond_version WHERE pond_name_id = ? AND version = ?", (pn_id, version)
        ).fetchone()
        db.execute("INSERT OR IGNORE INTO ripple (pond_version_id, name) VALUES (?, 'draw')", (pv_id,))
        db.execute(
            "INSERT INTO pond (pond_name_id, major, pond_version_id, is_draw) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(pond_name_id, major) DO UPDATE SET pond_version_id = excluded.pond_version_id, "
            "is_draw = 1",
            (pn_id, major, pv_id),
        )

    def _destroy_draw(self, name: str, major: int) -> None:
        """Remove a Pond Draw's identity + state rows (caller holds the lock and reloads). Leaves the
        ``pond_name`` placeholder so a Sink that still references it keeps its source row."""
        db = self.db
        row = db.execute("SELECT id FROM pond_name WHERE name = ?", (name,)).fetchone()
        if row is None:
            return
        pn_id = row[0]
        prow = db.execute(
            "SELECT id, pond_version_id, is_draw FROM pond WHERE pond_name_id = ? AND major = ?",
            (pn_id, major),
        ).fetchone()
        if prow is None or not prow[2]:
            return  # not a Draw — never remove a real local Pond here
        pond_id, pv_id = prow[0], prow[1]
        db.execute("DELETE FROM ripple_run WHERE pond_version_id = ?", (pv_id,))
        db.execute("DELETE FROM pond_run WHERE pond_version_id = ?", (pv_id,))
        for tbl in ("pond_state", "pond_target", "pond_open", "pond_trigger", "pond_retry", "pond_window"):
            db.execute(f"DELETE FROM {tbl} WHERE pond_id = ?", (pond_id,))
        db.execute("DELETE FROM pond WHERE id = ?", (pond_id,))
        rids = [r[0] for r in db.execute("SELECT id FROM ripple WHERE pond_version_id = ?", (pv_id,))]
        if rids:
            marks = ",".join("?" * len(rids))
            db.execute(
                f"DELETE FROM ripple_to_ripple WHERE sink_id IN ({marks}) OR source_id IN ({marks})",
                rids * 2,
            )
        db.execute("DELETE FROM ripple WHERE pond_version_id = ?", (pv_id,))
        db.execute("DELETE FROM pond_version WHERE id = ?", (pv_id,))

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
            if self.state.ponds[pond].is_draw:  # no Duck process — the poller drives transfers
                continue
            ps = self.state.pond_states[pond]
            if ps.is_blocked or ps.is_killed:  # killed Ponds are intentionally down — don't re-fail
                continue
            # In flight, and fresher than any recorded failure — so a retry-on-change Run draws a fresh
            # liveness check, but an already-failed Run is not re-failed.
            if not (ps.start_f > ps.end_f and ps.start_f > ps.failed_f):
                continue
            last = self.last_seen.get(pond)
            dead = not self.launcher.is_running(pond)
            silent = last is not None and (now - last) > _DUCK_DEAD_AFTER
            if dead:
                self._fail_whole_pond(pond, now, "Duck process is not running (it crashed or exited)")
            elif silent:
                self._fail_whole_pond(pond, now, "Lost contact with the Duck (no events received)")

    # ─── Core processing ──────────────────────────────────────────────────────

    def _tick_process(self, now: datetime) -> None:
        self.state = tick(now, self.state)
        self._process(now)

    def _process(self, now: datetime, notify: bool = True) -> None:
        self.state, _started = sentinel(now, self.state)
        for cmd in drain_begin_runs(self.state):
            self._dispatch_begin_run(cmd.pond_id, cmd.f, now, force=cmd.force)
        self._persist_state()
        self._reap_idle()
        # Wake the poller so a Draw forwards new demand to its upstream at once. The poller's own
        # observe/transfer paths pass notify=False (they're handled in-cycle) to avoid a busy loop.
        if notify and self._notify_cb is not None:
            self._notify_cb()

    def _dispatch_begin_run(self, pond: str, f: datetime, now: datetime, force: bool = False) -> None:
        meta = self.meta[pond]
        # A Pond Draw is not run by a Duck: record the Run as running and hand the parquet transfer to
        # the poller (it fetches out-of-lock, then reports completion via complete_draw_transfer).
        if meta.get("is_draw"):
            self.db.execute(
                "INSERT OR IGNORE INTO pond_run (pond_version_id, f, started_at, status) "
                "VALUES (?, ?, ?, 'running')",
                (meta["version_id"], _iso(f), _iso(now)),
            )
            self.db.commit()
            if (pond, f) not in self._pending_transfers:
                self._pending_transfers.append((pond, f))
            return
        self.launcher.ensure(pond, meta["version"], meta["source_path"])
        self.last_seen[pond] = now  # grace clock: a freshly (re)spawned Duck isn't immediately stale
        self.jobs.setdefault(pond, []).append({
            "kind": "begin_run", "f": _iso(f), "force": force,
            "immediate_retries": self.state.ponds[pond].retry_immediately,  # live budget, per Run
        })
        # Write started_at as tz-aware ISO (UTC) to match finished_at; the SQLite `datetime('now')`
        # default is naive and would be misread as local time by the UI. A Force re-opens the Run.
        self.db.execute(
            "INSERT OR REPLACE INTO pond_run (pond_version_id, f, started_at, status) VALUES (?, ?, ?, 'running')"
            if force else
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
        retry: int = 0, error: str | None = None, traceback: str | None = None,
    ) -> None:
        meta = self.meta[pond]
        rid = meta["ripple_ids"].get(rname)
        if rid is None:
            return
        # Keyed on (pond_version, f, ripple, retry): each attempt is its own row — the retry trace.
        self.db.execute(
            "INSERT OR REPLACE INTO ripple_run "
            "(pond_version_id, f, ripple_id, retry, started_at, finished_at, status, error, traceback) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (meta["version_id"], f, rid, retry, started_at, finished_at, status, error, traceback),
        )
        self.db.commit()

    def _finish_pond_run(self, pond: str, f: str, now: datetime) -> None:
        meta = self.meta[pond]
        self.db.execute(
            "UPDATE pond_run SET finished_at = ?, status = 'success' WHERE pond_version_id = ? AND f = ?",
            (_iso(now), meta["version_id"], f),
        )
        self.db.commit()

    def _fail_whole_pond(
        self, pond: str, now: datetime, error: str | None = None, tb: str | None = None
    ) -> None:
        """Fail a Pond with no single culprit Ripple (dead/silent Duck, or a reported Duck-level
        error): mark its most recently started Run failed and run the cascade (which may re-dispatch a
        retry-on-change Run, respawning a Duck). No-op if nothing is in flight."""
        ps = self.state.pond_states[pond]
        if ps.start_f <= ps.end_f:
            return
        f = _iso(ps.start_f)
        self.state = fail_pond(self.state, pond, now)
        self._fail_pond_run(pond, f, now, error, tb)
        self._process(now)

    def _fail_pond_run(
        self, pond: str, f: str, now: datetime, error: str | None = None, tb: str | None = None
    ) -> None:
        meta = self.meta[pond]
        self.db.execute(
            "INSERT INTO pond_run (pond_version_id, f, started_at, finished_at, status, error, traceback) "
            "VALUES (?, ?, ?, ?, 'failed', ?, ?) ON CONFLICT(pond_version_id, f) DO UPDATE SET "
            "finished_at = excluded.finished_at, status = 'failed', error = excluded.error, "
            "traceback = excluded.traceback",
            (meta["version_id"], f, _iso(now), _iso(now), error, tb),
        )
        self.db.commit()

    def _kill_pond_run(self, pond: str, f: str, now: datetime) -> None:
        meta = self.meta[pond]
        self.db.execute(
            "INSERT INTO pond_run (pond_version_id, f, started_at, finished_at, status, error) "
            "VALUES (?, ?, ?, ?, 'killed', 'Killed by operator') ON CONFLICT(pond_version_id, f) DO UPDATE SET "
            "finished_at = excluded.finished_at, status = 'killed', error = excluded.error",
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
                "is_failed, is_blocked, failed_f, failures, is_killed, pull_local, pull_m) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(pond_id) DO UPDATE SET "
                "start_f = excluded.start_f, end_f = excluded.end_f, d_ms = excluded.d_ms, "
                "has_pull = excluded.has_pull, has_received_pull = excluded.has_received_pull, "
                "is_failed = excluded.is_failed, is_blocked = excluded.is_blocked, "
                "failed_f = excluded.failed_f, failures = excluded.failures, "
                "is_killed = excluded.is_killed, pull_local = excluded.pull_local, pull_m = excluded.pull_m",
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
                    int(ps.is_killed),
                    int(ps.pull_local),
                    _iso(ps.pull_m) if ps.pull_m != NEVER else None,
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
            for key in self.state.ponds:
                ps = self.state.pond_states[key]
                # Ripples belonging to this Pond, with their live per-Ripple state and intra-Pond edges.
                ripples = []
                ripple_edges = []
                for rid, rip in self.state.ripples.items():
                    if rip.pond_id != key:
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
                        if psrc is not None and psrc.pond_id == key:
                            ripple_edges.append([psrc.name, rip.name])

                busy = any(r["status"] == "running" for r in ripples)
                # Failure/kill/block take precedence over demand state so a stalled Pond reads truthfully.
                if ps.is_failed:
                    st = "failed"
                elif ps.is_killed:
                    st = "killed"
                elif ps.is_blocked:
                    st = "blocked"
                else:
                    st = _demand_status(ps, busy)

                # Why is it blocked? Required Sources that are themselves down (failed/killed/blocked).
                pond = self.state.ponds[key]
                blocked_by = [
                    sp for sp in pond.sources if sp not in pond.optional_sources and (
                        self.state.pond_states[sp].is_failed
                        or self.state.pond_states[sp].is_blocked
                        or self.state.pond_states[sp].is_killed
                    )
                ]
                # The failure message (freshest failed Run), shown when failed.
                error = None
                if ps.is_failed:
                    row = self.db.execute(
                        "SELECT error FROM pond_run WHERE pond_version_id = ? AND status = 'failed' "
                        "ORDER BY f DESC LIMIT 1", (self.meta[key]["version_id"],),
                    ).fetchone()
                    error = row[0] if row else None

                trig = self.state.triggers.get(key)
                trigger = None
                if trig is not None:
                    trigger = {
                        "kind": trig.kind,
                        "bound_ms": int(trig.bound.total_seconds() * 1000) if trig.bound is not None else None,
                    }

                ponds.append({
                    "id": key,
                    "name": self.meta[key]["name"],
                    "major": self.meta[key]["major"],
                    "kind": self.meta[key]["kind"],
                    "is_draw": self.meta[key].get("is_draw", False),
                    "version": self.meta[key]["version"],
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
                    "is_killed": ps.is_killed,
                    "failed_f": ts(ps.failed_f),
                    "failures": ps.failures,
                    "missing_sources": self.meta[key].get("missing_sources", []),
                    "blocked_by": blocked_by,
                    "error": error,
                    "immediate_retries": self.state.ponds[key].retry_immediately,
                    "source_retries": self.state.ponds[key].retry_on_change,
                    "ripples": ripples,
                    "ripple_edges": ripple_edges,
                })
            # Edge endpoints are pond keys ("name@major") — match entries on their "id".
            edges = [[s, key] for key, pond in self.state.ponds.items() for s in pond.sources]
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
        """Recent Pond Runs (newest first), optionally filtered to ``pond`` (an engine key,
        ``name@major``) and — when ``lineage`` — its upstream sources. History within a major line
        spans every version that ran on it. Ripple Runs are nested only when ``ripples`` is set."""
        with self.lock:
            params: list = []
            where = ""
            if pond is not None:
                keys = self._ancestors(pond) if lineage else {pond}
                where = f"WHERE (pn.name || '@' || pv.major) IN ({','.join('?' * len(keys))})"
                params.extend(sorted(keys))
            rows = self.db.execute(
                "SELECT pn.name, pv.major, pv.version, pr.pond_version_id, pr.f, pr.started_at, pr.finished_at, "
                "pr.status, pr.error, pr.traceback "
                "FROM pond_run pr "
                "JOIN pond_version pv ON pv.id = pr.pond_version_id "
                "JOIN pond_name pn ON pn.id = pv.pond_name_id "
                f"{where} ORDER BY pr.started_at DESC, pr.f DESC LIMIT ?",
                (*params, limit),
            ).fetchall()

            runs = []
            for pname, major, version, pv_id, f, started_at, finished_at, status, error, tb in rows:
                run = {
                    "pond": pname, "major": major, "id": pond_key(pname, major), "version": version, "f": f,
                    "started_at": started_at, "finished_at": finished_at, "status": status,
                    "error": error, "traceback": tb,
                }
                if ripples:
                    rrows = self.db.execute(
                        "SELECT r.name, rr.started_at, rr.finished_at, rr.status, rr.retry, rr.error, rr.traceback "
                        "FROM ripple_run rr JOIN ripple r ON r.id = rr.ripple_id "
                        "WHERE rr.pond_version_id = ? AND rr.f = ? "
                        "ORDER BY COALESCE(rr.finished_at, rr.started_at), rr.retry",
                        (pv_id, f),
                    ).fetchall()
                    run["ripples"] = [
                        {"ripple": rn, "started_at": rsa, "finished_at": rfa, "status": rst,
                         "retry": rt, "error": rerr, "traceback": rtb}
                        for (rn, rsa, rfa, rst, rt, rerr, rtb) in rrows
                    ]
                runs.append(run)
            return runs
