from __future__ import annotations

import shutil
import time
import traceback as tb
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..changes import ChangeLedger
from ..core import Pond, load_ripples, retry_on_lock
from ..reports import clear as clear_reports
from ..reports import collect as collect_reports
from .project import Project
from .state import Hydrated, Position, SourceState, _iso, load_hydrated, load_position, position_path, save_position

TARGETS = ("next-change", "next-run", "most-recent")


@dataclass
class RippleResult:
    name: str
    status: str  # "ok" | "failed"
    duration_s: float = 0.0
    error: str | None = None
    traceback: str | None = None


@dataclass
class RunResult:
    seeded: bool
    ripples: list[RippleResult]
    f: datetime | None = None
    previous_f: datetime | None = None
    stepped: list[str] = field(default_factory=list)  # Sources a step wrote this run
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.status == "ok" for r in self.ripples)


def _load_ripples(project: Project):
    """The Pond's Python and SQL Ripples (:func:`duckstring.core.load_ripples`)."""
    return load_ripples(project.dir, require_entry=True)


def _registry_connect(project: Project):
    import duckdb

    return retry_on_lock(lambda: duckdb.connect(str(project.out_dir / "registry.duckdb")))


def _seed(project: Project) -> bool:
    """Copy a self-puddle (``puddles/ponds/{name}/data/*.parquet``) into the run registry as the
    prior state, so an incremental run starts from the same point every time (rerun-idempotent)."""
    from ..dataplane import get_data_plane

    dp = get_data_plane()
    seed_dir = project.snapshot_dir(project.name)
    tables = dp.list_tables(seed_dir)
    if not tables:
        return False
    con = _registry_connect(project)
    try:
        for table in tables:
            con.execute(f'CREATE OR REPLACE TABLE "{table}" AS {dp.read_select(seed_dir, table)}')
    finally:
        con.close()
    return True


def _staging_dir(project: Project) -> Path:
    from ..objects import STAGING_DIR

    return project.out_dir / STAGING_DIR


def _work_dir(project: Project, source: str) -> Path:
    """A copied Puddle's working copy, once a step or a live definition has written to it: the hydrated copy
    under ``puddles/ponds/`` stays as hydrated, so a baseline run goes back to it."""
    return project.puddles_dir / "work" / source / "data"


def reset_runs(project: Project) -> None:
    """Forget where local runs got to: the position, step output and working copies (a new baseline)."""
    shutil.rmtree(project.puddles_dir / "work", ignore_errors=True)
    position_path(project).unlink(missing_ok=True)
    _run_f_marker(project).unlink(missing_ok=True)


def _export(project: Project, f=None, held=frozenset()) -> None:
    registry = project.out_dir / "registry.duckdb"
    if not registry.exists():
        return
    import duckdb

    from ..dataplane import get_data_plane
    from ..objects import commit_objects

    def _copy() -> None:
        con = duckdb.connect(str(registry))
        try:
            # Nothing else reads a local run's output while it publishes, so keep only the newest version.
            get_data_plane().export(con, project.out_dir, mode="overwrite", f=f, retain_from=None, held=held,
                                    exact_ints=True)  # as a line first deployed from 0.9 on publishes
        finally:
            con.close()

    retry_on_lock(_copy)
    # Objects publish after the table export (which writes the sidecar); the staging under out_dir is
    # created during the run and cleared here.
    commit_objects(_staging_dir(project), project.out_dir, f, held)


def _legacy_hydrated(project: Project) -> bool:
    """Puddles hydrated before ``hydrated.json`` existed: Source snapshots on disk with no record."""
    ponds = project.puddles_dir / "ponds"
    return ponds.is_dir() and any((ponds / s / "data").is_dir() for s in project.sources)


