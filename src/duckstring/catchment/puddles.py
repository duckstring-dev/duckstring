"""Puddle Sources on a Catchment (plans/showcase-fixes.md item 10): a change spanning several Ponds tested on a dev
Catchment, its Ponds reading the Sources that live in production as Puddles.

**Config.** Which of a line's Sources are Puddles, and with which Puddle set, is the ``puddles.toml`` section named
for this Catchment (its identity name), sent with each deploy or by ``pond puddle push`` and stored on the line
outside the version (``pond_puddle_source``). A deploy without one clears it.

**The Puddle node.** Each Source line read as a Puddle is one engine node, ``{source}~puddle@{major}``, shared by
every line reading it and wired in place of the Source (a copy of the Source deployed here is left alone). It is
a Draw underneath (``is_draw``): its freshness is its position, which only the Catchment moves (``remote_f``), its
Run completes at once (a live Puddle moves no data), and nothing sends its demand upstream, so tests never change
what production does. The engine needs nothing new.

**Positions.** ``refresh now --hydrate --live-puddles --from PROD`` records each Puddle's baseline (its Source's
newest freshness at the hydration point, or ``--as-at``), resets the Ponds the Refresh reaches and runs them from
it; ``--rewind`` goes back to the baseline. ``--next-change|--next-run|--most-recent|--to`` move the Puddles to a
target through production's history (:mod:`duckstring.puddle_positions`, shared with ``pond run``) and refresh at
it. A plain Refresh reaching a Puddle is refused: production can't be asked to be as fresh as now.

**Reads.** A Duck reading a Puddle Source compiles its Pond's definitions to queries and runs them at its read
positions through this Catchment (``/api/duck/{name}/{major}/puddle/…``), which forwards them to production with the
credentials stored at hydration (the secret ``PUDDLE_ORIGIN``). No Duck holds production's key, and every Duck kind
reads the same way. Nothing is read in place.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ..engine import NEVER, complete_ripple
from ..trickle_io import first_row

PUDDLE_SUFFIX = "~puddle"
PUDDLE_RIPPLE = "puddle"
ORIGIN_SECRET = "PUDDLE_ORIGIN"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(f: datetime) -> str:
    return f.isoformat()


def puddle_node_name(source: str) -> str:
    return f"{source}{PUDDLE_SUFFIX}"


def parse_section(sections: dict | None, catchment: str | None) -> dict[str, str] | None:
    """This Catchment's section of a parsed ``puddles.toml`` (``{source: set}``), or ``None`` without one."""
    if not sections or not catchment or catchment not in sections:
        return None
    section = sections[catchment]
    if not isinstance(section, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in section.items()):
        raise ValueError(f"puddles.toml [{catchment}] maps each Source to a Puddle set, like transactions = \"default\"")
    return dict(section)


def check_section(pond: str, section: dict[str, str], declared, defined: dict[str, list[str]],
                  error: str | None) -> None:
    """Refuse a section naming a Source ``pond`` doesn't declare, or a set with no definitions (``defined``:
    ``{source: [sets]}`` from discovery), or one for a Pond whose definitions couldn't be loaded (``error``)."""
    from ..core import DEFAULT_PUDDLE_SET

    if not section:
        return
    if error:
        raise ValueError(f"'{pond}' reads Puddle Sources here, and its Puddle definitions couldn't be loaded: {error}")
    known = {s for sets in defined.values() for s in sets} | {DEFAULT_PUDDLE_SET}
    for source, set_name in section.items():
        if source not in declared:
            raise ValueError(f"puddles.toml names '{source}', which '{pond}' doesn't read "
                             f"(its Sources: {', '.join(sorted(declared)) or 'none'})")
        if set_name not in known:
            raise ValueError(f"puddles.toml sets '{source}' to '{set_name}', and no Puddle is in that set "
                             f"(sets defined: {', '.join(sorted(known))})")


