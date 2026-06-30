from pathlib import Path

import duckdb

from ..storage import LocalStorage, Storage, get_storage


def connect(path: Path) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


def pond_major_dir(root: Path, pond_name: str, major: int) -> Path:
    """The runtime **state** storage dir for one major line of a Pond — its working ``registry.duckdb``
    and ``pond.db`` ledger. Each major executes independently, so registry/ledger are per-(name, major).
    ``m{major}`` cannot collide with the version dirs deploy writes alongside it (``ponds/{name}/{semver}/``).

    This is **hot state**: it always lives on the local POSIX state root (never an object store). The
    *data* a Pond publishes goes to :func:`pond_data_dir`, which may be elsewhere (a bucket / Volume)."""
    return root / "ponds" / pond_name / f"m{major}"


def pond_registry_path(root: Path, pond_name: str, major: int) -> Path:
    return pond_major_dir(root, pond_name, major) / "registry.duckdb"


def pond_data_dir(root: Path, pond_name: str, major: int, data_root: str | None = None) -> Storage:
    """The data-plane storage location one major line publishes its tables into — a :class:`Storage`,
    not a ``Path``, so it can be a local directory **or** an object store / Volume URI.

    With ``data_root`` unset the data lives under the state root (``{root}/ponds/{name}/m{major}/data`` —
    today's exact layout). With ``data_root`` set (a path or ``s3://``/``gs://``/``abfss://`` URI) the
    line's data is ``{data_root}/{name}/m{major}/data``."""
    if data_root is None:
        return LocalStorage(pond_major_dir(root, pond_name, major) / "data")
    return get_storage(data_root).child(pond_name, f"m{major}", "data")


def pond_connect(root: Path, pond_name: str, major: int) -> duckdb.DuckDBPyConnection:
    return connect(pond_registry_path(root, pond_name, major))
