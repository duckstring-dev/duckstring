from __future__ import annotations

import time
import traceback as tb
from dataclasses import dataclass
from pathlib import Path

from ..core import Puddle, collect_puddles, import_pond_module
from .project import Project


@dataclass
class HydrateResult:
    target: str
    name: str  # the puddle function's name
    status: str  # "ok" | "failed"
    duration_s: float = 0.0
    error: str | None = None
    traceback: str | None = None


def collect_definitions(project: Project) -> list[dict]:
    """Import the puddles entrypoint and return its ``@puddle`` registrations, validated against the
    Pond's declared Sources (the Pond's own name is also a valid target — the incremental seed)."""
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
        source = d["target"].partition(".")[0]
        if source not in allowed:
            declared = ", ".join(sorted(allowed))
            raise ValueError(
                f"puddle '{d['target']}' ({d['name']}) targets a Source this Pond does not declare — "
                f"declared: {declared}"
            )
    return definitions


def hydrate(
    project: Project,
    only_sources: list[str] | None = None,
    catchment: str | None = None,
    from_catchment: bool = False,
) -> tuple[list[HydrateResult], list[str]]:
    """Materialise each puddle definition into ``puddles/ponds/{source}/data/``. Declared Sources
    with no definition are skipped with a warning, or — with ``from_catchment`` — filled with every
    exported table of that Source from the Catchment. Returns (results, warnings)."""
    definitions = collect_definitions(project)
    warnings: list[str] = []
    results: list[HydrateResult] = []

    selected = definitions
    if only_sources:
        wanted = set(only_sources)
        selected = [d for d in definitions if d["target"].partition(".")[0] in wanted]
        unknown = wanted - {d["target"].partition(".")[0] for d in definitions}
        for s in sorted(unknown):
            warnings.append(f"no puddle defined for source '{s}'")

    for d in selected:
        p = Puddle(d["target"], root=project.puddles_dir, default_catchment=catchment)
        started = time.perf_counter()
        try:
            returned = d["func"](p)
            if returned is not None:
                if isinstance(returned, (str, Path)):
                    p.write_path(returned)
                else:
                    p.write_table(returned)
            results.append(HydrateResult(d["target"], d["name"], "ok", time.perf_counter() - started))
        except Exception as exc:
            results.append(
                HydrateResult(
                    d["target"], d["name"], "failed", time.perf_counter() - started,
                    error=f"{type(exc).__name__}: {exc}", traceback=tb.format_exc(),
                )
            )

    covered = {d["target"].partition(".")[0] for d in definitions}
    missing = [s for s in project.sources if s not in covered]
    if only_sources:
        missing = [s for s in missing if s in set(only_sources)]
    for source in missing:
        if not from_catchment:
            warnings.append(f"source '{source}' has no puddle definition — skipped (pass --from-catchment to pull it)")
            continue
        warnings.append(f"source '{source}' has no puddle definition — pulling from the Catchment")
        p = Puddle(source, root=project.puddles_dir, default_catchment=catchment)
        started = time.perf_counter()
        try:
            client = p.catchment()
            tables = client.tables()
            if not tables:
                raise RuntimeError(f"the Catchment has no exported tables for '{source}' — has it run?")
            for table in tables:
                p.write_table(table, client.get(table=table))
            results.append(HydrateResult(source, "(from catchment)", "ok", time.perf_counter() - started))
        except Exception as exc:
            results.append(
                HydrateResult(
                    source, "(from catchment)", "failed", time.perf_counter() - started,
                    error=f"{type(exc).__name__}: {exc}", traceback=tb.format_exc(),
                )
            )

    return results, warnings
