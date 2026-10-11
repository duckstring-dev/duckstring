"""The change ledger: what each Ripple of a Pond Run wrote, checked or held (plans/change-detection.md).

Change is tracked per published object (a table or an Object). A Ripple records each write through the
Pond handle (with whether it changed anything: an overwrite write always does, a Trickle write reports it)
and each :meth:`~duckstring.core.Pond.detect_change` check (with whether the value matched what is
published). The ledger answers two questions:

- ``pond.changed(name)`` and ``pond.changed()`` inside the run: did this run change an own object;
- the publish: which objects are **held**, checked as unchanged and not written, so the export keeps their
  published copy (no new overwrite version, the sidecar entry carried forward).

Entries are keyed by the run's freshness ``f`` (pipelined Runs share a registry and must not mix) and by
Ripple. A Ripple's entries are cleared when it is launched, so an immediate retry starts clean. The ledger
lives in memory: after a Duck crash it is gone, and the affected tables count as changed and are
republished, a redundant write rather than a missed update.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class _Entry:
    written: bool = False
    changed: bool = False  # written and changed something (an overwrite write always does)
    checked: bool = False
    unchanged: bool = False  # the last check matched the published copy


class ChangeLedger:
    """Thread-safe: Ripples record from executor threads while the serve loop launches and publishes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict = {}  # f → {ripple: {name: _Entry}}

    def begin(self, f, ripple: str) -> RunChanges:
        """Start (or restart, for a retry) ``ripple``'s attempt in the Run at ``f``: drop what an earlier
        attempt recorded and return the sink its Pond handle records into."""
        with self._lock:
            self._runs.setdefault(f, {})[ripple] = {}
        return RunChanges(self, f, ripple)

    def drop(self, f) -> None:
        """Forget the Run at ``f`` (it published, or gave up)."""
        with self._lock:
            self._runs.pop(f, None)

    def _entry(self, f, ripple: str, name: str) -> _Entry:
        return self._runs.setdefault(f, {}).setdefault(ripple, {}).setdefault(name, _Entry())

    def record_write(self, f, ripple: str, name: str, changed: bool) -> None:
        with self._lock:
            e = self._entry(f, ripple, name)
            e.written = True
            e.changed = e.changed or bool(changed)

    def record_check(self, f, ripple: str, name: str, unchanged: bool) -> None:
        with self._lock:
            e = self._entry(f, ripple, name)
            e.checked = True
            e.unchanged = bool(unchanged)

    def _entries(self, f, name: str) -> list[_Entry]:
        return [names[name] for names in self._runs.get(f, {}).values() if name in names]

    def changed(self, f, name: str) -> bool:
        """Whether the Run at ``f`` wrote ``name`` and the write changed it. A held or untouched object is
        unchanged."""
        with self._lock:
            return any(e.written and e.changed for e in self._entries(f, name))

    def written(self, f, name: str) -> bool:
        """Whether the Run at ``f`` wrote ``name``."""
        with self._lock:
            return any(e.written for e in self._entries(f, name))

    def any_changed(self, f) -> bool:
        """Whether the Run at ``f`` changed any object it recorded."""
        with self._lock:
            return any(e.written and e.changed for names in self._runs.get(f, {}).values() for e in names.values())

    def held(self, f) -> frozenset[str]:
        """The objects the Run at ``f`` holds: checked, every check unchanged, and never written."""
        with self._lock:
            names = {n for names in self._runs.get(f, {}).values() for n in names}
            out = set()
            for n in names:
                entries = self._entries(f, n)
                if any(e.written for e in entries):
                    continue
                checks = [e for e in entries if e.checked]
                if checks and all(e.unchanged for e in checks):
                    out.add(n)
            return frozenset(out)


class RunChanges:
    """One Ripple attempt's view of the ledger: the sink a :class:`~duckstring.core.Pond` handle records
    into and queries."""

    def __init__(self, ledger: ChangeLedger, f, ripple: str) -> None:
        self.ledger = ledger
        self.f = f
        self.ripple = ripple

    def record_write(self, name: str, changed: bool = True) -> None:
        self.ledger.record_write(self.f, self.ripple, name, changed)

    def record_check(self, name: str, unchanged: bool) -> None:
        self.ledger.record_check(self.f, self.ripple, name, unchanged)

    def changed(self, name: str) -> bool:
        return self.ledger.changed(self.f, name)

    def written(self, name: str) -> bool:
        return self.ledger.written(self.f, name)

    def any_changed(self) -> bool:
        return self.ledger.any_changed(self.f)
