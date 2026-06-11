from __future__ import annotations

import shutil
import time
import traceback as tb
from dataclasses import dataclass

from ..core import Pond, collect_ripples, import_pond_module, retry_on_lock
from .project import Project


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

    @property
    def ok(self) -> bool:
        return all(r.status == "ok" for r in self.ripples)


def _load_ripples(project: Project) -> list[dict]:
    entry = project.dir / project.ripples_entry
    if not entry.exists():
        raise FileNotFoundError(f"no ripples entrypoint at {project.ripples_entry} — is this a Pond project?")
    try:
        import_pond_module(project.dir, project.ripples_entry)
    except Exception:
        collect_ripples()
        raise
    return collect_ripples()


def _topo_order(ripples: list[dict]) -> list[str]:
    func_to_name = {r["func"]: r["name"] for r in ripples}
    parents = {
        r["name"]: [func_to_name[p] for p in r["parents"] if p in func_to_name]
        for r in ripples
    }
    order: list[str] = []
    done: set[str] = set()
    remaining = dict(parents)
    while remaining:
        ready = sorted(n for n, ps in remaining.items() if all(p in done for p in ps))
        if not ready:
            raise ValueError(f"cycle in ripple graph: {', '.join(sorted(remaining))}")
        for n in ready:
            order.append(n)
            done.add(n)
            remaining.pop(n)
    return order


def _registry_connect(project: Project):
    import duckdb

    return retry_on_lock(lambda: duckdb.connect(str(project.out_dir / "registry.duckdb")))


def _seed(project: Project) -> bool:
    """Copy a self-puddle (``puddles/ponds/{name}/data/*.parquet``) into the run registry as the
    prior state, so an incremental run starts from the same point every time (rerun-idempotent)."""
    seed_dir = project.snapshot_dir(project.name)
    files = sorted(seed_dir.glob("*.parquet")) if seed_dir.exists() else []
    if not files:
        return False
    con = _registry_connect(project)
    try:
        for pq in files:
            path_sql = str(pq).replace("'", "''")
            con.execute(f'CREATE OR REPLACE TABLE "{pq.stem}" AS SELECT * FROM read_parquet(\'{path_sql}\')')
    finally:
        con.close()
    return True


def _export(project: Project) -> None:
    registry = project.out_dir / "registry.duckdb"
    if not registry.exists():
        return
    import duckdb

    def _copy() -> None:
        con = duckdb.connect(str(registry))
        try:
            for (table,) in con.execute("SHOW TABLES").fetchall():
                dest = project.out_dir / f"{table}.parquet"
                tmp = project.out_dir / f"{table}.parquet.tmp"
                con.execute(f'COPY "{table}" TO \'{tmp}\' (FORMAT PARQUET)')
                tmp.replace(dest)
        finally:
            con.close()

    retry_on_lock(_copy)


def run_pond(project: Project, ripple: str | None = None, fresh: bool = False) -> RunResult:
    """One local Pond Run against the hydrated puddles. A full run resets ``puddles/out/`` (seeding
    it from a self-puddle unless ``fresh``) and executes every Ripple in topo order; ``ripple`` runs
    a single Ripple against the existing registry. Stops at the first failure; exports whatever the
    registry holds either way."""
    ripples = _load_ripples(project)
    by_name = {r["name"]: r for r in ripples}
    order = _topo_order(ripples)

    if ripple is not None:
        if ripple not in by_name:
            raise ValueError(f"no ripple '{ripple}' — this Pond has: {', '.join(order)}")
        project.out_dir.mkdir(parents=True, exist_ok=True)
        targets = [ripple]
        seeded = False
    else:
        shutil.rmtree(project.out_dir, ignore_errors=True)
        project.out_dir.mkdir(parents=True)
        seeded = False if fresh else _seed(project)
        targets = order

    results: list[RippleResult] = []
    for name in targets:
        started = time.perf_counter()
        try:
            con = _registry_connect(project)
            try:
                by_name[name]["func"](Pond(project.name, project.version, con, root=project.puddles_dir))
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

    _export(project)
    return RunResult(seeded=seeded, ripples=results)