class _Plan:
    """Where a run reads each Source from and at what freshness."""

    def __init__(self, project: Project, hydrated: Hydrated | None):
        self.project = project
        self.hydrated = hydrated
        self.warnings: list[str] = []
        self._locator = None

    @property
    def locator(self):
        if self._locator is None:
            from .sources import Locator

            self._locator = Locator(self.project, self.hydrated.catchment if self.hydrated else None)
        return self._locator

    def store(self, source: str, state: SourceState):
        """Where the run reads ``source``: the real Source (live), the working copy, or the hydrated copy."""
        if state.mode == "live":
            return self.locator.storage(source)
        work = _work_dir(self.project, source)
        if (work / "_trickle.json").exists():
            from ..storage import LocalStorage

            return LocalStorage(work)
        return None  # the flat puddles layout

    def history_store(self, source: str, state: SourceState):
        """Where a Source's history is: the real Source for a live one, else its hydrated copy."""
        if state.mode in ("live", "derived"):
            return self.locator.storage(source)
        from ..storage import LocalStorage

        return LocalStorage(self.project.snapshot_dir(source))

    def recorded(self, source: str, state: SourceState) -> list[datetime]:
        """The freshnesses the Source reached after its hydration point (its runs, passes included), oldest first."""
        from .sources import change_points

        f0 = datetime.fromisoformat(state.f) if state.f else None
        if state.mode in ("live", "derived"):
            runs = self.locator.runs(source, after=f0)
            if runs is not None:
                return runs
            self.warnings.append(f"couldn't read '{source}''s run history from the Catchment — using its change points")
            return change_points(self.history_store(source, state), after=f0)
        if not state.history:
            return []
        if state.runs is not None:
            return [datetime.fromisoformat(r) for r in state.runs]
        return change_points(self.history_store(source, state), after=f0)

    def changes(self, source: str, state: SourceState, after: datetime) -> list[datetime]:
        from .sources import change_points

        if state.mode == "copied" and not state.history:
            return []
        return change_points(self.history_store(source, state), after=after)


def _target(plan: _Plan, position: Position, target: str, to: datetime | None) -> datetime:
    """The freshness an incremental run asks for: the next change, the Pond's next production run, the Sources'
    most recent, or ``to``. When the Sources have nothing further, now (only steps can reach it)."""
    prev = position.freshness
    now = datetime.now(timezone.utc)
    hydrated = plan.hydrated
    if to is not None:
        if to <= prev:
            raise ValueError(f"--to {to.isoformat()} isn't after the last run's freshness ({prev.isoformat()})")
        return to
    found: datetime | None = None
    if target == "next-change":
        nexts = [c[0] for s, st in hydrated.sources.items() if (c := plan.changes(s, st, prev))]
        found = min(nexts) if nexts else None
    elif target == "next-run":
        runs = hydrated.own_runs
        if hydrated.live:
            fresh = plan.locator.runs(plan.project.name, after=prev)
            runs = [_iso(f) for f in fresh] if fresh is not None else runs
        if runs is None:
            raise ValueError("--next-run needs this Pond's production runs, and none were recorded: hydrate with "
                             "--as-at or --live while the Catchment it's deployed on is reachable")
        later = [datetime.fromisoformat(r) for r in runs if datetime.fromisoformat(r) > prev]
        found = min(later) if later else None
    elif target == "most-recent":
        latest = []
        for s, st in hydrated.sources.items():
            points = plan.recorded(s, st)
            current = datetime.fromisoformat(position.sources[s]) if s in position.sources else prev
            latest.append(max([current, *points]))
        found = min(latest) if latest else None
        if found is not None and found <= prev:
            found = None
    else:
        raise ValueError(f"unknown target '{target}' — one of: {', '.join(TARGETS)}")
    return found if found is not None else now


