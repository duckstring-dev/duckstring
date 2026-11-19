"""Reaching a Source's real published data from a local run: where it is, what changed when, and when it ran.

A Puddle that reads its Source in place (``p.read_source``, ``pond hydrate --live``, ``--as-at``) reads the Source's
published files directly, as a remote Duck would: from a registered local Catchment's root, else from the
Catchment's data root with this machine's own credentials. Run freshnesses come from the Catchment's
``/api/runs``; without it, a Source's change points stand in for them."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from ..keys import spec_major
from .project import Project

log = logging.getLogger(__name__)


class SourceUnreachable(ValueError):
    """The Catchment's published data can't be read from this machine."""


def _registration(name: str | None) -> tuple[str, dict]:
    """``(url, registration)`` for a registered Catchment, or ``(url, {})`` for a bare URL."""
    if name and "://" in name:
        return name, {}
    from ..cli.config import load_config

    config = load_config()
    catchments = config.get("catchments", {})
    effective = name or config.get("default_catchment")
    if not effective and len(catchments) == 1:
        effective = next(iter(catchments))
    if not effective or effective not in catchments:
        raise SourceUnreachable(
            f"no catchment {name!r} registered" if name else "no catchment specified and no default set"
        )
    cfg = catchments[effective]
    return cfg["url"], cfg


class Locator:
    """Finds the Sources of one project on one Catchment."""

    def __init__(self, project: Project, catchment: str | None = None):
        self.project = project
        self.catchment = catchment
        self._reg: tuple[str, dict] | None = None
        self._data_root: str | None | bool = False  # False = not looked up yet
        self._stores: dict[str, object] = {}

    def major(self, source: str) -> int:
        if source == self.project.name:
            return spec_major(self.project.version)
        spec = self.project.sources.get(source)
        if spec is None:
            raise ValueError(f"'{source}' isn't a Source of this Pond")
        return spec_major(str(spec))

    def _registration(self) -> tuple[str, dict]:
        if self._reg is None:
            self._reg = _registration(self.catchment)
        return self._reg

    def _client(self):
        import httpx

        from ..core import resolve_catchment_auth

        url, headers = resolve_catchment_auth(self.catchment)
        return httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=10.0)

    def _get(self, path: str, params: dict | None = None):
        with self._client() as client:
            resp = client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()

    def data_root(self) -> str | None:
        if self._data_root is False:
            _, reg = self._registration()
            root = reg.get("data_root")
            if root is None and not reg.get("root"):
                try:
                    root = self._get("/api/catchment/settings").get("data_root")
                except Exception as exc:  # noqa: BLE001 - reported as unreachable below
                    raise SourceUnreachable(
                        f"couldn't ask the Catchment where its data is ({exc}): start it, or use p.catchment() or "
                        "pond hydrate --from-catchment") from None
            self._data_root = root
        return self._data_root or None

    def storage(self, source: str):
        """The Source's published data as a :class:`~duckstring.storage.Storage`, at its pinned major."""
        if source in self._stores:
            return self._stores[source]
        from ..catchment.registry import pond_data_dir, readable_root, resolve_data_dir

        url, reg = self._registration()
        major = self.major(source)
        data_root = self.data_root()
        if not readable_root(data_root):
            raise SourceUnreachable(
                f"the Catchment's data root {data_root} can only be reached through its Files API, which a local "
                "run can't read in place: use p.catchment() or pond hydrate --from-catchment"
            )
        if reg.get("root"):
            store = resolve_data_dir(Path(reg["root"]).expanduser(), source, major, data_root)
        elif data_root:
            store = pond_data_dir(Path("."), source, major, data_root)
        else:
            raise SourceUnreachable(
                f"the Catchment at {url} keeps its data on its own disk, which this machine can't read: use "
                "p.catchment() or pond hydrate --from-catchment (or register its root with catchment connect)"
            )
        if not store.exists("_trickle.json"):
            raise SourceUnreachable(f"'{source}@{major}' has published nothing at {store.uri()}")
        self._stores[source] = store
        return store

    def runs(self, source: str, after: datetime | None = None, upto: datetime | None = None) -> list[datetime] | None:
        """The freshnesses the Source's line ran at in ``(after, upto]`` (passes included), oldest first; ``None``
        when the Catchment can't be asked."""
        from ..engine.core import NEVER

        params = {"pond": source, "major": self.major(source), "lineage": "false", "limit": 1000}
        out: set[datetime] = set()
        cursor = after if after is not None and after > NEVER else None
        try:
            while True:
                page = self._get("/api/runs", {**params, **({"after": cursor.isoformat()} if cursor else {})})
                runs = page.get("runs", [])
                for r in runs:
                    if r.get("status") != "success" or not r.get("f"):
                        continue
                    f = datetime.fromisoformat(r["f"])
                    if (after is None or f > after) and (upto is None or f <= upto):
                        out.add(f)
                if cursor is None or len(runs) < 1000:
                    break
                cursor = max(datetime.fromisoformat(r["started_at"]) for r in runs if r.get("started_at"))
        except Exception as exc:  # noqa: BLE001 - the caller falls back to change points
            log.debug("couldn't read the run history of %s: %s", source, exc)
            return None
        return sorted(out)

    def latest_run(self, source: str, upto: datetime) -> datetime | None:
        """The Source's newest run freshness at or before ``upto``; ``None`` if it can't be told."""
        params = {"pond": source, "major": self.major(source), "lineage": "false", "limit": 100,
                  "before": upto.isoformat()}
        try:
            runs = self._get("/api/runs", params).get("runs", [])
        except Exception:  # noqa: BLE001
            return None
        fs = [datetime.fromisoformat(r["f"]) for r in runs if r.get("status") == "success" and r.get("f")]
        fs = [f for f in fs if f <= upto]
        return max(fs) if fs else None


