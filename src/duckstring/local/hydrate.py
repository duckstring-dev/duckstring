from __future__ import annotations

import shutil
import time
import traceback as tb
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..core import DEFAULT_PUDDLE_SET, Puddle, collect_puddles, import_pond_module, parse_ref
from .project import Project
from .session import SourceSession
from .sources import Locator, SourceUnreachable, change_points, copy_published, earliest_readable
from .state import Hydrated, SourceState, _iso, load_hydrated, local_sets, save_hydrated


@dataclass
class HydrateResult:
    target: str
    name: str  # the puddle function's name
    status: str  # "ok" | "failed"
    duration_s: float = 0.0
    error: str | None = None
    traceback: str | None = None
    note: str | None = None  # how the Source was hydrated: live, copied with history, …


def _source_of(target: str) -> str:
    """The Source a Puddle target names: ``"source"`` or ``"source.table"`` (see :func:`parse_ref`)."""
    source, name = parse_ref(target)
    return name if source is None else source


def collect_definitions(project: Project) -> list[dict]:
    """Import the puddles entrypoint and return its ``@puddle`` and ``@puddle.step`` registrations, validated
    against the Pond's declared Sources (the Pond's own name is also a valid target — the incremental seed)."""
    entry = project.dir / project.puddles_entry
    if not entry.exists():
        return []
    try:
        import_pond_module(project.dir, project.puddles_entry)
    except Exception:
        collect_puddles()
        raise
    definitions = collect_puddles()

    allowed = set(project.sources) | {project.name}
    for d in definitions:
        source = _source_of(d["target"])
        if source not in allowed:
            declared = ", ".join(sorted(allowed))
            raise ValueError(
                f"puddle '{d['target']}' ({d['name']}) targets a Source this Pond does not declare — "
                f"declared: {declared}"
            )
    return definitions


def select_definitions(definitions: list[dict], source: str, set_name: str, *, step: bool = False) -> list[dict]:
    """A Source's definitions (or steps) in ``set_name``: per target, the set's where it has one and the default
    set's otherwise; a whole-Source definition in the set replaces all of the Source's default ones."""
    mine = [d for d in definitions if d.get("step", False) == step and _source_of(d["target"]) == source]
    default = [d for d in mine if DEFAULT_PUDDLE_SET in d.get("sets", (DEFAULT_PUDDLE_SET,))]
    if set_name == DEFAULT_PUDDLE_SET:
        return default
    in_set = [d for d in mine if set_name in d.get("sets", ())]
    if any(parse_ref(d["target"])[0] is None for d in in_set):
        return in_set
    chosen = {d["target"]: d for d in default}
    chosen.update({d["target"]: d for d in in_set})
    return list(chosen.values())