def _advance(project: Project, plan: _Plan, position: Position, target: datetime) -> tuple[dict, list[str]]:
    """Bring every Source to ``target``, as a Refresh would: each is read as it stood at the moment the last of
    them reached it. A copied Source whose history doesn't reach it steps (stamped at the target), a live
    definition is evaluated at the new position. Returns ``({source: freshness}, stepped)``."""
    from .hydrate import _Hydration, collect_definitions, select_definitions

    hydrated = plan.hydrated
    definitions = collect_definitions(project)
    reach: dict[str, datetime] = {}
    stepping: list[str] = []
    recorded: dict[str, list[datetime]] = {}
    for source, state in hydrated.sources.items():
        points = recorded[source] = plan.recorded(source, state)
        current = datetime.fromisoformat(position.sources.get(source) or state.f or position.f)
        ahead = [p for p in [current, *points] if p >= target]
        if ahead:
            reach[source] = min(ahead)
        elif state.mode == "copied" and state.steps:
            stepping.append(source)
        elif state.mode in ("live", "derived"):
            raise ValueError(f"production hasn't brought '{source}' to {target.isoformat()} (its latest is "
                             f"{max([current, *points]).isoformat()}): pick an earlier target, or wait")
        else:
            raise ValueError(f"'{source}' has no history reaching {target.isoformat()} and no @puddle.step to go on "
                             "from it")
    moment = max([*reach.values(), target])
    freshness: dict[str, datetime] = {}
    for source in reach:
        current = datetime.fromisoformat(position.sources.get(source) or hydrated.sources[source].f or position.f)
        freshness[source] = max(p for p in [current, *recorded[source]] if p <= moment)
    for source in stepping:
        freshness[source] = target

    step = position.step + 1
    runner = _Hydration(project, hydrated.catchment, live=False, as_at=None)
    for source, state in hydrated.sources.items():
        previous = datetime.fromisoformat(position.sources.get(source) or state.f or position.f)
        new = freshness[source]
        if source in stepping:
            defs = select_definitions(definitions, source, state.set, step=True)
        elif state.mode == "derived" and new > previous:
            defs = select_definitions(definitions, source, state.set)
        else:
            continue
        work = _work_dir(project, source)
        if not (work / "_trickle.json").exists():
            from .sources import link_tree

            shutil.rmtree(work, ignore_errors=True)
            if project.snapshot_dir(source).is_dir():
                link_tree(project.snapshot_dir(source), work)
        f0 = datetime.fromisoformat(state.f) if state.f else None
        session = runner.evaluate(source, defs, work, new, previous_f=previous, read_at=new,
                                  step=step if source in stepping else 0, retain_from=f0, record=False)
        if session is None:
            failed = next(r for r in reversed(runner.results) if r.status == "failed")
            raise ValueError(f"'{source}': {failed.name} failed — {failed.error}\n{failed.traceback or ''}")
    return freshness, stepping


