"""Waiting for the Runs a command caused, for scripts and agents (``--wait``).

A command that asks for work (``refresh now``, ``control wake|force|rebuild``, ``pond deploy``) can wait until its
target Pond has done it and settled, then exit 0 if every Run it caused succeeded, 1 naming each failed Pond,
Ripple and error, or 2 on a timeout. Without a terminal the one-shot commands wait this way by default, since the
live status view is for people.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import typer

_SETTLED = ("idle", "failed", "killed", "blocked")
_POLL_S = 1.0

WAIT_HELP = ("Wait until the Pond has done it and settled, then exit 0 if every Run it caused succeeded, 1 if one "
             "failed (naming it) or 2 on --timeout. The default without a terminal.")
TIMEOUT_HELP = "With --wait: how long to wait, e.g. 10m or 2h."
LIVE_HELP = "Open the live status view even without a terminal."


def interactive() -> bool:
    """Whether a person is watching: stdout is a terminal."""
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def parse_timeout(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    from .window import _parse_duration

    return float(_parse_duration(text))


class Waiter:
    """Snapshot the Runs that exist before a command, then wait for its target to reach ``goal`` and settle."""

    def __init__(self, cfg: dict, pond: str, major: Optional[int] = None, version: Optional[str] = None):
        self.cfg, self.pond, self.major, self.version = cfg, pond, major, version
        self.since = datetime.now(timezone.utc)
        self.before = {(r["id"], r["f"]) for r in self._runs(limit=500)}
        entry = self.entry()
        self.target = entry["id"] if entry else None
        self.completed = entry.get("runs_completed", 0) if entry else 0

    def _runs(self, limit: int = 200, ripples: bool = False) -> list[dict]:
        from . import _http

        params = {"pond": self.pond, "lineage": "true", "limit": limit, "ripples": "true" if ripples else "false"}
        if self.major is not None:
            params["major"] = self.major
        r = _http.get(f"{self.cfg['url']}/api/runs", auth=self.cfg, params=params, allow=(404,))
        return [] if r.status_code == 404 else r.json().get("runs", [])  # 404: not deployed yet (a first deploy)

    def ran_version(self, version: str) -> bool:
        """Whether a Run of ``version`` of the target has finished since this Waiter was made."""
        return any(r.get("version") == version and r["id"] == self.target_id() and r.get("finished_at")
                   and (r["id"], r["f"]) not in self.before for r in self._runs(limit=50))

    def target_id(self) -> str:
        return self.target or f"{self.pond}@{self.major}"

    def entry(self) -> Optional[dict]:
        from . import _http

        ponds = _http.get(f"{self.cfg['url']}/api/status", auth=self.cfg).json().get("ponds", [])
        mine = [p for p in ponds if p["name"] == self.pond and (self.major is None or p["major"] == self.major)]
        if not mine:
            return None
        return max(mine, key=lambda p: p["major"])

    def wait(self, goal: Callable[[dict], bool], timeout: Optional[float], what: str) -> None:
        """Poll until the target settles with ``goal(entry)`` true (or settles failed, killed or blocked), then
        report and exit with the outcome's code."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            entry = self.entry()
            if entry is None:
                typer.echo(f"Error: no Pond named '{self.pond}'.", err=True)
                raise typer.Exit(1)
            self.target = entry["id"]
            status = entry.get("status")
            if status in ("failed", "killed", "blocked") or (status in _SETTLED and goal(entry)):
                break
            if deadline is not None and time.monotonic() >= deadline:
                typer.echo(f"Timed out waiting for '{self.pond}' ({status}): {what} hasn't finished.", err=True)
                raise typer.Exit(2)
            time.sleep(_POLL_S)
        self.report(entry, what)

    def report(self, entry: dict, what: str) -> None:
        new = [r for r in self._runs(ripples=True) if (r["id"], r["f"]) not in self.before]
        failed = [r for r in new if r.get("status") in ("failed", "killed")]
        status = entry.get("status")
        if not failed and status not in ("failed", "killed", "blocked"):
            done = [r for r in new if r.get("status") == "success"]
            typer.echo(f"{what}: '{self.pond}' is fresh as of {entry.get('end_f') or 'never'}, "
                       f"after {len(done)} Run{'s' if len(done) != 1 else ''}.")
            return
        for r in sorted(failed, key=lambda r: r["started_at"] or ""):
            ripples = [rr for rr in r.get("ripples") or [] if rr.get("status") == "failed"]
            where = ", ".join(sorted({rr["ripple"] for rr in ripples})) or "the Pond"
            error = next((rr.get("error") for rr in reversed(ripples) if rr.get("error")), None) or r.get("error")
            typer.echo(f"Failed: {r['id']} at {r['f']} ({where}): {error or r['status']}", err=True)
        if status in ("failed", "killed", "blocked") and not any(r["id"] == entry["id"] for r in failed):
            reason = entry.get("error") or entry.get("blocked_reason") or status
            if status == "blocked" and entry.get("blocked_by"):
                reason = f"blocked by {', '.join(entry['blocked_by'])}"
            typer.echo(f"{entry['id']} is {status}: {reason}", err=True)
        raise typer.Exit(1)
