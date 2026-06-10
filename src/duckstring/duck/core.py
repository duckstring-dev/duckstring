"""DuckCore — the transport-free heart of a Duck.

Wires the push-only :class:`~duckstring.engine.worker.WorkerState` to the run ledger and an outgoing
event buffer. It is driven by two inputs — ``begin_run(F)`` from the Catchment and ``ripple_completed``
from the executor — and produces two outputs — the list of Ripple **names to launch** and buffered
**events** to report. Threads, the executor, and HTTP live in :mod:`.executor` / :mod:`.client` /
``__main__`` so this stays deterministically testable.

Resilience: every state change is persisted to the ledger before it is reported, and events are
buffered until the Catchment acknowledges them — so a Pond Run completes (and is recoverable) with no
Catchment involvement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..engine import NEVER, worker
from ..engine import pond as ledger


@dataclass
class Event:
    """A buffered report to the Catchment. ``kind`` is ``"ripple"`` or ``"run_completed"``; idempotent
    on ``(kind, ripple, f)`` so replay after a reconnect is safe. ``started_at``/``finished_at`` are
    the Ripple's wall-clock execution span (telemetry for the run-history duration)."""

    kind: str
    f: datetime
    ripple: str | None = None
    status: str = "success"
    retry: int = 0  # attempt index for a ripple/failed event (0 = first try); the retry trace
    error: str | None = None  # failure message (for a failed ripple/run), surfaced in the UI + DB
    traceback: str | None = None  # full traceback for the failure, if any
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def payload(self) -> dict:
        d = {"kind": self.kind, "f": self.f.isoformat(), "status": self.status, "retry": self.retry}
        if self.ripple is not None:
            d["ripple"] = self.ripple
        if self.error is not None:
            d["error"] = self.error
        if self.traceback is not None:
            d["traceback"] = self.traceback
        if self.started_at is not None:
            d["started_at"] = self.started_at.isoformat()
        if self.finished_at is not None:
            d["finished_at"] = self.finished_at.isoformat()
        return d


class DuckCore:
    def __init__(self, pond_name: str, con, parents: dict[str, list[str]], optional=None):
        self.pond_name = pond_name
        self.con = con
        self.state = ledger.load_state(con, parents, optional)
        self.events: list[Event] = []  # buffered, awaiting delivery to the Catchment
        self.attempts: dict[str, int] = {}  # ripple name → attempt index of its current in-flight run
        self.last_begin_f: datetime = NEVER  # freshness of the most recently started Pond Run

    def begin_run(self, f: datetime, now: datetime, retry_immediately: int = 0, force: bool = False) -> list[str]:
        """Start a Pond Run at freshness ``f`` (idempotent — completed Ripples are not re-stamped, unless
        ``force``, which recomputes every Ripple). ``retry_immediately`` is the Run's Ripple-retry budget
        (the Catchment's live setting). Returns the Ripple names the caller must launch."""
        self.state = worker.begin_run(self.state, f, retry_immediately, force=force)
        self.last_begin_f = max(self.last_begin_f, f)
        ledger.record_pond_run_start(self.con, f, now)
        return self._advance(now)

    def pond_failed(self, error: str | None = None, traceback: str | None = None) -> None:
        """Buffer a Pond-level failure (a Duck error not tied to a Ripple — e.g. a failed ledger
        write). Attributed to the most recently started Pond Run; the Catchment fails the whole Pond."""
        self.events.append(Event(
            kind="pond_failed", f=self.last_begin_f, status="failed", error=error, traceback=traceback,
        ))

    def ripple_completed(
        self, name: str, now: datetime, started_at=None, finished_at=None, export=None
    ) -> list[str]:
        """Record a finished Ripple, buffer a report, and (if a Pond Run just completed) export +
        buffer the run completion. ``started_at``/``finished_at`` are the Ripple's wall-clock span
        (run-history duration telemetry). Returns any newly launchable Ripple names."""
        end_f = self.state.states[name].start_f
        ledger.record_ripple_complete(self.con, name, end_f)
        self.state, rc = worker.complete_ripple(self.state, name, now)
        self.events.append(
            Event(kind="ripple", ripple=name, f=end_f, retry=self.attempts.pop(name, 0),
                  started_at=started_at, finished_at=finished_at)
        )
        if rc is not None:
            if export is not None:
                export()  # materialise outputs (parquet) before announcing completion
            ledger.record_pond_run_finish(self.con, rc.f, now)
            self.events.append(Event(kind="run_completed", f=rc.f))
        return self._advance(now)

    def ripple_failed(
        self, name: str, now: datetime, started_at=None, finished_at=None,
        error: str | None = None, traceback: str | None = None,
    ) -> list[str]:
        """A Ripple errored. Spend one of the Pond Run's immediate retries (relaunching the Ripple) if
        any remain; otherwise the Run gives up — record it and buffer a ``failed`` event for the
        Catchment. ``error``/``traceback`` are the failure message + stack (surfaced in the UI).
        Returns any Ripple names to (re)launch."""
        self.state, rf = worker.fail_ripple(self.state, name, now)
        ledger.record_ripple_failed(self.con, name)
        attempt = self.attempts.get(name, 0)
        if rf is None:  # retried within budget — log the failed attempt, the Run continues
            self.events.append(Event(
                kind="ripple", ripple=name, f=self.state.states[name].start_f, status="failed",
                retry=attempt, error=error, traceback=traceback, started_at=started_at, finished_at=finished_at,
            ))
            self.attempts[name] = attempt + 1  # next launch of this Ripple is the next attempt
        else:  # immediate budget exhausted — this Pond Run failed at the Ripple's freshness
            ledger.record_pond_run_finish(self.con, rf.f, now, status="failed")
            self.events.append(Event(
                kind="failed", ripple=name, f=rf.f, status="failed",
                retry=attempt, error=error, traceback=traceback, started_at=started_at, finished_at=finished_at,
            ))
            self.attempts.pop(name, None)  # the Run is done; reset for any future run
        return self._advance(now)

    def _advance(self, now: datetime) -> list[str]:
        self.state, launched = worker.sentinel(now, self.state)
        for name in launched:
            ledger.record_ripple_start(self.con, name, self.state.states[name].start_f)
        return launched

    def idle(self) -> bool:
        """True when no Ripple is running and no target is outstanding — the Pond has quiesced and the
        Duck may shut down (once its event buffer has drained)."""
        return all(not s.is_running and not s.targets for s in self.state.states.values())

    def flush(self, send) -> None:
        """Try to deliver buffered events in order via ``send(payload) -> bool``, **stopping at the
        first failure** so causal order is preserved (ripples before their run completion). A failure
        usually means the Catchment is unreachable, so the rest would fail too. Safe to call
        repeatedly; events are idempotent on the Catchment side."""
        while self.events:
            if not send(self.events[0].payload()):
                return
            self.events.pop(0)
