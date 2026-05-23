from pathlib import Path

import duckdb


def connect(path: Path) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


def pond_registry_path(root: Path, pond_name: str) -> Path:
    return root / "ponds" / pond_name / "registry.duckdb"


def pond_connect(root: Path, pond_name: str) -> duckdb.DuckDBPyConnection:
    return connect(pond_registry_path(root, pond_name))
