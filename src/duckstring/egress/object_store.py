"""Object-store egress driver — the baseline (see plans/egress.md "Object-store egress").

v1: write each egressed table as a **snapshot** Parquet file under the destination prefix
(``{prefix}/{table}.parquet``, atomic replace). ``supports_delta=False``, so the worker always
``write_full``s — the simplest correct "land my Pond's output as Parquet over there". Only ``file://``
is wired; ``s3://`` / ``gs://`` (via DuckDB ``httpfs``) and the incremental per-run-parts / Iceberg-in-
bucket layout are the next step.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from . import credentials
from .base import Capabilities
from .destination import Destination


def _safe_table(name: str) -> str:
    if not name or name in (".", "..") or "/" in name or "\\" in name or os.sep in name:
        raise ValueError(f"unsafe table name for an object-store path: {name!r}")
    return name


class ObjectStoreEgressDriver:
    SCHEMES = ("file",)  # s3/gs (DuckDB httpfs) land next

    def __init__(self, dest: Destination):
        self.dest = dest

    def capabilities(self) -> Capabilities:
        return Capabilities(supports_delta=False, supports_delete=False, transactional=False)

    def ensure(self, *, table: str, schema: dict | None, pk: list[str] | None) -> None:
        self._base().mkdir(parents=True, exist_ok=True)

    def write_full(self, relation, *, table: str, pk: list[str] | None, f: datetime) -> None:
        base = self._base()
        base.mkdir(parents=True, exist_ok=True)
        target = base / f"{_safe_table(table)}.parquet"
        tmp = base / f".{_safe_table(table)}.{os.getpid()}.tmp.parquet"  # same dir → atomic os.replace
        try:
            relation.write_parquet(str(tmp))
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    def apply_delta(self, delta, *, table: str, pk: list[str] | None, f: datetime) -> None:
        raise NotImplementedError("object-store egress is snapshot-only in v1 (no apply_delta)")

    def _base(self) -> Path:
        """The local directory for a ``file://`` destination, with ``${env:NAME}`` credentials resolved.
        (Resolution happens here, at egress time — never stored or logged.)"""
        raw = credentials.resolve(self.dest.raw)
        u = urlparse(raw)
        if u.scheme != "file":
            raise NotImplementedError(f"object-store scheme {u.scheme!r} not implemented yet (file:// only)")
        if u.netloc and u.netloc not in ("", "localhost"):
            raise ValueError(
                f"file:// destination must be an absolute local path (file:///path); got host {u.netloc!r}"
            )
        return Path(u.path)
