"""CatchmentClient — the Duck's HTTP link to the Catchment.

Catchment→Duck is a poll (``GET /api/duck/{pond}/jobs``) for ``begin_run`` / ``shutdown`` commands, which
the Catchment holds briefly when nothing is queued and releases when a job is (``woken``); Duck→Catchment
is a plain POST per event. Both are Duck-initiated, so the same client works identically for a local
subprocess and a remote Duck. All calls are best-effort: failures are swallowed so the run ledger remains
the durable source of truth and events buffer for replay.
"""

from __future__ import annotations

import time

import httpx


class CatchmentClient:
    def __init__(self, base_url: str, pond_name: str, major: int, token: str, poll_timeout: float = 25.0,
                 version: str | None = None):
        self.base = base_url.rstrip("/")
        self.pond = pond_name
        self.major = major
        self.version = version  # the code this Duck runs, sent on each poll so a redeploy can replace it
        self.token = token
        self.poll_timeout = poll_timeout
        self._client = httpx.Client(timeout=poll_timeout + 5.0, headers={"X-Duck-Token": token})
        # When the Catchment last answered (time.monotonic()): a poll or an event it accepted. A refused
        # request (a recreated Catchment's new token, a removed Pond) is no contact either.
        self.last_contact = time.monotonic()
        # Whether the last empty poll was held by the Catchment (released by a queued job, or at the end of
        # its hold): the Duck then polls again at once rather than backing off (routes/duck.py).
        self.woken = False

    def poll_jobs(self) -> list[dict]:
        """Poll for commands. Returns a list of ``{"kind": "begin_run", "f": ...}`` / ``{"kind": "shutdown"}``
        dicts, empty when nothing is queued; ``woken`` says whether the Catchment held the empty poll.
        Returns ``[]`` on any transport error."""
        self.woken = False
        try:
            params: dict = {"wait": self.poll_timeout}
            if self.version:
                params["version"] = self.version
            r = self._client.get(f"{self.base}/api/duck/{self.pond}/{self.major}/jobs", params=params)
            r.raise_for_status()
            self.last_contact = time.monotonic()
            body = r.json()
            self.woken = bool(body.get("wake") or body.get("held"))
            return body.get("jobs", [])
        except Exception:
            return []

    def fetch_artifact(self) -> bytes:
        """Fetch the deployed version's source bundle (an uncompressed tar) — the remote-Duck boot
        path (prereqs D6): a Duck that can't read the Catchment's disk pulls its code over the duck
        channel. NOT best-effort — a Duck without its source cannot serve, so this raises."""
        r = self._client.get(f"{self.base}/api/duck/{self.pond}/{self.major}/artifact", timeout=60.0)
        r.raise_for_status()
        return r.content

    def post_event(self, payload: dict) -> bool:
        """Deliver one buffered event. Returns True on success (so the Duck drops it from the buffer)."""
        try:
            r = self._client.post(f"{self.base}/api/duck/{self.pond}/{self.major}/events", json=payload)
            r.raise_for_status()
            self.last_contact = time.monotonic()
            return True
        except Exception:
            return False

    def close(self) -> None:
        self._client.close()
