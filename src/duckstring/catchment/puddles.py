"""Puddles on a Catchment (plans/showcase-fixes.md item 10): a change spanning several Ponds tested on a dev
Catchment, its Ponds reading the Sources that live in production as Puddles.

**A Puddle is the Source line.** ``transactions@1`` on a dev Catchment is either a deployed Pond or a Puddle, and
every Pond there reading it reads whichever it is. A Puddle is deployed from a reading Pond's definitions
(``pond puddle deploy``, ``POST /api/puddles``): for each Source named, or by default each one not yet on the
Catchment. Naming one that is already a Puddle replaces it once confirmed (``force``); one deployed as a Pond is
refused, and a Pond deployed over a Puddle replaces it once confirmed (``routes/deploy.py``).

**Building it** runs its definitions in a subprocess, in the environment of the Pond that deployed it
(:mod:`duckstring.puddle_build`), as ``pond hydrate`` does locally. That records its baseline: its Source's freshness
at the hydration point. A **copied** Puddle holds its own data, written by its definitions alone or copied from
production (``--from``, with every change after ``--as-at``), and published where a Draw lands, so a reading Duck
reads it like any Source. A **live** one holds none: its definitions compiled to queries when it was built, and a
reading Duck runs them on production through this Catchment (``/api/duck/{name}/{major}/puddle/…``), which forwards
them with the credentials stored when it was built (a secret named for production's address). No Duck holds
production's key, and nothing is read in place.

**The line.** A ``pond`` row like a Draw's (``is_draw``, ``is_puddle``): its freshness is its position, which only
this Catchment moves (``remote_f``); its Run completes at once; it never sends demand anywhere, so tests never change
what production does. ``refresh now --rewind`` resets the Ponds a Refresh reaches and runs them from the baseline
(the first Run after a deploy too); ``--hydrate`` builds a new baseline with the same definitions; ``--next-change
|--next-run|--most-recent|--to`` move the Puddles through their history (:mod:`duckstring.puddle_positions`,
shared with ``pond run``), running a copied Puddle's steps past its end. A plain Refresh reaching a Puddle is
refused: production can't be asked to be as fresh as now.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..engine import NEVER, complete_ripple
from ..keys import pond_key
from ..trickle_io import first_row

PUDDLE_RIPPLE = "puddle"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(f: datetime) -> str:
    return f.isoformat()


def _dt(iso: str | None) -> datetime | None:
    return datetime.fromisoformat(iso) if iso else None


def puddle_version(major: int) -> str:
    """A Puddle line's version: no Pond's, and outside every ``min_version`` check (``routes/deploy.py``)."""
    return f"{major}.0.0-puddle"


def puddle_dir(root, name: str, major: int) -> Path:
    """A Puddle's own files: ``code/`` (the deploying Pond's bundle), ``baseline/`` (a copy as built) and ``work/``
    (the copy as stepped, which is what's published)."""
    return Path(root) / "puddles" / name / f"m{major}"


def origin_secret(url: str) -> str:
    """The secret holding the credentials for the Catchment at ``url``, shared by every Puddle reading it."""
    return "PUDDLE_ORIGIN_" + hashlib.sha256(url.rstrip("/").encode()).hexdigest()[:16]


class PuddleConflict(ValueError):
    """A deploy would replace Puddles: confirmed with ``force``."""

    def __init__(self, replace: list[str], owners: dict[str, str]):
        self.replace = replace
        listed = ", ".join(f"{k} (from {owners[k]}'s definitions)" for k in replace)
        super().__init__(f"{listed} {'is a Puddle' if len(replace) == 1 else 'are Puddles'} here already: "
                         "deploying replaces it once confirmed (--force)")


def _sync_local(src: Path, dest: Path) -> None:
    """``dest`` holding every file of ``src``, as hard links where the filesystem allows: a file that changed (the
    sidecar) is replaced, one already there is left."""
    import os

    for path in src.rglob("*"):
        if not path.is_file():
            continue
        target = dest / path.relative_to(src)
        if target.exists() and os.path.samefile(path, target):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.puddle-tmp")
        tmp.unlink(missing_ok=True)
        try:
            os.link(path, tmp)
        except OSError:
            shutil.copy2(path, tmp)
        tmp.replace(target)


class PuddlesMixin:
    """Puddles, mixed into the Driver."""

    # ── deploying ───────────────────────────────────────────────────────────

    def puddle_plan(self, owner: str, declared: dict[str, int], named: list[str] | None,
                    force: bool = False, own_major: int | None = None) -> tuple[list[tuple[str, int]], list[dict]]:
        """Which Sources a Puddle deploy from ``owner`` (its Sources ``{name: major}``; its own major line
        ``own_major``) builds, and which it leaves: the ones ``named`` (the Pond's own name too: a self-Puddle, built
        from its definitions against itself), or each Source not yet on this Catchment. Raises ``ValueError`` for a
        Source it doesn't read or one deployed here as a Pond, and :class:`PuddleConflict` for a Puddle already
        here, unless ``force``. Whichever deploys first wins: a default deploy never replaces anything."""
        with self.lock:
            build, skipped, replace = [], [], []
            if named:
                lines = dict(declared)
                if own_major is not None:
                    lines[owner] = own_major
                for source in dict.fromkeys(named):
                    if source not in lines:
                        raise ValueError(f"'{owner}' doesn't read '{source}' (its Sources: "
                                         f"{', '.join(sorted(declared)) or 'none'})")
                    key = pond_key(source, lines[source])
                    meta = self.meta.get(key)
                    if meta is not None and not meta.get("is_puddle"):
                        raise ValueError(f"'{key}' is deployed here as a Pond: a Puddle can't replace it (remove it "
                                         "first to test against a Puddle)")
                    if meta is not None and not force:
                        replace.append(key)
                    build.append((source, lines[source]))
                if replace:
                    raise PuddleConflict(replace, {k: self.meta[k]["puddle"]["owner"] for k in replace})
                return build, skipped
            for source, major in sorted(declared.items()):
                meta = self.meta.get(pond_key(source, major))
                if meta is None:
                    build.append((source, major))
                else:
                    skipped.append({"source": source, "major": major,
                                    "as": "puddle" if meta.get("is_puddle") else "pond"})
            return build, skipped

    def install_puddles(self, owner: str, code: Path, built: list[dict], origin: dict | None) -> list[dict]:
        """Make each of ``built`` (``{source, major, set, live, as_at, out, result}``: a build's output, see
        :mod:`duckstring.puddle_build`) the Puddle line on this Catchment, replacing any Puddle there. ``code`` is
        the deploying Pond's bundle, kept beside each so it can be built again. Returns their status."""
        with self.lock:
            if origin is not None:
                self._set_origin(origin)
            now = _iso(_now())
            for b in built:
                source, major = b["source"], b["major"]
                key = pond_key(source, major)
                if key in self.meta:
                    meta = self.meta[key]
                    if not meta.get("is_puddle"):
                        raise ValueError(f"'{key}' is deployed here as a Pond: a Puddle can't replace it")
                    self._destroy_puddle(source, major)
                d = puddle_dir(self.root, source, major)
                shutil.rmtree(d, ignore_errors=True)
                d.mkdir(parents=True)
                shutil.copytree(code, d / "code", symlinks=True)
                if Path(b["out"]).exists():
                    shutil.move(str(b["out"]), str(d / "baseline"))
                self._create_puddle_line(source, major)
                state = b["result"]["state"]
                (pond_id,) = first_row(self.db.execute(
                    "SELECT p.id FROM pond p JOIN pond_name pn ON pn.id = p.pond_name_id "
                    "WHERE pn.name = ? AND p.major = ?", (source, major)))
                self.db.execute(
                    "INSERT INTO puddle (pond_id, owner, puddle_set, live, mode, origin, origin_url, as_at, "
                    "baseline_f, position_f, history, runs, steps, step, queries, kinds, hydrated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                    (pond_id, owner, b["set"], int(bool(b["live"])), state["mode"],
                     (origin or {}).get("name"), (origin or {}).get("url", "").rstrip("/") or None, b.get("as_at"),
                     state["f"], state["f"], int(bool(state.get("history"))),
                     json.dumps(state["runs"]) if state.get("runs") is not None else None,
                     int(bool(state.get("steps"))), json.dumps(b["result"].get("queries")),
                     json.dumps(b["result"].get("kinds")), now))
            self.db.commit()
            self.reload()
            for b in built:
                key = pond_key(b["source"], b["major"])
                self._install_data(key, reset=True)
            self._persist_state()
            return [self.puddle_status(pond_key(b["source"], b["major"])) for b in built]

    def _create_puddle_line(self, name: str, major: int) -> None:
        db = self.db
        db.execute("INSERT OR IGNORE INTO pond_name (name, kind) VALUES (?, 'inlet')", (name,))
        (pn_id,) = first_row(db.execute("SELECT id FROM pond_name WHERE name = ?", (name,)))
        version = puddle_version(major)
        db.execute("INSERT OR IGNORE INTO pond_version (pond_name_id, version, major, source_path) VALUES (?, ?, ?, ?)",
                   (pn_id, version, major, str(puddle_dir(self.root, name, major).relative_to(self.root) / "code")))
        (pv_id,) = first_row(db.execute(
            "SELECT id FROM pond_version WHERE pond_name_id = ? AND version = ?", (pn_id, version)))
        db.execute("INSERT OR IGNORE INTO ripple (pond_version_id, name) VALUES (?, ?)", (pv_id, PUDDLE_RIPPLE))
        db.execute("INSERT INTO pond (pond_name_id, major, pond_version_id, is_draw, is_puddle) VALUES (?, ?, ?, 1, 1)",
                   (pn_id, major, pv_id))

    def _destroy_puddle(self, name: str, major: int) -> None:
        """A Puddle line's rows and files (caller holds the lock and reloads)."""
        from .registry import landing_root, pond_data_dir, pond_major_dir

        key = pond_key(name, major)
        meta = self.meta.get(key)
        if meta is not None:
            self.db.execute("DELETE FROM puddle WHERE pond_id = ?", (meta["pond_id"],))
        self._destroy_draw(name, major)
        shutil.rmtree(puddle_dir(self.root, name, major), ignore_errors=True)
        shutil.rmtree(pond_major_dir(Path(self.root), name, major), ignore_errors=True)
        if landing_root(self.data_root) is not None:
            pond_data_dir(Path(self.root), name, major, landing_root(self.data_root)).rmtree()

    def remove_puddle(self, key: str) -> dict:
        """Remove the Puddle line ``key``: the Ponds reading it block on a missing Source until it or the Pond is
        deployed again."""
        with self.lock:
            meta = self.meta.get(key)
            if meta is None or not meta.get("is_puddle"):
                raise ValueError(f"no Puddle '{key}'")
            readers = sorted(self.meta[k]["name"] for k, p in self.state.ponds.items() if key in p.sources)
            self._destroy_puddle(meta["name"], meta["major"])
            self.db.commit()
            self.reload()
            return {"removed": key, "now_blocked": readers, "spouts_removed": []}

    def _install_data(self, key: str, *, reset: bool) -> None:
        """Publish a copied Puddle's working copy where a Draw would land (caller holds the lock). ``reset`` goes
        back to the baseline first, dropping what steps wrote."""
        from ..dataplane import persist_tree
        from ..storage import LocalStorage
        from .registry import landing_root, pond_data_dir

        meta = self.meta[key]
        if meta["puddle"]["mode"] != "copied":
            return
        d = puddle_dir(self.root, meta["name"], meta["major"])
        work = d / "work"
        if reset:
            from ..local.sources import link_tree

            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True)
            if (d / "baseline").is_dir():
                link_tree(d / "baseline", work)
        landing = landing_root(self.data_root)
        dest = pond_data_dir(Path(self.root), meta["name"], meta["major"], landing)
        if reset:
            dest.rmtree()
        if landing is None:
            _sync_local(work, dest.root)
        else:
            persist_tree(LocalStorage(work), dest)

    # ── loading and status ──────────────────────────────────────────────────

    def _load_puddles(self) -> None:
        """After a reload: each Puddle line's record and position."""
        by_id = {m["pond_id"]: k for k, m in self.meta.items()}
        cols = ("owner", "set", "live", "mode", "origin", "origin_url", "as_at", "baseline_f", "position_f", "history",
                "runs", "steps", "step", "queries", "kinds", "hydrated_at")
        for row in self.db.execute(
                "SELECT pond_id, owner, puddle_set, live, mode, origin, origin_url, as_at, baseline_f, position_f, "
                "history, runs, steps, step, queries, kinds, hydrated_at FROM puddle"):
            key = by_id.get(row[0])
            if key is None:
                continue
            rec = dict(zip(cols, row[1:], strict=True))
            for k in ("live", "history", "steps"):
                rec[k] = bool(rec[k])
            for k in ("runs", "queries", "kinds"):
                rec[k] = json.loads(rec[k]) if rec[k] else None
            self.meta[key]["is_puddle"] = True
            self.meta[key]["puddle"] = rec
            position = rec.pop("position_f")
            self.state.pond_states[key].remote_f = _dt(position) or NEVER

    def puddle_status(self, key: str) -> dict | None:
        meta = self.meta.get(key)
        if meta is None or not meta.get("is_puddle"):
            return None
        rec = meta["puddle"]
        ps = self.state.pond_states[key]
        return {"source": meta["name"], "major": meta["major"], "owner": rec["owner"], "set": rec["set"],
                "mode": rec["mode"], "origin": rec["origin"], "as_at": rec["as_at"], "baseline_f": rec["baseline_f"],
                "position_f": _iso(ps.remote_f) if ps.remote_f > NEVER else None, "history": rec["history"],
                "steps": rec["steps"], "step": rec["step"], "hydrated_at": rec["hydrated_at"]}

    def list_puddles(self) -> list[dict]:
        with self.lock:
            return [self.puddle_status(k) for k in sorted(self.meta) if self.meta[k].get("is_puddle")]

    def live_puddles_of(self, pond: str) -> dict | None:
        """The live Puddles ``pond`` reads, for its Duck's job: ``{name: {major, queries, kinds}}``."""
        out = {}
        for skey in self.state.ponds[pond].sources:
            meta = self.meta.get(skey, {})
            if meta.get("is_puddle") and meta["puddle"]["mode"] == "live":
                rec = meta["puddle"]
                out[meta["name"]] = {"major": meta["major"], "queries": rec["queries"] or {},
                                     "kinds": rec["kinds"] or {}}
        return out or None

    # ── the engine's side ───────────────────────────────────────────────────

    def _queue_puddle_run(self, pond: str, f: datetime, now: datetime) -> None:
        """A Puddle's Run: recorded, and completed in this ``_process`` (its data is already in place)."""
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
        and a Puddle never sends demand on, so it would wait for good (caller holds the lock)."""
        nodes = self._reached_puddles(pond)
        if nodes:
            names = ", ".join(sorted(nodes))
            raise ValueError(f"'{self.meta[pond]['name']}' reads {names} as {'a Puddle' if len(nodes) == 1 else 'Puddles'}"
                             ": refresh it with --rewind to run from the baseline, then --next-change, --next-run, "
                             "--most-recent or --to")

    # ── reaching production ─────────────────────────────────────────────────

    def _set_origin(self, origin: dict) -> None:
        from .secrets import SecretStore

        url = origin["url"].rstrip("/")
        SecretStore(self.root).set(origin_secret(url), json.dumps(
            {"name": origin.get("name"), "url": url, "headers": origin.get("headers") or {}}))

    def _origin_of(self, key: str) -> dict | None:
        """The Catchment the Puddle ``key`` reads, with its stored credentials; ``None`` for one that reads none."""
        from .secrets import SecretStore

        url = self.meta[key]["puddle"]["origin_url"]
        if not url:
            return None
        raw = SecretStore(self.root).get(origin_secret(url))
        return json.loads(raw) if raw else {"name": self.meta[key]["puddle"]["origin"], "url": url, "headers": {}}

    def _transport(self, key: str):
        from ..remote_source import HttpTransport

        origin = self._origin_of(key)
        if origin is None:
            raise ValueError(f"'{key}' reads no Catchment")
        return HttpTransport(origin["url"], origin.get("headers"))

    def _remote(self, key: str):
        from ..remote_source import RemoteSource

        meta = self.meta[key]
        return RemoteSource(meta["name"], meta["major"], self._transport(key))

    # ── building ────────────────────────────────────────────────────────────

    def _python_for_puddle(self, key: str) -> str:
        from ..environments import ensure_env

        meta = self.meta[key]
        return str(ensure_env(Path(self.root), puddle_dir(self.root, meta["name"], meta["major"]) / "code",
                              check_lock=False))

    def _build_spec(self, key: str, op: str, out: Path, **extra) -> dict:
        meta = self.meta[key]
        rec = meta["puddle"]
        return {"op": op, "project_dir": str(puddle_dir(self.root, meta["name"], meta["major"]) / "code"),
                "source": meta["name"], "set": rec["set"], "out": str(out), **extra}

    # ── refreshes ───────────────────────────────────────────────────────────

    def refresh_puddles(self, pond: str, *, target: str | None = None, to: datetime | None = None,
                        hydrate: bool = False, as_at: datetime | None = None, rewind: bool = False,
                        origin: dict | None = None) -> datetime:
        """Refresh ``pond`` through its Puddles: build a new baseline with their definitions (``hydrate``, as of
        ``as_at``, reading ``origin`` instead of the Catchment each was built from), go back to the baseline
        (``rewind``), or move them to a target (``target``, or ``to``) and refresh at it. Returns the freshness
        asked for. Raises ``ValueError`` with what to do instead when it can't."""
        if sum(map(bool, (hydrate, rewind, target is not None or to is not None))) != 1:
            raise ValueError("give one of --hydrate, --rewind, or a target (--next-change, --next-run, "
                             "--most-recent, --to)")
        with self.lock:
            if pond not in self.meta:
                raise KeyError(f"no Pond '{pond}'")
            nodes = self._reached_puddles(pond)
            if not nodes:
                raise ValueError(f"'{self.meta[pond]['name']}' reads no Puddle on this Catchment")
            if hydrate or rewind:
                self._refuse_running(pond)
        if hydrate:
            return self._hydrate(pond, nodes, as_at, origin)
        if rewind:
            with self.lock:
                positions = {n: datetime.fromisoformat(self.meta[n]["puddle"]["baseline_f"]) for n in nodes}
                for n in nodes:
                    self.db.execute("UPDATE puddle SET step = 0 WHERE pond_id = ?", (self.meta[n]["pond_id"],))
                    self.meta[n]["puddle"]["step"] = 0
                    self._install_data(n, reset=True)
                self._rebase(pond, positions)
                return self._refresh_at(pond, min(positions.values()))
        return self._refresh_to_target(pond, nodes, target, to)

    def _hydrate(self, pond: str, nodes: list[str], as_at: datetime | None, origin: dict | None) -> datetime:
        import tempfile

        from ..puddle_build import PuddleBuildError, run_build

        with self.lock:
            specs = {}
            for n in nodes:
                from_ = origin if origin is not None and self.meta[n]["puddle"]["origin_url"] else self._origin_of(n)
                specs[n] = {"from": from_, "python": None}
            stage = Path(tempfile.mkdtemp(prefix=".hydrate-", dir=self._puddles_root()))
        try:
            results = {}
            for n in nodes:  # the definitions run outside the lock
                python = self._python_for_puddle(n)
                out = stage / self.meta[n]["name"]
                rec = self.meta[n]["puddle"]
                from_ = specs[n]["from"]
                spec = self._build_spec(n, "build", out, live=rec["live"],
                                        as_at=_iso(as_at) if as_at is not None else None,
                                        origin={"url": from_["url"], "headers": from_.get("headers") or {}}
                                        if from_ else None)
                try:
                    results[n] = (out, run_build(python, spec), from_)
                except PuddleBuildError as exc:
                    raise ValueError(f"'{n}' couldn't be built again: {exc}\n{exc.detail}".rstrip()) from None
            with self.lock:
                self._refuse_running(pond)
                if origin is not None:
                    self._set_origin(origin)
                now = _iso(_now())
                positions = {}
                for n, (out, result, from_) in results.items():
                    meta = self.meta[n]
                    d = puddle_dir(self.root, meta["name"], meta["major"])
                    shutil.rmtree(d / "baseline", ignore_errors=True)
                    if out.exists():
                        shutil.move(str(out), str(d / "baseline"))
                    state = result["state"]
                    self.db.execute(
                        "UPDATE puddle SET mode = ?, origin = ?, origin_url = ?, as_at = ?, baseline_f = ?, "
                        "position_f = ?, history = ?, runs = ?, steps = ?, step = 0, queries = ?, kinds = ?, "
                        "hydrated_at = ? WHERE pond_id = ?",
                        (state["mode"], (from_ or {}).get("name"), (from_ or {}).get("url", "").rstrip("/") or None,
                         _iso(as_at) if as_at is not None else None, state["f"], state["f"],
                         int(bool(state.get("history"))),
                         json.dumps(state["runs"]) if state.get("runs") is not None else None,
                         int(bool(state.get("steps"))), json.dumps(result.get("queries")),
                         json.dumps(result.get("kinds")), now, meta["pond_id"]))
                    positions[n] = datetime.fromisoformat(state["f"])
                self.db.commit()
                self._load_puddles()
                for n in nodes:
                    self._install_data(n, reset=True)
                self._rebase(pond, positions)
                return self._refresh_at(pond, min(positions.values()))
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _puddles_root(self) -> Path:
        path = Path(self.root) / "puddles"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _refresh_to_target(self, pond: str, nodes: list[str], target: str | None, to: datetime | None) -> datetime:
        from ..dataplane import change_points
        from ..puddle_positions import choose_target, reach

        with self.lock:
            prev = self.state.pond_states[pond].end_f
            if prev <= NEVER:
                raise ValueError(f"'{self.meta[pond]['name']}' has no baseline Run to go on from: refresh it with "
                                 "--rewind (or --hydrate)")
            current = {n: self.state.pond_states[n].remote_f for n in nodes}
            recs = {n: dict(self.meta[n]["puddle"]) for n in nodes}
            own = (self.meta[pond]["name"], self.meta[pond]["major"])
            live = {n for n in nodes if recs[n]["mode"] == "live"}
            remotes = {n: self._remote(n) for n in live}
            reading = [n for n in nodes if recs[n]["origin_url"]]
            own_transport = self._transport(reading[0]) if reading else None
            baselines = {n: puddle_dir(self.root, self.meta[n]["name"], self.meta[n]["major"]) / "baseline"
                         for n in nodes}

        # Production's history and the copies' own, read without the lock.
        def recorded(n: str) -> list[datetime]:
            if n in live:
                return remotes[n].runs(after=current[n])
            if not recs[n]["history"]:
                return []
            return [datetime.fromisoformat(r) for r in recs[n]["runs"] or []]

        def next_change(n: str):
            if n in live:
                changes = remotes[n].changes(after=prev)
            elif recs[n]["history"]:
                changes = change_points(baselines[n], after=prev)
            else:
                changes = []
            return changes[0] if changes else None

        def own_runs():
            if own_transport is None:
                return None
            from ..remote_source import RemoteSource

            return RemoteSource(own[0], own[1], own_transport).run_freshnesses(after=prev)

        history = {n: recorded(n) for n in nodes}

        def latest(n: str):
            return max([current[n], *history[n]])

        goal = choose_target(target or "next-change", prev, _now(), sources=nodes, next_change=next_change,
                             own_runs=own_runs, latest=latest, to=to,
                             no_runs=f"--next-run needs '{own[0]}@{own[1]}' deployed on the Catchment the Puddles read"
                                     + ("" if reading else ", and these Puddles read none"))
        moved, stepping = reach(goal, current, history, live=live,
                                steppable={n for n in nodes if recs[n]["mode"] == "copied" and recs[n]["steps"]})
        for n in stepping:  # their steps run outside the lock
            self._step(n, goal, current[n], recs[n])
        with self.lock:
            for n in stepping:
                self._install_data(n, reset=False)
            for n, f in moved.items():
                self._move_puddle(n, f)
            return self._refresh_at(pond, goal)

    def _step(self, key: str, f: datetime, previous: datetime, rec: dict) -> None:
        from ..puddle_build import PuddleBuildError, run_build

        meta = self.meta[key]
        work = puddle_dir(self.root, meta["name"], meta["major"]) / "work"
        step = rec["step"] + 1
        spec = self._build_spec(key, "step", work, f=_iso(f), previous_f=_iso(previous), step=step,
                                retain_from=rec["baseline_f"])
        try:
            run_build(self._python_for_puddle(key), spec)
        except PuddleBuildError as exc:
            raise ValueError(f"'{key}': its step failed: {exc}\n{exc.detail}".rstrip()) from None
        with self.lock:
            self.db.execute("UPDATE puddle SET step = ? WHERE pond_id = ?", (step, meta["pond_id"]))
            self.db.commit()
            meta["puddle"]["step"] = step

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

    def _refuse_running(self, pond: str) -> None:
        for k in self._reached(pond):
            ps = self.state.pond_states[k]
            if not self.meta[k].get("is_puddle") and ps.start_f > ps.end_f:
                raise ValueError(f"'{self.meta[k]['name']}' is running: wait for it, or sleep it, then try again")

    def _rebase(self, pond: str, positions: dict[str, datetime]) -> None:
        """Back to a baseline: every Pond the Refresh reaches is reset (registry, published data, Run history and
        demand: what incremental Runs added goes, as a Rebuild drops it), and each Puddle is put at its position
        with no Runs. Refuses while any of them is running (caller holds the lock)."""
        self._refuse_running(pond)
        reached = [k for k in self._reached(pond) if not self.meta[k].get("is_puddle")]
        # Demand first: resetting a Pond processes the engine, and what was asked before must not start a Run.
        for k in [*reached, *positions]:
            self._clear_demand(k)
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

    def _puddle_line(self, key: str, source: str, major) -> str:
        """The live Puddle ``source@major`` that the line ``key`` reads; anything else is refused."""
        with self.lock:
            try:
                skey = pond_key(source, int(major))
            except (TypeError, ValueError):
                raise PermissionError(f"'{key}' doesn't read '{source}' as a live Puddle") from None
            pond = self.state.ponds.get(key)
            meta = self.meta.get(skey, {})
            if pond is None or skey not in pond.sources or not meta.get("is_puddle") \
                    or meta["puddle"]["mode"] != "live":
                raise PermissionError(f"'{key}' doesn't read '{skey}' as a live Puddle")
            return skey

    def puddle_query(self, key: str, payload: dict) -> tuple[bytes, str | None]:
        """A Duck's query of one of its live Puddles, forwarded to production (the Duck channel)."""
        skey = self._puddle_line(key, str(payload.get("pond")), payload.get("major"))
        allowed = {k: payload[k] for k in ("pond", "major", "sql", "format", "at", "since") if k in payload}
        return self._transport(skey).query(allowed)

    def puddle_get(self, key: str, path: str, params: dict | None) -> dict:
        """A Duck's metadata read of one of its live Puddles (its tables), forwarded to production."""
        params = dict(params or {})
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "ponds"] and parts[3] in ("tables", "changes"):
            source = parts[2]
        elif path == "/api/runs":
            source = str(params.get("pond"))
        else:
            raise PermissionError(f"a Duck can't read {path} through its Puddles")
        skey = self._puddle_line(key, source, params.get("major"))
        return self._transport(skey).get(path, params)


__all__ = ["PuddlesMixin", "PuddleConflict", "PUDDLE_RIPPLE", "puddle_version", "puddle_dir", "origin_secret"]
