"""Reaching a Source's real published data from a local run: through its Catchment's API, never in place.

A Puddle that reads its Source (``p.read_source``, ``pond hydrate --live``, ``--as-at``) reads it with ``/api/query``
at a point (:mod:`duckstring.remote_source`), with the credentials of the registered Catchment it hydrates from.
Run freshnesses come from the Catchment's ``/api/runs`` and change points from ``/api/ponds/{name}/changes``."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..dataplane import change_points  # noqa: F401 - re-exported for the local modules
from ..keys import spec_major
from .project import Project


class SourceUnreachable(ValueError):
    """The Catchment a Puddle reads from couldn't be asked."""


class _LazyTransport:
    def __init__(self, catchment: str | None):
        self.catchment = catchment
        self._inner = None

    def _resolve(self):
        if self._inner is None:
            from ..remote_source import HttpTransport

            try:
                self._inner = HttpTransport.registered(self.catchment)
            except Exception as exc:  # noqa: BLE001 - no registration, or no default
                raise SourceUnreachable(f"no Catchment to read the Sources from: {exc}") from None
        return self._inner

    def query(self, payload: dict):
        return self._resolve().query(payload)

    def get(self, path: str, params: dict | None = None):
        return self._resolve().get(path, params)


class Locator:
    """Finds the Sources of one project on one Catchment."""

    def __init__(self, project: Project, catchment: str | None = None, transport=None):
        self.project = project
        self.catchment = catchment
        self._transport = transport  # given: a Duck's, which reaches production through its own Catchment
        self._remotes: dict[str, object] = {}

    def major(self, source: str) -> int:
        if source == self.project.name:
            return spec_major(self.project.version)
        spec = self.project.sources.get(source)
        if spec is None:
            raise ValueError(f"'{source}' isn't a Source of this Pond")
        return spec_major(str(spec))

    @property
    def transport(self):
        """The registered Catchment's transport, resolved at its first use (a definition that never reads its
        Source needs no Catchment)."""
        if self._transport is None:
            self._transport = _LazyTransport(self.catchment)
        return self._transport

    def remote(self, source: str, queries: dict | None = None, kinds: dict | None = None):
        """The Source on its Catchment, read through its whole tables or through ``queries``."""
        from ..remote_source import RemoteSource

        if queries is None and kinds is None:
            if source not in self._remotes:
                self._remotes[source] = RemoteSource(source, self.major(source), self.transport)
            return self._remotes[source]
        return RemoteSource(source, self.major(source), self.transport, queries=queries, kinds=kinds)

    def runs(self, source: str, after: datetime | None = None, upto: datetime | None = None) -> list[datetime]:
        """The freshnesses the Source reached in ``(after, upto]`` (its Runs, passes included, and its changes),
        oldest first. Raises :class:`SourceUnreachable` when the Catchment can't be asked."""
        try:
            return self.remote(source).runs(after, upto)
        except SourceUnreachable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SourceUnreachable(f"couldn't ask the Catchment about '{source}': {exc}") from None

    def own_runs(self, after: datetime | None = None) -> list[datetime] | None:
        """This Pond's own Run freshnesses on the Catchment after ``after``; ``None`` when it isn't deployed
        there."""
        try:
            return self.remote(self.project.name).run_freshnesses(after)
        except SourceUnreachable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SourceUnreachable(f"couldn't ask the Catchment about '{self.project.name}': {exc}") from None

    def latest_run(self, source: str, upto: datetime) -> datetime | None:
        """The Source's newest freshness at or before ``upto``."""
        points = self.runs(source, upto=upto)
        return points[-1] if points else None

    def changes(self, source: str, after: datetime | None = None) -> list[datetime]:
        try:
            return self.remote(source).changes(after)
        except Exception as exc:  # noqa: BLE001
            raise SourceUnreachable(f"couldn't ask the Catchment about '{source}': {exc}") from None


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
