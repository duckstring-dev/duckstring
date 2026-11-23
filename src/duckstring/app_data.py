"""Reading Apps' Records and Media from a Pond (plans/records-and-media.md).

A Pond reads an App line's Records with ``pond.read_records("feedback@1.ratings")`` (a relation, through DuckDB's
``postgres`` extension, read-only) and its files with ``pond.media("feedback@1", "uploads/")`` (a
:class:`MediaInput`). Both are reads of an outside source: an Inlet always runs when asked, and a Pond with
Sources that reads them sets ``@ripple(always_run=True)`` on the Ripple doing so.

Where they are comes from the Catchment on each Run's job (``set_locations``); a local ``pond run`` reads
``puddles/records/{name}_v{major}/{table}.parquet`` and ``puddles/media/{name}/m{major}/`` instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

_LINE = re.compile(r"([a-z_][a-z0-9_]*)@([0-9]+)")
ATTACHED = "_duckstring_records"

# Where this process reads Records and Media: set by the Duck from each Run's job (constant per Catchment).
_locations: dict[str, str | None] = {"records": None, "media": None}
_attach_lock = threading.Lock()


def set_locations(records: str | None, media: str | None) -> None:
    _locations.update(records=records, media=media)


def parse_line(line: str) -> tuple[str, int]:
    m = _LINE.fullmatch(line.strip())
    if not m:
        raise ValueError(f"an App line is name@major, such as feedback@1 (not {line!r})")
    return m.group(1), int(m.group(2))


def split_ref(ref: str) -> tuple[str, int, str]:
    """``"feedback@1.ratings"`` → ``("feedback", 1, "ratings")``."""
    line, dot, table = ref.partition(".")
    if not dot or not table:
        raise ValueError(f"a Records table is name@major.table, such as feedback@1.ratings (not {ref!r})")
    name, major = parse_line(line)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", table):
        raise ValueError(f"not a Records table name: {table!r}")
    return name, major, table


def read_records(pond, ref: str):
    """The App line's Records table ``ref`` (``name@major.table``) as a relation on the Pond's connection."""
    name, major, table = split_ref(ref)
    schema = f"{name}_v{major}"
    url = _locations["records"] or os.environ.get("DUCKSTRING_RECORDS_READER_URL")
    if url:
        with _attach_lock:  # an attached catalog is the instance's, shared by every Ripple's cursor
            pond.con.execute("INSTALL postgres; LOAD postgres")
            attached = {r[0] for r in pond.con.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
            if ATTACHED not in attached:
                pond.con.execute(f"ATTACH '{url}' AS {ATTACHED} (TYPE postgres, READ_ONLY)")
        return pond.con.sql(f'SELECT * FROM {ATTACHED}."{schema}"."{table}"')
    snapshot = Path(pond.root) / "records" / schema / f"{table}.parquet"
    if snapshot.is_file():  # a local run: a snapshot under puddles/records/
        return pond.con.sql(f"SELECT * FROM read_parquet('{snapshot}')")
    raise RuntimeError(f"Records aren't reachable here: run on a Catchment, or put a snapshot of {ref} at {snapshot}")


def _media_base(pond) -> str:
    base = _locations["media"] or os.environ.get("DUCKSTRING_MEDIA_ROOT")
    return base or str(Path(pond.root) / "media")


class MediaInput:
    """An App line's Media (or a folder of it) as one Ripple run sees it.

    Attributes:
        ref: What was read: ``name@major`` and the folder.
        version: A digest of every file's path, size and modification time.
        changed: Whether the version differs from the one the Ripple's previous completed run read (always true
            on a first run and after a Rebuild).
    """

    def __init__(self, pond, line: str, prefix: str = ""):
        from .catchment.media import clean_path

        self.name, self.major = parse_line(line)
        self.prefix = clean_path(prefix) if prefix.strip("/") else ""
        self.ref = f"media:{self.name}@{self.major}/{self.prefix}"
        self._pond = pond
        base = _media_base(pond)
        self._local = "://" not in base
        self._top = (f"{base.rstrip('/')}/{self.name}/m{self.major}")
        self._files = self._list()
        listing = {e["path"]: [e["size"], e["modified"]] for e in self._files}
        self.version = hashlib.sha256(json.dumps(listing, sort_keys=True).encode()).hexdigest()[:32]
        self._previous = pond._input_state(self.ref)
        self.changed = self._previous is None or self._previous.get("version") != self.version
        pond._record_input(self.ref, {"version": self.version, "files": listing})

    def _list(self) -> list[dict]:
        out = []
        if self._local:
            top = Path(self._top)
            start = top / self.prefix if self.prefix else top
            if start.is_dir():
                for dirpath, _dirs, files in os.walk(start):
                    for f in files:
                        if f.startswith(".") and f.endswith(".uploading"):
                            continue  # an upload in flight
                        p = Path(dirpath) / f
                        st = p.stat()
                        out.append({"path": p.relative_to(top).as_posix(), "size": st.st_size,
                                    "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()})
        else:
            fs, root = self._fs()
            start = root + ("/" + self.prefix if self.prefix else "")
            try:
                found = fs.find(start, detail=True)
            except FileNotFoundError:
                found = {}
            stripped = root.split("://", 1)[-1]
            for key, info in found.items():
                if info.get("type") == "directory":
                    continue
                modified = info.get("LastModified") or info.get("last_modified") or info.get("mtime")
                out.append({"path": key.split(stripped, 1)[-1].lstrip("/"), "size": int(info.get("size") or 0),
                            "modified": modified.isoformat() if isinstance(modified, datetime) else str(modified)})
        return sorted(out, key=lambda e: e["path"])

    def _fs(self):
        from .storage import get_storage

        store = get_storage(self._top)
        return store.fs, store._key()

    def files(self) -> list[dict]:
        """Every file now: ``{path, size, modified}``, paths relative to the line's Media, by path."""
        return list(self._files)

    def added(self) -> list[dict]:
        """The files new or changed since the Ripple's previous completed run (every file on a first run)."""
        before = (self._previous or {}).get("files") or {}
        return [e for e in self._files if before.get(e["path"]) != [e["size"], e["modified"]]]

    def removed(self) -> list[str]:
        """The paths of files gone since the Ripple's previous completed run."""
        before = (self._previous or {}).get("files") or {}
        now = {e["path"] for e in self._files}
        return sorted(p for p in before if p not in now)

    def open(self, path: str, mode: str = "rb"):
        """A file of the line's Media, for reading."""
        from .catchment.media import clean_path

        if "w" in mode or "a" in mode or "+" in mode:
            raise TypeError("a Pond reads Media; only its App writes it")
        rel = clean_path(path)
        if self._local:
            return open(Path(self._top) / rel, mode)
        fs, root = self._fs()
        return fs.open(f"{root}/{rel}", mode)

    def path(self, path: str) -> Path:
        """A file of the line's Media as a local path (fetched into the Pond's scratch space from an object
        store), for libraries that want a filename."""
        from .catchment.media import clean_path

        rel = clean_path(path)
        if self._local:
            return Path(self._top) / rel
        cache = Path(self._pond.root) / ".media-cache" / self.name / f"m{self.major}" / rel
        cache.parent.mkdir(parents=True, exist_ok=True)
        fs, root = self._fs()
        fs.get(f"{root}/{rel}", str(cache))
        return cache
