from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..core import pond_entrypoints, read_pond_toml


@dataclass
class Project:
    """A Pond project directory (the cwd of ``pond hydrate`` / ``pond run``)."""

    dir: Path
    info: dict = field(repr=False)

    @property
    def name(self) -> str:
        return self.info.get("pond", {}).get("name", self.dir.name)

    @property
    def version(self) -> str:
        return self.info.get("pond", {}).get("version", "0.0.0")

    @property
    def sources(self) -> dict[str, str]:
        return self.info.get("sources", {})

    @property
    def ripples_entry(self) -> str:
        return pond_entrypoints(self.info)[0]

    @property
    def puddles_entry(self) -> str:
        return pond_entrypoints(self.info)[1]

    @property
    def puddles_dir(self) -> Path:
        """The root the local run uses as the Pond handle's ``root`` — snapshots live under
        ``puddles/ponds/{source}/data/`` so ``Pond.read_table`` resolves them unchanged."""
        return self.dir / "puddles"

    @property
    def out_dir(self) -> Path:
        return self.puddles_dir / "out"

    def snapshot_dir(self, source: str) -> Path:
        return self.puddles_dir / "ponds" / source / "data"


def load_project(cwd: Path | None = None) -> Project:
    """Load the Pond project at ``cwd``; raises ``FileNotFoundError`` when there is no pond.toml."""
    directory = Path(cwd) if cwd else Path.cwd()
    if not (directory / "pond.toml").exists():
        raise FileNotFoundError(f"no pond.toml found in {directory}")
    return Project(dir=directory, info=read_pond_toml(directory))