def chosen_sets(project: Project, definitions: list[dict], override: str | None) -> dict[str, str]:
    """``{source: set}`` for every Source and the Pond's own name: ``override`` for all, else ``puddles.toml``'s
    ``[local]`` section, else the default set. Raises ``ValueError`` for an unknown Source or set."""
    known = {s for d in definitions for s in d.get("sets", (DEFAULT_PUDDLE_SET,))} | {DEFAULT_PUDDLE_SET}
    targets = [*project.sources, project.name]
    if override is not None:
        if override not in known:
            raise ValueError(f"no Puddle set '{override}' — sets defined: {', '.join(sorted(known))}")
        return {s: override for s in targets}
    sets = local_sets(project)
    for source, name in sets.items():
        if source not in targets:
            raise ValueError(f"puddles.toml [local] names '{source}', which this Pond doesn't read")
        if name not in known:
            raise ValueError(f"puddles.toml [local] sets '{source}' to '{name}', and no Puddle is in that set")
    return {s: sets.get(s, DEFAULT_PUDDLE_SET) for s in targets}


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class _Hydration:
    """One ``pond hydrate``."""

    def __init__(self, project: Project, catchment: str | None, *, live: bool, as_at: datetime | None):
        self.project = project
        self.catchment = catchment
        self.live = live
        self.as_at = _utc(as_at) if as_at is not None else None
        self.point = self.as_at or datetime.now(timezone.utc)
        self.locator = Locator(project, catchment)
        self.results: list[HydrateResult] = []
        self.warnings: list[str] = []

    # ── a Source's freshness at the point ───────────────────────────────────

    def freshness_at(self, source: str, store) -> datetime:
        """The Source's newest run freshness at or before the point (a dimension that runs hourly but changes
        weekly is that fresh), else its newest change point, else the point itself."""
        f = self.locator.latest_run(source, self.point)
        if f is not None:
            return f
        changes = change_points(store, upto=self.point)
        self.warnings.append(f"couldn't read '{source}''s run history from the Catchment — using its change points")
        return changes[-1] if changes else self.point

    def runs_after(self, source: str, after: datetime) -> list[str] | None:
        runs = self.locator.runs(source, after=after)
        if runs is None:
            self.warnings.append(
                f"couldn't read '{source}''s run history from the Catchment — incremental runs will use its "
                "change points, so a Source that runs without changing holds the others back"
            )
            return None
        return [_iso(f) for f in runs]

    # ── running definitions ─────────────────────────────────────────────────

    def evaluate(self, source: str, defs: list[dict], path: Path, f: datetime, *, previous_f=None, step: int = 0,
                 read_at=None, resolve=None, retain_from=None, record: bool = True) -> SourceSession | None:
        """Run ``defs`` for ``source`` into ``path`` at ``f`` and publish. ``resolve`` (with ``p.read_source``) moves the
        stamp to the Source's own freshness at the point before anything is written. Returns the session, or
        ``None`` if a definition failed (recorded as a result)."""
        session = SourceSession(self.project, source, path, f, previous_f=previous_f, step=step,
                                locator=self.locator, read_at=read_at, retain_from=retain_from)
        session.resolve_point = resolve
        ok = True
        try:
            for d in defs:
                started = time.perf_counter()
                try:
                    p = Puddle(d["target"], root=self.project.puddles_dir, default_catchment=self.catchment,
                               session=session)
                    returned = d["func"](p)
                    if returned is not None:
                        if isinstance(returned, (str, Path)):
                            p.write_path(returned)
                        else:
                            p.write_table(returned)
                    if record:
                        self.results.append(HydrateResult(d["target"], d["name"], "ok", time.perf_counter() - started))
                except Exception as exc:  # noqa: BLE001 - reported per definition
                    ok = False
                    self.results.append(HydrateResult(
                        d["target"], d["name"], "failed", time.perf_counter() - started,
                        error=f"{type(exc).__name__}: {exc}", traceback=tb.format_exc()))
            if ok:
                session.publish()
        finally:
            session.close()
        return session if ok else None

    # ── per Source ──────────────────────────────────────────────────────────

    def hydrate_source(self, source: str, defs: list[dict], steps: list[dict], set_name: str,
                       from_catchment: bool) -> SourceState | None:
        path = self.project.snapshot_dir(source)
        shutil.rmtree(path.parent, ignore_errors=True)
        own = source == self.project.name
        base = {"set": set_name, "steps": bool(steps), "major": self.locator.major(source)}

        if not defs:
            if own:
                return None
            if self.live:
                return self._live(source, base)
            if self.as_at is not None:
                return self._copy(source, base)
            if from_catchment:
                return self._from_catchment(source, base)
            if steps:  # generated by its steps alone
                return SourceState("copied", f=_iso(self.point), **base)
            self.warnings.append(f"source '{source}' has no puddle definition — skipped (pass --live, --as-at or "
                                 "--from-catchment to read it from the Catchment)")
            return None

        point: dict = {}

        def resolve():  # the first p.read_source: stamp the Puddle at the Source's own freshness at the point
            store = self.locator.storage(source)
            point["f"] = self.freshness_at(source, store)
            return point["f"]

        session = self.evaluate(source, defs, path, self.point, read_at=self.point, resolve=resolve)
        if session is None:
            return None
        f0 = point.get("f", self.point)
        if not session.used_source:
            if self.live or self.as_at is not None:
                self.warnings.append(f"'{source}': its definition doesn't read the Source (p.read_source), so it was "
                                     "copied as written")
            return SourceState("copied", f=_iso(f0), **base)
        if own:
            return SourceState("copied", f=_iso(f0), **base)
        if self.live:
            self._note(source, "live, through its definition")
            return SourceState("derived", f=_iso(f0), **base)
        if self.as_at is None:
            return SourceState("copied", f=_iso(f0), **base)
        # Replay the definition through the Source's history, each change point a window of the copy.
        store = self.locator.storage(source)
        previous = f0
        for c in change_points(store, after=f0):
            if self.evaluate(source, defs, path, c, previous_f=previous, read_at=c, retain_from=f0,
                             record=False) is None:
                return None
            previous = c
        self._note(source, f"copied with its history from {f0.isoformat()}")
        return SourceState("copied", f=_iso(f0), history=True, runs=self.runs_after(source, f0), **base)

    def _note(self, source: str, note: str) -> None:
        for r in reversed(self.results):
            if _source_of(r.target) == source:
                r.note = note
                return

    def _live(self, source: str, base: dict) -> SourceState | None:
        started = time.perf_counter()
        try:
            store = self.locator.storage(source)
            f0 = self.freshness_at(source, store)
        except SourceUnreachable as exc:
            self.results.append(HydrateResult(source, "(live)", "failed", time.perf_counter() - started,
                                              error=str(exc)))
            return None
        self.results.append(HydrateResult(source, "(live)", "ok", time.perf_counter() - started,
                                          note=f"live, as of {f0.isoformat()}"))
        return SourceState("live", f=_iso(f0), **base)

    def _copy(self, source: str, base: dict) -> SourceState | None:
        started = time.perf_counter()
        try:
            store = self.locator.storage(source)
            from ..trickle.io import load_sidecar, sidecar_tables

            for table in sidecar_tables(load_sidecar(store)):
                earliest, exact = earliest_readable(store, table)
                if earliest is not None and self.point < earliest:
                    if exact:
                        raise ValueError(f"'{source}.{table}' is kept from {earliest.isoformat()} on: use an --as-at "
                                         "from then or later")
                    self.warnings.append(f"'{source}.{table}' no longer keeps its version at the point; the oldest "
                                         f"it keeps ({earliest.isoformat()}) stands in")
            f0 = self.freshness_at(source, store)
            files = copy_published(store, self.project.snapshot_dir(source))
        except (SourceUnreachable, ValueError) as exc:
            self.results.append(HydrateResult(source, "(copy)", "failed", time.perf_counter() - started,
                                              error=str(exc)))
            return None
        self.results.append(HydrateResult(source, "(copy)", "ok", time.perf_counter() - started,
                                          note=f"copied with its history from {f0.isoformat()} ({files} files)"))
        return SourceState("copied", f=_iso(f0), history=True, runs=self.runs_after(source, f0), **base)

    def _from_catchment(self, source: str, base: dict) -> SourceState | None:
        self.warnings.append(f"source '{source}' has no puddle definition — pulling from the Catchment")
        started = time.perf_counter()
        session = SourceSession(self.project, source, self.project.snapshot_dir(source), self.point)
        try:
            p = Puddle(source, root=self.project.puddles_dir, default_catchment=self.catchment, session=session)
            client = p.catchment()
            tables = client.tables()
            if not tables:
                raise RuntimeError(f"the Catchment has no exported tables for '{source}' — has it run?")
            for table in tables:
                p.write_table(table, client.get(table=table))
            session.publish()
        except Exception as exc:  # noqa: BLE001 - reported as a result
            self.results.append(HydrateResult(source, "(from catchment)", "failed", time.perf_counter() - started,
                                              error=f"{type(exc).__name__}: {exc}", traceback=tb.format_exc()))
            return None
        finally:
            session.close()
        self.results.append(HydrateResult(source, "(from catchment)", "ok", time.perf_counter() - started))
        return SourceState("copied", f=_iso(self.point), **base)