class PuddlesMixin:
    """Puddle Sources, mixed into the Driver."""

    # ── config ──────────────────────────────────────────────────────────────

    def puddle_section(self, sections: dict | None) -> dict[str, str] | None:
        """This Catchment's section of a parsed ``puddles.toml``."""
        return parse_section(sections, self.identity().get("name"))

    def set_puddle_sources(self, key: str, section: dict[str, str] | None) -> list[dict]:
        """Store ``section`` (this Catchment's ``puddles.toml`` section) as ``key``'s Puddle Sources, replacing what
        it had; none clears them. Returns ``[{source, set}]``. Raises ``ValueError`` (see :func:`check_section`)."""
        with self.lock:
            meta = self.meta.get(key)
            if meta is None:
                raise KeyError(f"no Pond '{key}'")
            section = section or {}
            pond_id = meta["pond_id"]
            if section:
                if meta.get("is_draw") or meta.get("is_spout") or meta.get("is_app") or meta.get("dbt"):
                    raise ValueError(f"'{meta['name']}' can't read Sources as Puddles: only a Pond with Ripples can")
                declared = {r[0] for r in self.db.execute(
                    "SELECT pn.name FROM pond_to_pond ptp JOIN pond_name pn ON pn.id = ptp.source_pond_name_id "
                    "WHERE ptp.pond_id = ?", (pond_id,))}
                row = first_row(self.db.execute(
                    "SELECT puddle_sets, puddles_error FROM pond_version WHERE id = ?", (meta["version_id"],)))
                sets_json, error = row if row else (None, None)
                check_section(meta["name"], section, declared, json.loads(sets_json) if sets_json else {}, error)
            self.db.execute("DELETE FROM pond_puddle_source WHERE pond_id = ?", (pond_id,))
            self.db.executemany(
                "INSERT INTO pond_puddle_source (pond_id, source_name, puddle_set) VALUES (?, ?, ?)",
                [(pond_id, s, v) for s, v in section.items()])
            self._sync_puddle_nodes()
            self.db.commit()
            self.reload()
            return [{"source": s, "set": v} for s, v in sorted(section.items())]

    def _puddle_wiring(self) -> dict[int, dict[str, str]]:
        """``{pond_id: {source: set}}``: each line's Puddle Sources (caller holds the lock)."""
        out: dict[int, dict[str, str]] = {}
        for pond_id, source, set_name in self.db.execute(
                "SELECT pond_id, source_name, puddle_set FROM pond_puddle_source"):
            out.setdefault(pond_id, {})[source] = set_name
        return out

    def _sync_puddle_nodes(self) -> None:
        """Create a Puddle node for every Source line some line reads as a Puddle, and remove the ones no line
        reads any more (caller holds the lock and reloads)."""
        wanted = {(s, m) for s, m in self.db.execute(
            "SELECT pps.source_name, ptp.source_major FROM pond_puddle_source pps "
            "JOIN pond_to_pond ptp ON ptp.pond_id = pps.pond_id "
            "JOIN pond_name pn ON pn.id = ptp.source_pond_name_id AND pn.name = pps.source_name")}
        have = {(s, m): pid for pid, s, m in self.db.execute(
            "SELECT pu.pond_id, pu.source_name, p.major FROM puddle pu JOIN pond p ON p.id = pu.pond_id")}
        for source, major in sorted(wanted - set(have)):
            self._create_puddle_node(source, major)
        for (source, major), pid in have.items():
            if (source, major) not in wanted:
                self.db.execute("DELETE FROM puddle WHERE pond_id = ?", (pid,))
                self._destroy_draw(puddle_node_name(source), major)

    def _create_puddle_node(self, source: str, major: int) -> None:
        db = self.db
        name = puddle_node_name(source)
        db.execute("INSERT OR IGNORE INTO pond_name (name, kind) VALUES (?, 'inlet')", (name,))
        (pn_id,) = first_row(db.execute("SELECT id FROM pond_name WHERE name = ?", (name,)))
        version = f"{major}.0.0"
        db.execute("INSERT OR IGNORE INTO pond_version (pond_name_id, version, major, source_path) VALUES (?, ?, ?, ?)",
                   (pn_id, version, major, f"puddle://{source}@{major}"))
        (pv_id,) = first_row(db.execute(
            "SELECT id FROM pond_version WHERE pond_name_id = ? AND version = ?", (pn_id, version)))
        db.execute("INSERT OR IGNORE INTO ripple (pond_version_id, name) VALUES (?, ?)", (pv_id, PUDDLE_RIPPLE))
        db.execute(
            "INSERT INTO pond (pond_name_id, major, pond_version_id, is_draw, is_puddle) VALUES (?, ?, ?, 1, 1) "
            "ON CONFLICT(pond_name_id, major) DO UPDATE SET pond_version_id = excluded.pond_version_id, "
            "is_draw = 1, is_puddle = 1", (pn_id, major, pv_id))
        (pond_id,) = first_row(db.execute("SELECT id FROM pond WHERE pond_name_id = ? AND major = ?", (pn_id, major)))
        db.execute("INSERT OR IGNORE INTO puddle (pond_id, source_name) VALUES (?, ?)", (pond_id, source))

    def _load_puddles(self, wiring: dict[int, dict[str, str]]) -> None:
        """After a reload: each Puddle node's record and position, and each line's Puddle Sources."""
        by_id = {m["pond_id"]: k for k, m in self.meta.items()}
        for pond_id, source, origin, url, live, baseline, position, hydrated in self.db.execute(
                "SELECT pond_id, source_name, origin, origin_url, live, baseline_f, position_f, hydrated_at "
                "FROM puddle"):
            key = by_id.get(pond_id)
            if key is None:
                continue
            self.meta[key]["is_puddle"] = True
            self.meta[key]["puddle"] = {"source": source, "origin": origin, "origin_url": url, "live": bool(live),
                                        "baseline_f": baseline, "hydrated_at": hydrated}
            ps = self.state.pond_states[key]
            ps.remote_f = datetime.fromisoformat(position) if position else NEVER
        for pond_id, sources in wiring.items():
            key = by_id.get(pond_id)
            if key is not None:
                self.meta[key]["puddle_sources"] = dict(sources)

    def puddle_status(self, key: str) -> dict | None:
        meta = self.meta[key]
        if not meta.get("is_puddle"):
            return None
        ps = self.state.pond_states[key]
        rec = meta["puddle"]
        return {"source": rec["source"], "origin": rec["origin"], "live": rec["live"], "baseline_f": rec["baseline_f"],
                "position_f": _iso(ps.remote_f) if ps.remote_f > NEVER else None, "hydrated_at": rec["hydrated_at"]}

    # ── the engine's side ───────────────────────────────────────────────────

    def _queue_puddle_run(self, pond: str, f: datetime, now: datetime) -> None:
        """A Puddle node's Run: recorded, and completed in this ``_process`` (a live Puddle moves no data)."""
        self.db.execute("INSERT OR IGNORE INTO pond_run (pond_version_id, f, started_at, status) "
                        "VALUES (?, ?, ?, 'running')", (self.meta[pond]["version_id"], _iso(f), _iso(now)))
        self.db.commit()
        runs = self.__dict__.setdefault("_puddle_runs", [])
        if (pond, f) not in runs:
            runs.append((pond, f))

    def _complete_puddle_runs(self, now: datetime) -> bool:
        runs = self.__dict__.get("_puddle_runs") or []
        self._puddle_runs = []
        for pond, f in runs:
            eid = f"{pond}.{PUDDLE_RIPPLE}"
            rs = self.state.ripple_states.get(eid)
            if rs is None:
                continue
            rs.start_f = f
            self.state = complete_ripple(self.state, eid, now)
            self._record_ripple_run(pond, PUDDLE_RIPPLE, _iso(f), "success", started_at=_iso(now),
                                    finished_at=_iso(now))
            self._finish_pond_run(pond, _iso(f), now)
        return bool(runs)

    def _reached(self, pond: str) -> list[str]:
        """The Pond and every Pond upstream of it on this Catchment (a Refresh's reach)."""
        seen, todo = [], [pond]
        while todo:
            k = todo.pop()
            if k in seen or k not in self.state.ponds:
                continue
            seen.append(k)
            todo.extend(self.state.ponds[k].sources)
        return seen

    def _reached_puddles(self, pond: str) -> list[str]:
        return [k for k in self._reached(pond) if self.meta.get(k, {}).get("is_puddle")]

    def refuse_puddle_refresh(self, pond: str) -> None:
        """A Refresh at now that reaches a Puddle is refused: production can't be asked for data as fresh as now,
        and a Puddle never sends demand upstream, so it would wait for good (caller holds the lock)."""
        nodes = self._reached_puddles(pond)
        if nodes:
            names = ", ".join(sorted(self.meta[k]["puddle"]["source"] for k in nodes))
            raise ValueError(f"'{self.meta[pond]['name']}' reads {names} as Puddles: refresh it with --next-change, "
                             "--next-run, --most-recent or --to (after --hydrate)")

    # ── reaching production ─────────────────────────────────────────────────

    def _origin(self) -> dict | None:
        from .secrets import SecretStore

        raw = SecretStore(self.root).get(ORIGIN_SECRET)
        return json.loads(raw) if raw else None

    def _set_origin(self, origin: dict) -> None:
        from .secrets import SecretStore

        SecretStore(self.root).set(ORIGIN_SECRET, json.dumps(
            {"name": origin.get("name"), "url": origin["url"].rstrip("/"), "headers": origin.get("headers") or {}}))

    def _transport(self, origin: dict | None = None):
        from ..remote_source import HttpTransport

        origin = origin or self._origin()
        if origin is None:
            raise ValueError("no Catchment to read the Puddles from: refresh with --hydrate --from CATCHMENT")
        return HttpTransport(origin["url"], origin.get("headers"))

    def _remote(self, node: str, transport):
        from ..remote_source import RemoteSource

        meta = self.meta[node]
        return RemoteSource(meta["puddle"]["source"], meta["major"], transport)

    # ── refreshes ───────────────────────────────────────────────────────────

    def refresh_puddles(self, pond: str, *, target: str | None = None, to: datetime | None = None,
                        hydrate: bool = False, live: bool = False, as_at: datetime | None = None,
                        rewind: bool = False, origin: dict | None = None) -> datetime:
        """Refresh ``pond`` through its Puddles: take a new baseline (``hydrate``), go back to it (``rewind``), or
        move them to a target (``target``, or ``to``) and refresh at it. Returns the freshness asked for. Raises
        ``ValueError`` with what to do instead when it can't."""
        from ..puddle_positions import choose_target, reach

        if sum(map(bool, (hydrate, rewind, target is not None or to is not None))) != 1:
            raise ValueError("give one of --hydrate, --rewind, or a target (--next-change, --next-run, "
                             "--most-recent, --to)")
        with self.lock:
            if pond not in self.meta:
                raise KeyError(f"no Pond '{pond}'")
            nodes = self._reached_puddles(pond)
            if not nodes:
                raise ValueError(f"'{self.meta[pond]['name']}' reads no Puddle Source on this Catchment")
            current_origin = self._origin()
        if hydrate:
            if not live:
                raise ValueError("a copied Puddle isn't supported on a Catchment: hydrate with --live-puddles")
            if origin is None or not origin.get("url"):
                raise ValueError("--hydrate needs --from CATCHMENT: the Catchment the Puddles read")
            transport = self._transport(origin)
            point = as_at or _now()
            positions = {}
            for node in nodes:
                remote = self._remote(node, transport)
                if not remote.published():
                    raise ValueError(f"'{remote.source}@{remote.major}' has published nothing on "
                                     f"{origin.get('name') or origin['url']}: has it run?")
                positions[node] = remote.latest(point) or point
            with self.lock:
                self._set_origin(origin)
                self._rebase(pond, positions, origin=origin, hydrated=True)
                return self._refresh_at(pond, min(positions.values()))

        with self.lock:
            for node in nodes:
                rec = self.meta[node]["puddle"]
                if not rec["baseline_f"]:
                    raise ValueError(f"'{rec['source']}' hasn't been hydrated here: refresh with --hydrate "
                                     "--live-puddles --from CATCHMENT")
                if current_origin is None or rec["origin_url"] != current_origin["url"]:
                    raise ValueError(f"'{rec['source']}' was hydrated from {rec['origin'] or rec['origin_url']}, "
                                     "and the Puddles now read another Catchment: hydrate it again")
            if rewind:
                positions = {n: datetime.fromisoformat(self.meta[n]["puddle"]["baseline_f"]) for n in nodes}
                self._rebase(pond, positions)
                return self._refresh_at(pond, min(positions.values()))
            prev = self.state.pond_states[pond].end_f
            if prev <= NEVER:
                raise ValueError(f"'{self.meta[pond]['name']}' has no baseline Run to go on from: refresh with "
                                 "--rewind (or --hydrate)")
            current = {n: self.state.pond_states[n].remote_f for n in nodes}
            source_of = {n: self.meta[n]["puddle"]["source"] for n in nodes}
            own = (self.meta[pond]["name"], self.meta[pond]["major"])

        # Production's history, read without the lock.
        transport = self._transport(current_origin)
        remotes = {n: self._remote(n, transport) for n in nodes}
        by_source = {source_of[n]: n for n in nodes}

        def next_change(source: str):
            changes = remotes[by_source[source]].changes(after=prev)
            return changes[0] if changes else None

        def own_runs():
            from ..remote_source import RemoteSource

            return RemoteSource(own[0], own[1], transport).run_freshnesses(after=prev)

        def latest(source: str):
            node = by_source[source]
            return remotes[node].latest(_now()) or current[node]

        goal = choose_target(target or "next-change", prev, _now(), sources=list(by_source), next_change=next_change,
                             own_runs=own_runs, latest=latest, to=to,
                             no_runs=f"--next-run needs '{own[0]}@{own[1]}' deployed on "
                                     f"{current_origin.get('name') or current_origin['url']}")
        recorded = {source_of[n]: remotes[n].runs(after=current[n]) for n in nodes}
        moved, _ = reach(goal, {source_of[n]: current[n] for n in nodes}, recorded, live=set(by_source))
        with self.lock:
            for source, f in moved.items():
                self._move_puddle(by_source[source], f)
            return self._refresh_at(pond, goal)

    def _refresh_at(self, pond: str, t: datetime) -> datetime:
        from ..engine import refresh_now

        now = _now()
        self.state = refresh_now(self.state, pond, t)
        self._process(now)
        return t

    def _move_puddle(self, node: str, f: datetime) -> None:
        ps = self.state.pond_states[node]
        if f > ps.remote_f:
            ps.remote_f = f
            self.db.execute("UPDATE puddle SET position_f = ? WHERE pond_id = ?", (_iso(f), self.meta[node]["pond_id"]))
            self.db.commit()

    def _rebase(self, pond: str, positions: dict[str, datetime], *, origin: dict | None = None,
                hydrated: bool = False) -> None:
        """Back to a baseline: every Pond the Refresh reaches is reset (registry, published data, Run history and
        demand: what incremental Runs added goes, as a Rebuild drops it), and each Puddle is put at its position
        with no Runs. Refuses while any of them is running (caller holds the lock)."""
        reached = [k for k in self._reached(pond) if not self.meta[k].get("is_puddle")]
        for k in reached:
            ps = self.state.pond_states[k]
            if ps.start_f > ps.end_f:
                raise ValueError(f"'{self.meta[k]['name']}' is running: wait for it, or sleep it, then try again")
        # Demand first: resetting a Pond processes the engine, and what was asked before must not start a Run.
        for k in [*reached, *positions]:
            self._clear_demand(k)
        now = _iso(_now())
        for node, f in positions.items():
            meta = self.meta[node]
            ps = self.state.pond_states[node]
            ps.start_f = ps.end_f = ps.changed_f = ps.persisted_f = NEVER
            ps.is_failed = ps.is_blocked = False
            ps.failed_f = NEVER
            ps.failures = 0
            ps.runs_started = ps.runs_completed = 0
            ps.remote_f = f
            for rid, rip in self.state.ripples.items():
                if rip.pond_id == node:
                    rs = self.state.ripple_states[rid]
                    rs.start_f = rs.end_f = NEVER
                    rs.is_running = False
                    rs.demand = []
            self.db.execute("DELETE FROM ripple_run WHERE pond_version_id = ?", (meta["version_id"],))
            self.db.execute("DELETE FROM pond_run WHERE pond_version_id = ?", (meta["version_id"],))
            if hydrated:
                self.db.execute(
                    "UPDATE puddle SET origin = ?, origin_url = ?, live = 1, baseline_f = ?, position_f = ?, "
                    "hydrated_at = ? WHERE pond_id = ?",
                    (origin.get("name"), origin["url"].rstrip("/"), _iso(f), _iso(f), now, meta["pond_id"]))
                meta["puddle"].update({"origin": origin.get("name"), "origin_url": origin["url"].rstrip("/"),
                                       "live": True, "baseline_f": _iso(f), "hydrated_at": now})
            else:
                self.db.execute("UPDATE puddle SET position_f = ? WHERE pond_id = ?", (_iso(f), meta["pond_id"]))
        self.db.commit()
        for k in reached:
            m = self.meta[k]
            if not (m.get("is_draw") or m.get("is_spout") or m.get("is_app")):
                self.reset_pond(k, clear_history=True)
        self.db.commit()
        self._persist_state()

    def _clear_demand(self, key: str) -> None:
        ps = self.state.pond_states[key]
        ps.demand = []
        ps.throttle = None
        self.db.execute("DELETE FROM pond_demand WHERE pond_id = ?", (self.meta[key]["pond_id"],))

    # ── the Duck channel ────────────────────────────────────────────────────

    def _puddle_line(self, key: str, source: str, major) -> None:
        """Refuse a Duck's read of anything but one of its own line's Puddle Sources."""
        with self.lock:
            meta = self.meta.get(key)
            sources = (meta or {}).get("puddle_sources") or {}
            if source not in sources:
                raise PermissionError(f"'{key}' doesn't read '{source}' as a Puddle")
            wired = [s for s in self.state.ponds[key].sources
                     if self.meta.get(s, {}).get("puddle", {}).get("source") == source]
            if not wired or int(major) != self.meta[wired[0]]["major"]:
                raise PermissionError(f"'{key}' reads another line of '{source}'")

    def puddle_query(self, key: str, payload: dict) -> tuple[bytes, str | None]:
        """A Duck's query of one of its Puddle Sources, forwarded to production (the Duck channel)."""
        self._puddle_line(key, str(payload.get("pond")), payload.get("major"))
        allowed = {k: payload[k] for k in ("pond", "major", "sql", "format", "at", "since") if k in payload}
        return self._transport().query(allowed)

    def puddle_get(self, key: str, path: str, params: dict | None) -> dict:
        """A Duck's metadata read of one of its Puddle Sources (its tables), forwarded to production."""
        params = dict(params or {})
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "ponds"] and parts[3] in ("tables", "changes"):
            source = parts[2]
        elif path == "/api/runs":
            source = str(params.get("pond"))
        else:
            raise PermissionError(f"a Duck can't read {path} through its Puddles")
        self._puddle_line(key, source, params.get("major"))
        return self._transport().get(path, params)


__all__ = ["PuddlesMixin", "PUDDLE_SUFFIX", "ORIGIN_SECRET", "puddle_node_name", "parse_section", "check_section"]
