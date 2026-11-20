"""What a local run remembers between commands: the Puddle sets chosen in ``puddles.toml``, what ``pond hydrate``
did (``puddles/hydrated.json``) and where the last ``pond run`` left off (``puddles/position.json``)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from ..core import DEFAULT_PUDDLE_SET
from .project import Project

PUDDLES_TOML = "puddles.toml"
LOCAL_SECTION = "local"
STATE_VERSION = 1


def _iso(f: datetime | None) -> str | None:
    return f.isoformat() if f is not None else None


def _dt(iso: str | None) -> datetime | None:
    return datetime.fromisoformat(iso) if iso else None


def read_puddles_toml(directory: Path) -> dict | None:
    """The parsed ``puddles.toml`` in ``directory`` (``None`` without one): a section per Catchment, named for it,
    plus ``[local]``. Raises ``ValueError`` when it doesn't parse."""
    path = Path(directory) / PUDDLES_TOML
    if not path.exists():
        return None
    import sys

    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover - 3.10
        import tomli as tomllib
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{PUDDLES_TOML}: {exc}") from None


def local_sets(project: Project) -> dict[str, str]:
    """``{source: set}`` from the ``[local]`` section of ``puddles.toml``: the Puddle set each Source hydrates from
    (a Source it doesn't list uses the default set). Empty without the file or the section."""
    info = read_puddles_toml(project.dir) or {}
    section = info.get(LOCAL_SECTION, {})
    if not isinstance(section, dict) or not all(isinstance(v, str) for v in section.values()):
        raise ValueError(f"{PUDDLES_TOML}: [{LOCAL_SECTION}] maps each Source to a set name, like transactions = \"recent\"")
    return dict(section)


@dataclass
class SourceState:
    """One Source's Puddle, as hydrated.

    ``mode``: ``copied`` (written into ``puddles/ponds/{source}/data``), ``live`` (read in place) or ``derived``
    (a live Source with a definition over it, evaluated by each run). ``f`` is its freshness at the hydration
    point. ``runs``: the freshnesses it ran at after that (passes included), recorded for a copied history; a
    live Source's are read at each run. ``history``: whether the copy holds changes after the hydration point.
    """

    mode: str
    set: str = DEFAULT_PUDDLE_SET
    f: str | None = None
    history: bool = False
    runs: list[str] | None = None
    major: int | None = None
    steps: bool = False


@dataclass
class Hydrated:
    at: str  # the hydration point: --as-at, else when hydrate ran
    catchment: str | None = None
    live: bool = False
    as_at: bool = False
    sources: dict[str, SourceState] = field(default_factory=dict)
    own_runs: list[str] | None = None  # this Pond's own run freshnesses after the point, for --next-run
    own_f: str | None = None  # this Pond's own freshness at the point, when its own output was hydrated from it
    version: int = STATE_VERSION

    @property
    def point(self) -> datetime:
        return datetime.fromisoformat(self.at)


def hydrated_path(project: Project) -> Path:
    return project.puddles_dir / "hydrated.json"


def load_hydrated(project: Project) -> Hydrated | None:
    path = hydrated_path(project)
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    sources = {name: SourceState(**s) for name, s in raw.pop("sources", {}).items()}
    return Hydrated(sources=sources, **raw)


def save_hydrated(project: Project, hydrated: Hydrated) -> None:
    path = hydrated_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(hydrated), indent=2), encoding="utf-8")
    tmp.replace(path)


@dataclass
class Position:
    """Where the last local run left off: its freshness, the step count since the baseline, and the freshness
    each Source was read at."""

    f: str
    step: int = 0
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def freshness(self) -> datetime:
        return datetime.fromisoformat(self.f)


def position_path(project: Project) -> Path:
    return project.puddles_dir / "position.json"


def load_position(project: Project) -> Position | None:
    path = position_path(project)
    if not path.exists():
        return None
    return Position(**json.loads(path.read_text(encoding="utf-8")))


def save_position(project: Project, position: Position) -> None:
    path = position_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(position), indent=2), encoding="utf-8")


__all__ = [
    "Hydrated", "Position", "SourceState", "PUDDLES_TOML", "load_hydrated", "save_hydrated", "load_position",
    "save_position", "local_sets", "read_puddles_toml", "_iso", "_dt",
]