def hydrate(
    project: Project,
    only_sources: list[str] | None = None,
    catchment: str | None = None,
    from_catchment: bool = False,
    *,
    set_name: str | None = None,
    live: bool = False,
    as_at: datetime | None = None,
) -> tuple[list[HydrateResult], list[str]]:
    """Set this Pond's baseline: build each Source's Puddle into ``puddles/ponds/{source}/data/`` (or, with ``live``,
    record that it's read in place) and record what was done in ``puddles/hydrated.json``.

    Each Source uses the definitions of its set (``set_name`` for all, else ``puddles.toml``'s ``[local]``).
    ``as_at`` hydrates as of that freshness: a Source with no definition is copied with every change since, and a
    definition that reads the Source is replayed through them, so incremental runs can step through real
    history. Declared Sources with no definition are skipped with a warning, unless ``live``, ``as_at`` or
    ``from_catchment`` reads them from the Catchment. Returns (results, warnings)."""
    definitions = collect_definitions(project)
    sets = chosen_sets(project, definitions, set_name)
    run = _Hydration(project, catchment, live=live, as_at=as_at)

    targets = list(project.sources)
    if select_definitions(definitions, project.name, sets[project.name]):
        targets.append(project.name)
    if only_sources:
        wanted = set(only_sources)
        for s in sorted(wanted - set(targets)):
            run.warnings.append(f"no puddle defined for source '{s}'")
        targets = [s for s in targets if s in wanted]

    previous = load_hydrated(project) if only_sources else None
    states = dict(previous.sources) if previous is not None else {}
    own_f = previous.own_f if previous is not None else None
    for source in targets:
        defs = select_definitions(definitions, source, sets[source])
        steps = select_definitions(definitions, source, sets[source], step=True)
        states.pop(source, None)
        state = run.hydrate_source(source, defs, steps, sets[source], from_catchment)
        if source == project.name:
            own_f = state.f if state is not None else None
            continue
        if state is not None:
            states[source] = state

    own_runs = None
    if live or as_at is not None:
        try:
            runs = run.locator.runs(project.name, after=run.point)
            own_runs = [_iso(f) for f in runs] if runs is not None else None
        except ValueError:
            own_runs = None
    save_hydrated(project, Hydrated(at=_iso(run.point), catchment=catchment, live=live, as_at=as_at is not None,
                                    sources=states, own_runs=own_runs, own_f=own_f))
    # A new baseline: earlier runs' positions and step output no longer apply.
    from .runner import reset_runs

    reset_runs(project)
    return run.results, run.warnings