def change_points(store, after: datetime | None = None, upto: datetime | None = None) -> list[datetime]:
    """The freshnesses at which ``store``'s published tables changed in ``(after, upto]``, oldest first: append
    parts, a merge table's base tokens, bands and change log parts, plain-table versions."""
    from ..dataplane import _base_tokens
    from ..trickle.io import changelog_name, load_sidecar, part_f, part_lo, sidecar_tables, table_parts, warm_name

    sidecar = load_sidecar(store)
    points: set[datetime] = set()

    def add(names):
        for n in names:
            try:
                points.update({part_lo(n), part_f(n)})  # a union of parts: its ends (the points inside aren't listed)
            except ValueError:
                continue

    for table, entry in sidecar_tables(sidecar).items():
        if table.startswith("_duckstring_"):
            continue  # the Run reports change with every reporting Run, not with the data
        mode = entry.get("mode")
        if mode == "append":
            add(table_parts(store, table))
        elif mode == "merge":
            points.update(t for t in _base_tokens(store, table) if t is not None)
            add(table_parts(store, warm_name(table)))
            add(table_parts(store, changelog_name(table)))
        else:
            add(table_parts(store, f"{table}__v"))
            if entry.get("f") and not table_parts(store, f"{table}__v"):
                points.add(datetime.fromisoformat(entry["f"]))
    return sorted(p for p in points if (after is None or p > after) and (upto is None or p <= upto))


def published_f(store) -> datetime | None:
    """The freshness ``store`` last published at (its run stamp)."""
    from ..dataplane import _sidecar_run_f

    iso = _sidecar_run_f(store)
    return datetime.fromisoformat(iso) if iso else None


def earliest_readable(store, table: str) -> tuple[datetime | None, bool]:
    """``(earliest, exact)``: the earliest freshness ``table`` can be read as of, and whether a read before it fails
    (a merge table before its oldest kept base, an append table below its floor) rather than falling back to the
    oldest version kept (a plain table)."""
    from ..dataplane import _base_tokens
    from ..trickle.io import load_sidecar, part_f, sidecar_tables, table_parts

    entry = sidecar_tables(load_sidecar(store)).get(table) or {}
    mode = entry.get("mode")
    if mode == "merge":
        tokens = [t for t in _base_tokens(store, table) if t is not None]
        return (min(tokens) if tokens else None), True
    if mode == "append":
        floor = entry.get("floor")
        return (datetime.fromisoformat(floor) if floor else None), True
    versions = []
    for n in table_parts(store, f"{table}__v"):
        try:
            versions.append(part_f(n))
        except ValueError:
            continue
    return (min(versions) if versions else None), False


def copy_published(store, dest: Path) -> int:
    """Copy every published file of ``store`` into ``dest`` (sidecar last), so reads as of any freshness the
    Source still keeps resolve the same there. Returns the number of files copied."""
    from ..dataplane import restore_tree
    from ..storage import LocalStorage

    dest.mkdir(parents=True, exist_ok=True)
    return restore_tree(store, LocalStorage(dest))


def link_tree(src: Path, dest: Path) -> None:
    """``dest`` as a copy of ``src`` made of hard links where the filesystem allows (published files are written
    once and replaced, never changed in place), plain copies otherwise."""
    import os
    import shutil

    for path in src.rglob("*"):
        rel = path.relative_to(src)
        target = dest / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(path, target)
        except OSError:
            shutil.copy2(path, target)
