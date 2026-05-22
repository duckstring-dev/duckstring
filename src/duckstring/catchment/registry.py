from pathlib import Path

import duckdb


def connect(path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(path))