def run_pond(project: Project, ripple: str | None = None, fresh: bool = False, *, incremental: bool = False,
             target: str = "next-change", to: datetime | None = None) -> RunResult:
    """One local Pond Run against the hydrated Puddles.

    A baseline run (the default) resets ``puddles/out/`` and the Puddles' step output, seeds the registry from a
    self-puddle unless ``fresh``, and runs every Ripple as of the hydrated baseline with ``previous_f`` unset. An
    ``incremental`` run keeps the registry and advances to ``target`` (``next-change``, ``next-run``,
    ``most-recent``) or ``to``, as a Refresh would: every Source must reach it, by its history or a step.
    ``ripple`` reruns one Ripple against the existing registry in the last run's window. Stops at the first
    failure; exports whatever the registry holds either way."""
    pond_ripples = _load_ripples(project)
    by_name = pond_ripples.by_name
    order = pond_ripples.order()
    table_writers = pond_ripples.sql_writers()  # plus each Python Ripple's tables as it runs (lineage)
    if ripple is not None and ripple not in by_name:
        raise ValueError(f"no ripple '{ripple}' — this Pond has: {', '.join(order)}")

    hydrated = load_hydrated(project)
    legacy = hydrated is None and _legacy_hydrated(project)
    if hydrated is None and not legacy and project.sources:
        raise ValueError("nothing is hydrated: run duckstring pond hydrate first")
    plan = _Plan(project, hydrated)
    position = load_position(project)
    now = datetime.now(timezone.utc)
    stepped: list[str] = []
    seeded = False

    if ripple is not None:
        project.out_dir.mkdir(parents=True, exist_ok=True)
        targets = [ripple]
        run_f = position.freshness if position is not None else now
        previous_f = None
        source_f = dict(position.sources) if position is not None else {}
        previous_positions = None
    elif incremental:
        if position is None or not (project.out_dir / "registry.duckdb").exists():
            raise ValueError("an incremental run goes on from a baseline: run duckstring pond run first")
        if legacy:
            raise ValueError("these Puddles were hydrated by an older Duckstring: hydrate again to run incrementally")
        targets = order
        previous_f = position.freshness
        # Each Source's changes are read from where the last run read it up to (plans/source-read-positions.md).
        previous_positions = dict(position.sources) if position.sources else None
        if hydrated is None or not hydrated.sources:
            run_f, source_f = max(now, previous_f), {}
        else:
            goal = _target(plan, position, target, to)
            reached, stepped = _advance(project, plan, position, goal)
            source_f = {s: _iso(f) for s, f in reached.items()}
            run_f = min(reached.values())
    else:
        legacy_f = _read_previous_f(project)
        shutil.rmtree(project.out_dir, ignore_errors=True)
        project.out_dir.mkdir(parents=True)
        reset_runs(project)
        seeded = False if fresh else _seed(project)
        targets = order
        if hydrated is not None and hydrated.sources:
            source_f = {s: st.f or hydrated.at for s, st in hydrated.sources.items()}
            run_f = min(datetime.fromisoformat(f) for f in source_f.values())
        else:
            source_f, run_f = {}, now
        previous_f = None
        previous_positions = None
        if seeded:
            own = hydrated.own_f if hydrated is not None else None
            candidates = [datetime.fromisoformat(own)] if own else []
            if position is not None:
                candidates.append(position.freshness)
            elif legacy_f is not None:
                candidates.append(legacy_f)
            previous_f = next((c for c in candidates if c < run_f), None)

    source_dirs = {}
    if hydrated is not None:
        for source, state in hydrated.sources.items():
            store = plan.store(source, state)
            if store is not None:
                source_dirs[source] = store

    changes = ChangeLedger()  # what each Ripple wrote, checked or held: pond.changed() and held tables
    results: list[RippleResult] = []
    for name in targets:
        started = time.perf_counter()
        try:
            con = _registry_connect(project)
            try:
                handle = Pond(project.name, project.version, con, root=project.puddles_dir,
                              f=run_f, previous_f=previous_f, sources=list(project.sources),
                              read_positions=source_f if not legacy else None,
                              previous_positions=previous_positions, source_dirs=source_dirs,
                              staging_dir=_staging_dir(project), own_data_dir=project.out_dir,
                              static_tables=pond_ripples.statics, ripple=name,
                              ancestors=pond_ripples.ancestors(name), table_writers=dict(table_writers),
                              change_sink=changes.begin(run_f, name))
                clear_reports(con, run_f, name)  # a Ripple run again reports afresh
                by_name[name]["func"](handle)
                for table in handle.take_lineage()["writes"]:
                    table_writers.setdefault(table, name)
            finally:
                con.close()
            results.append(RippleResult(name, "ok", time.perf_counter() - started))
        except Exception as exc:
            results.append(
                RippleResult(
                    name, "failed", time.perf_counter() - started,
                    error=f"{type(exc).__name__}: {exc}", traceback=tb.format_exc(),
                )
            )
            break

    if ripple is None and all(r.status == "ok" for r in results):
        con = _registry_connect(project)
        try:  # the Run's reports and its Sources' in its window, as a Duck's export carries them
            collect_reports(Pond(project.name, project.version, con, root=project.puddles_dir, f=run_f,
                                 previous_f=previous_f, sources=list(project.sources),
                                 read_positions=source_f if not legacy else None,
                                 previous_positions=previous_positions, source_dirs=source_dirs,
                                 own_data_dir=project.out_dir))
        finally:
            con.close()
    _export(project, run_f, changes.held(run_f))
    result = RunResult(seeded=seeded, ripples=results, f=run_f, previous_f=previous_f, stepped=stepped,
                       warnings=plan.warnings)
    if ripple is None:
        if result.ok:
            step = (position.step if incremental and position is not None else 0) + (1 if stepped else 0)
            save_position(project, Position(f=_iso(run_f), step=step, sources=source_f))
        elif not incremental:
            position_path(project).unlink(missing_ok=True)  # a failed baseline has nothing to go on from
    return result


def _run_f_marker(project: Project) -> Path:
    # The previous local run's freshness, before position.json recorded it.
    return project.puddles_dir / ".run_f"


def _read_previous_f(project: Project):
    marker = _run_f_marker(project)
    if not marker.exists():
        return None
    try:
        return datetime.fromisoformat(marker.read_text(encoding="utf-8").strip())
    except ValueError:
        return None
