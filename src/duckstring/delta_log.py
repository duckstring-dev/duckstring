"""Reading a Delta table's transaction log (``_delta_log/``) for :mod:`duckstring.databricks`.

DuckDB's ``delta`` extension reads the data. This module answers the questions around it: the table's newest
version, what a commit did, and which files a version reads (its data files and deletion vectors). The last is
what lets a table on a Volume reached through the Files API, which DuckDB can't address, be mirrored to local
disk file by file; the commits say which files were added since a version, so an append-only table can be read
incrementally.

Only what those questions need is implemented: commits are JSON lines; checkpoints are classic (one Parquet
file), multi-part, or v2 (a JSON or Parquet file naming sidecars in ``_delta_log/_sidecars/``). Paths in actions
are URL-encoded and relative to the table, unless absolute.
"""

from __future__ import annotations

import json
import re
import uuid
from urllib.parse import unquote

LOG = "_delta_log"
_COMMIT = re.compile(r"^(\d{20})\.json$")
_CLASSIC = re.compile(r"^(\d{20})\.checkpoint\.parquet$")
_MULTI = re.compile(r"^(\d{20})\.checkpoint\.(\d{10})\.(\d{10})\.parquet$")
_V2 = re.compile(r"^(\d{20})\.checkpoint\.[0-9a-fA-F-]{36}\.(json|parquet)$")
_Z85 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-:+=^!/*?&<>()[]{}@%$#"


class DeltaLogError(RuntimeError):
    """The log doesn't hold what was asked for (a version cleaned up by log retention, or a layout this reader
    doesn't follow)."""


def is_table(store) -> bool:
    """Whether ``store`` (a :class:`duckstring.storage.Storage`) is a Delta table's directory."""
    return store.is_dir(LOG)


def log_names(store) -> list[str]:
    return store.names(LOG) if store.is_dir(LOG) else []


def commit_name(version: int) -> str:
    return f"{version:020d}.json"


def latest_version(names) -> int | None:
    """The newest committed version among the log's file names."""
    versions = [int(m.group(1)) for m in map(_COMMIT.match, names) if m]
    return max(versions) if versions else None


def commit_actions(store, version: int) -> list[dict]:
    """The actions of one commit, in order."""
    text = store.read_text(LOG, commit_name(version))
    if text is None:
        raise DeltaLogError(f"commit {version} of {store.uri()} isn't in the log (log retention may have removed it)")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def checkpoints(names) -> dict[int, list[str]]:
    """The complete checkpoints among the log's file names: version → the file names to read (a v2 checkpoint's
    top-level file, a classic one, or every part of a multi-part one)."""
    out: dict[int, list[str]] = {}
    parts: dict[tuple[int, int], list[str]] = {}
    for name in names:
        if m := _V2.match(name) or _CLASSIC.match(name):
            out.setdefault(int(m.group(1)), [name])
        elif m := _MULTI.match(name):
            parts.setdefault((int(m.group(1)), int(m.group(3))), []).append(name)
    for (version, total), files in parts.items():
        if len(files) == total and version not in out:
            out[version] = sorted(files)
    return out


def base_checkpoint(names, version: int) -> int | None:
    """The newest complete checkpoint at or before ``version``, or None (replay from commit 0)."""
    usable = [v for v in checkpoints(names) if v <= version]
    return max(usable) if usable else None


def log_files_for(names, versions) -> list[str]:
    """The log files a reader of each of ``versions`` needs: its base checkpoint (sidecars aside: see
    :func:`sidecars`) and every commit after it up to the newest version, plus ``_last_checkpoint``."""
    lows = []
    for version in versions:
        base = base_checkpoint(names, version)
        lows.append(-1 if base is None else base)
    low, high = min(lows), max(versions)
    cps = checkpoints(names)
    keep = {name for v in set(lows) if v >= 0 for name in cps[v]}
    keep |= {n for n in names if (m := _COMMIT.match(n)) and low < int(m.group(1)) <= high}
    if "_last_checkpoint" in names:
        keep.add("_last_checkpoint")
    missing = [v for v in range(low + 1, high + 1) if commit_name(v) not in names]
    if missing:
        raise DeltaLogError(f"the log is missing commits {missing[0]}..{missing[-1]} (log retention may have "
                            "removed them)")
    return sorted(keep)


def sidecars(con, store, names) -> list[str]:
    """The sidecar files (``_sidecars/...``) the v2 checkpoints among ``names`` reference."""
    out: list[str] = []
    for name in names:
        if _V2.match(name):
            for action in _checkpoint_actions(con, store, [name], ("sidecar",)):
                out.append("_sidecars/" + unquote(action["sidecar"]["path"]).rsplit("/", 1)[-1])
    return out


def files_at(con, store, version: int, names=None) -> tuple[set[str], set[str]]:
    """The files ``version`` reads, as paths relative to the table: ``(data files, deletion vector files)``.
    ``store`` holds the log (at least :func:`log_files_for` and :func:`sidecars`); ``con`` is a DuckDB
    connection that can read it."""
    names = log_names(store) if names is None else names
    base = base_checkpoint(names, version)
    live: dict[tuple, dict] = {}
    if base is not None:
        files = checkpoints(names)[base]
        actions = _checkpoint_actions(con, store, files, ("add", "sidecar"))
        side = ["_sidecars/" + unquote(a["sidecar"]["path"]).rsplit("/", 1)[-1] for a in actions if a.get("sidecar")]
        if side:
            actions += _checkpoint_actions(con, store, side, ("add",))
        for a in actions:
            if a.get("add"):
                live[_key(a["add"])] = a["add"]
    for v in range((base if base is not None else -1) + 1, version + 1):
        for a in commit_actions(store, v):
            if a.get("add"):
                live[_key(a["add"])] = a["add"]
            elif a.get("remove"):
                live.pop(_key(a["remove"]), None)
    data, dvs = set(), set()
    for add in live.values():
        data.add(relative(add["path"]))
        dv = dv_path(add.get("deletionVector"))
        if dv:
            dvs.add(dv)
    return data, dvs


def appended_files(store, after: int, upto: int) -> list[str] | None:
    """The data files the commits in ``(after, upto]`` added, when every one of them only adds files (no
    removes, no deletion vectors), else None: an update, delete, overwrite or compaction happened. Raises
    :class:`DeltaLogError` when a commit is gone from the log."""
    out: list[str] = []
    for v in range(after + 1, upto + 1):
        for a in commit_actions(store, v):
            if a.get("remove") or (a.get("add") and a["add"].get("deletionVector")):
                return None
            if a.get("add"):
                out.append(relative(a["add"]["path"]))
    return out


def relative(path: str) -> str:
    """An action's path as a path relative to the table (decoded)."""
    if "://" in path or path.startswith("/"):
        raise DeltaLogError(f"the table names a file outside its directory ({path}), which this reader can't mirror")
    return unquote(path)


def dv_path(dv: dict | None) -> str | None:
    """The file a deletion vector lives in, relative to the table, or None (none, or stored inline)."""
    if not dv or dv.get("storageType") == "i":
        return None
    if dv.get("storageType") == "u":
        encoded = dv["pathOrInlineDv"]
        prefix, ident = encoded[:-20], uuid.UUID(bytes=z85_decode(encoded[-20:]))
        name = f"deletion_vector_{ident}.bin"
        return f"{prefix}/{name}" if prefix else name
    return relative(dv["pathOrInlineDv"])


def z85_decode(text: str) -> bytes:
    """Decode Z85 (ZeroMQ's base-85), the encoding of a deletion vector's file id."""
    out = bytearray()
    for i in range(0, len(text), 5):
        n = 0
        for ch in text[i:i + 5]:
            n = n * 85 + _Z85.index(ch)
        out += n.to_bytes(4, "big")
    return bytes(out)


def _key(action: dict) -> tuple:
    """A logical file's identity: its path and its deletion vector's (the spec's ``(path, dvId)``)."""
    dv = action.get("deletionVector") or {}
    return (unquote(action["path"]), dv.get("storageType"), dv.get("pathOrInlineDv"), dv.get("offset"))


def _checkpoint_actions(con, store, files: list[str], kinds: tuple[str, ...]) -> list[dict]:
    """The actions of ``kinds`` in checkpoint (or sidecar) files under the log."""
    out: list[dict] = []
    parquet = [f for f in files if f.endswith(".parquet")]
    for name in files:
        if name.endswith(".json"):
            text = store.read_text(LOG, name) or ""
            out += [a for a in map(json.loads, filter(str.strip, text.splitlines())) if any(k in a for k in kinds)]
    if parquet:
        uris = ", ".join(f"'{store.uri(LOG, *f.split('/'))}'" for f in parquet)
        rel = f"read_parquet([{uris}], union_by_name = true)"
        cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()}
        for kind in kinds:
            if kind in cols:
                out += [{kind: json.loads(row[0])} for row in
                        con.execute(f"SELECT to_json({kind}) FROM {rel} WHERE {kind} IS NOT NULL").fetchall()]
    return out
