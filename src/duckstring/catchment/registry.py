from pathlib import Path

import duckdb


def connect(path: Path) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


def pond_major_dir(root: Path, pond_name: str, major: int) -> Path:
    """The runtime storage dir for one major line of a Pond. Each major executes independently, so
    registry/data/ledger are all per-(name, major). ``m{major}`` cannot collide with the version
    dirs deploy writes alongside it (``ponds/{name}/{semver}/``)."""
    return root / "ponds" / pond_name / f"m{major}"


def pond_registry_path(root: Path, pond_name: str, major: int) -> Path:
    return pond_major_dir(root, pond_name, major) / "registry.duckdb"


def pond_data_dir(root: Path, pond_name: str, major: int) -> Path:
    return pond_major_dir(root, pond_name, major) / "data"


def pond_connect(root: Path, pond_name: str, major: int) -> duckdb.DuckDBPyConnection:
    return connect(pond_registry_path(root, pond_name, major))
