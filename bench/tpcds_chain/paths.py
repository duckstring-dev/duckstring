"""Paths that may be local or on S3, for the benchmark's scripts (not its Ponds, which inline the little they need).

A location is a string: a local path or an `s3://bucket/prefix` URI. fsspec handles files and listings, and
`duckdb_setup` lets a DuckDB connection read and write S3 with the machine's own credentials (an instance
profile on EC2, the AWS profile on a laptop).
"""

from __future__ import annotations

import os

import fsspec


def is_remote(loc: str) -> bool:
    return "://" in str(loc)


def join(base: str, *parts: str) -> str:
    base = str(base)
    return "/".join([base.rstrip("/"), *(p.strip("/") for p in parts)]) if parts else base


def fs(loc: str):
    return fsspec.core.url_to_fs(str(loc))[0]


def exists(loc: str) -> bool:
    return fs(loc).exists(str(loc))


def read_text(loc: str) -> str:
    with fsspec.open(str(loc), "r") as f:
        return f.read()


def write_text(loc: str, text: str) -> None:
    if not is_remote(loc):
        os.makedirs(os.path.dirname(os.path.abspath(loc)), exist_ok=True)
    with fsspec.open(str(loc), "w") as f:
        f.write(text)


def remove(loc: str) -> None:
    """Delete a file or a whole prefix, if it exists."""
    f = fs(loc)
    if f.exists(str(loc)):
        f.rm(str(loc), recursive=True)


def makedirs(loc: str) -> None:
    if not is_remote(loc):
        os.makedirs(loc, exist_ok=True)


def size(loc: str) -> int:
    f = fs(loc)
    return f.du(str(loc)) if f.exists(str(loc)) else 0


def table(dirpath: str, name: str) -> str:
    """The read_parquet target for a table under ``dirpath``: one ``{name}.parquet`` file, or a ``{name}/``
    directory of chunks (how large tables are written, so the write runs in parallel)."""
    single = join(dirpath, f"{name}.parquet")
    return single if exists(single) else join(dirpath, name, "*.parquet")


def duckdb_setup(con, *locations: str) -> None:
    """Let ``con`` reach S3 when any of ``locations`` is remote: httpfs, and a secret from the credential chain."""
    if not any(is_remote(loc) for loc in locations):
        return
    con.execute("INSTALL httpfs")
    con.execute("LOAD httpfs")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    con.execute(f"CREATE OR REPLACE SECRET bench_s3 (TYPE s3, PROVIDER credential_chain, REGION '{region}')")
