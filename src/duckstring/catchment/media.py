"""Media: an App line's files, held by the Catchment (plans/records-and-media.md).

One place per Catchment: a directory under its root (``media/``, the default) or an object-store prefix
(``s3://``, ``gs://``, ``az://``/``abfss://``; credentials as the data root's, in the URI or the environment).
Each App line ``name@major`` has ``{name}/m{major}/`` inside it. Paths inside a line are relative, ``/``
separated, and never leave it.
"""

from __future__ import annotations

import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator

SETTING = "media"  # "local" (default) or an object-store URI
CHUNK = 1 << 20


class MediaError(Exception):
    pass


def clean_path(path: str) -> str:
    """``path`` as a relative path inside a line, or :class:`MediaError`: no ``..``, no absolute path, no
    backslash, no empty or hidden-control segments."""
    if not path or "\\" in path or "\x00" in path:
        raise MediaError(f"not a Media path: {path!r}")
    parts = PurePosixPath(path.strip("/")).parts
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise MediaError(f"not a Media path: {path!r}")
    return "/".join(parts)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class Media:
    def __init__(self, root: Path, db):
        self.root = Path(root)
        self.db = db
        self._lock = threading.Lock()
        self._storage = None
        self._storage_for: str | None = None

    # ── settings ───────────────────────────────────────────────────────────────────────────────────────

    @property
    def target(self) -> str:
        from .cloud import get_setting
        from .db import locked

        with locked(self.db):
            return get_setting(self.db, SETTING) or "local"

    @property
    def is_local(self) -> bool:
        return self.target == "local"

    def in_use(self) -> bool:
        from .db import locked

        with locked(self.db):
            return self.db.execute("SELECT 1 FROM app_data WHERE media = 1 LIMIT 1").fetchone() is not None

    def configure(self, target: str) -> None:
        from ..storage import is_object_uri
        from .cloud import set_setting
        from .db import locked

        target = (target or "").strip() or "local"
        if target != "local" and not is_object_uri(target):
            raise MediaError(f"Media goes in the Catchment's own directory (local) or an object store "
                             f"(s3://, gs://, az://, abfss://), not {target!r}")
        if target == self.target:
            return
        if self.in_use():
            raise MediaError("App lines already hold Media here; where Media lives can't change now (a move that "
                             "copies it across is the way)")
        with locked(self.db):
            set_setting(self.db, SETTING, None if target == "local" else target)

    # ── addressing ─────────────────────────────────────────────────────────────────────────────────────

    def _base(self):
        """The local directory, or the object store's :class:`~duckstring.storage.ObjectStorage`."""
        target = self.target
        if target == "local":
            return self.root / "media"
        with self._lock:
            if self._storage is None or self._storage_for != target:
                from ..storage import get_storage

                self._storage = get_storage(target)
                self._storage_for = target
            return self._storage

    @staticmethod
    def _line(name: str, major: int) -> str:
        if not name or "/" in name or name.startswith("."):
            raise MediaError(f"not an App name: {name!r}")
        return f"{name}/m{int(major)}"

    def location(self, name: str, major: int) -> str:
        """Where the line's Media lives, as an App is told it: a local path or an object-store URI."""
        base = self._base()
        line = self._line(name, major)
        if isinstance(base, Path):
            return str(base / line)
        return base.uri(*line.split("/"))

    def ensure_line(self, name: str, major: int) -> None:
        base = self._base()
        if isinstance(base, Path):
            (base / self._line(name, major)).mkdir(parents=True, exist_ok=True)

    # ── files ──────────────────────────────────────────────────────────────────────────────────────────

    def list(self, name: str, major: int, prefix: str = "") -> list[dict]:
        """Every file under ``prefix`` in the line: ``{path, size, modified}``, by path."""
        base = self._base()
        line = self._line(name, major)
        sub = clean_path(prefix) if prefix.strip("/") else ""
        out = []
        if isinstance(base, Path):
            top = base / line
            start = top / sub if sub else top
            if not start.exists():
                return []
            for dirpath, _dirs, files in os.walk(start):
                for f in files:
                    p = Path(dirpath) / f
                    st = p.stat()
                    out.append({"path": p.relative_to(top).as_posix(), "size": st.st_size,
                                "modified": _iso(st.st_mtime)})
        else:
            fs = base.fs
            top = base._key(*line.split("/"))
            start = top + ("/" + sub if sub else "")
            try:
                found = fs.find(start, detail=True)
            except FileNotFoundError:
                return []
            for key, info in found.items():
                if info.get("type") == "directory":
                    continue
                rel = key.split(top.split("://", 1)[-1], 1)[-1].lstrip("/")
                modified = info.get("LastModified") or info.get("last_modified") or info.get("mtime")
                if isinstance(modified, datetime):
                    modified = modified.astimezone(timezone.utc).isoformat()
                elif isinstance(modified, (int, float)):
                    modified = _iso(modified)
                out.append({"path": rel, "size": int(info.get("size") or 0), "modified": modified})
        return sorted(out, key=lambda e: e["path"])

    def usage(self, name: str, major: int) -> int:
        return sum(e["size"] for e in self.list(name, major))

    def read(self, name: str, major: int, path: str) -> Iterator[bytes]:
        base = self._base()
        rel = f"{self._line(name, major)}/{clean_path(path)}"
        if isinstance(base, Path):
            p = base / rel
            if not p.is_file():
                raise FileNotFoundError(path)
            with open(p, "rb") as f:
                while chunk := f.read(CHUNK):
                    yield chunk
        else:
            key = base._key(*rel.split("/"))
            if not base.fs.isfile(key):
                raise FileNotFoundError(path)
            with base.fs.open(key, "rb") as f:
                while chunk := f.read(CHUNK):
                    yield chunk

    def size(self, name: str, major: int, path: str) -> int:
        base = self._base()
        rel = f"{self._line(name, major)}/{clean_path(path)}"
        if isinstance(base, Path):
            p = base / rel
            if not p.is_file():
                raise FileNotFoundError(path)
            return p.stat().st_size
        key = base._key(*rel.split("/"))
        if not base.fs.isfile(key):
            raise FileNotFoundError(path)
        return int(base.fs.size(key))

    def write(self, name: str, major: int, path: str, data: BinaryIO) -> int:
        """Write ``data`` (a readable binary stream) to ``path``, replacing a file there; the bytes written.
        Locally the file appears whole (written beside it, then renamed)."""
        base = self._base()
        rel = f"{self._line(name, major)}/{clean_path(path)}"
        written = 0
        if isinstance(base, Path):
            p = base / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f".{p.name}.uploading")
            with open(tmp, "wb") as f:
                while chunk := data.read(CHUNK):
                    f.write(chunk)
                    written += len(chunk)
            os.replace(tmp, p)
        else:
            key = base._key(*rel.split("/"))
            with base.fs.open(key, "wb") as f:
                while chunk := data.read(CHUNK):
                    f.write(chunk)
                    written += len(chunk)
        return written

    def delete(self, name: str, major: int, path: str) -> None:
        base = self._base()
        rel = f"{self._line(name, major)}/{clean_path(path)}"
        if isinstance(base, Path):
            p = base / rel
            if not p.is_file():
                raise FileNotFoundError(path)
            p.unlink()
        else:
            key = base._key(*rel.split("/"))
            if not base.fs.isfile(key):
                raise FileNotFoundError(path)
            base.fs.rm(key)

    def copy_line(self, src: tuple[str, int], dst: tuple[str, int]) -> None:
        """Copy every file of one line into another, replacing what the destination held: real copies locally
        (a hardlink would let an App appending to a file in place change both lines, a frozen one included),
        server-side copies on an object store."""
        base = self._base()
        a, b = self._line(*src), self._line(*dst)
        if isinstance(base, Path):
            target = base / b
            if target.exists():
                shutil.rmtree(target)
            source = base / a
            if not source.exists():
                target.mkdir(parents=True, exist_ok=True)
                return
            shutil.copytree(source, target)
        else:
            fs = base.fs
            to = base._key(*b.split("/"))
            if fs.exists(to):
                fs.rm(to, recursive=True)
            for entry in self.list(*src):
                fs.copy(base._key(*a.split("/"), *entry["path"].split("/")),
                        base._key(*b.split("/"), *entry["path"].split("/")))

    def drop(self, name: str, major: int) -> None:
        base = self._base()
        line = self._line(name, major)
        if isinstance(base, Path):
            shutil.rmtree(base / line, ignore_errors=True)
        else:
            key = base._key(*line.split("/"))
            if base.fs.exists(key):
                base.fs.rm(key, recursive=True)
