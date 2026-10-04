"""The duck channel's job delivery, and how hard a Duck asks for work.

Delivery is a poll, not a push: ``take_jobs`` DRAINS the queue, so holding the connection would let a
Duck killed mid-poll swallow work meant for its replacement (verified — it broke reset and
missing-source recovery, and ASGI offers no reliable disconnect signal for an idle GET to close that
window). Making it a true long poll needs a spawn token so the server knows which Duck owns the queue.

Until then the cost is controlled at the client: an idle Duck backs off instead of asking ten times a
second, which was invisible beside a local Catchment but meant continuous round-trips per REMOTE Duck —
and a log so noisy it hindered debugging a live pool agent.
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest

from duckstring.duck.__main__ import _POLL_MAX_S, _POLL_MIN_S, _poll_delay

pytestmark = pytest.mark.timeout(30)

_CFG = {"sources": {}, "immediate_retries": 0, "source_retries": 0, "kind": "inlet"}
_RIPPLES = [{"func": "f1", "name": "r1", "parents": []}]


def test_idle_polling_backs_off_and_is_bounded():
    delays = [_poll_delay(n) for n in range(8)]
    assert delays[0] == _POLL_MIN_S, "the first idle poll must stay responsive"
    assert delays == sorted(delays), "backoff must be monotonic"
    assert max(delays) == _POLL_MAX_S, "and bounded, or dispatch latency grows without limit"
    # The point of the change: an idle minute costs far fewer requests than a flat 0.1s would.
    flat = 60 / _POLL_MIN_S
    backed_off = 60 / _POLL_MAX_S
    assert backed_off <= flat / 10


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def channel(tmp_path, monkeypatch):
    """A live Catchment with one Pond and NO Ducks — so queued jobs stay queued for us to observe."""
    import uvicorn

    from duckstring.catchment.app import create_app
    from duckstring.catchment.db import connect, migrate
    from duckstring.catchment.routes.deploy import _register

    monkeypatch.setenv("DUCKSTRING_DISABLE_DUCKS", "1")
    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "src", "1.0.0", "inlet", "ponds/src/1.0.0", _CFG, _RIPPLES)
    db.close()

    app = create_app(tmp_path)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{url}/api/health", timeout=1.0)
            break
        except Exception:
            time.sleep(0.05)
    else:
        raise RuntimeError("Catchment did not start")
    yield url, app
    server.should_exit = True
    thread.join(timeout=5)


def test_the_poll_answers_at_once_and_delivers_queued_work(channel):
    """It must NOT hold: a held connection is what let a dead Duck drain another's queue."""
    url, app = channel
    started = time.monotonic()
    r = httpx.get(f"{url}/api/duck/src/1/jobs", params={"wait": 20.0}, timeout=10.0)
    assert r.status_code == 200 and r.json()["jobs"] == []
    assert time.monotonic() - started < 2.0, "the poll held the connection — see the module docstring"

    app.state.driver.pulse("src@1")
    jobs = httpx.get(f"{url}/api/duck/src/1/jobs", params={"wait": 20.0}, timeout=10.0).json()["jobs"]
    assert jobs and jobs[0]["kind"] == "begin_run"


def test_jobs_are_delivered_once(channel):
    """Delivery is destructive by design — the second reader gets nothing. This is precisely why a held
    connection is unsafe without knowing which Duck owns the queue."""
    url, app = channel
    app.state.driver.pulse("src@1")
    first = httpx.get(f"{url}/api/duck/src/1/jobs", timeout=10.0).json()["jobs"]
    second = httpx.get(f"{url}/api/duck/src/1/jobs", timeout=10.0).json()["jobs"]
    assert first and not second


# ─── an orphaned Duck exits ──────────────────────────────────────────────────────


class _IdleCore:
    pond_name = "p"
    events: list = []
    last_begin_f = None

    def idle(self) -> bool:
        return True

    def flush(self, post) -> None:
        pass


class _Executor:
    persist_dir = None

    def shutdown(self) -> None:
        pass


class _Client:
    """A Catchment link: unreachable (contact never refreshes), or answering, then sending a shutdown."""

    def __init__(self, *, reachable: bool, shutdown_after: int = 0):
        self.reachable, self.shutdown_after, self.polls = reachable, shutdown_after, 0
        self.last_contact = time.monotonic() - 3600

    def poll_jobs(self) -> list[dict]:
        self.polls += 1
        if not self.reachable:
            return []
        self.last_contact = time.monotonic()
        return [{"kind": "shutdown"}] if self.polls >= self.shutdown_after else []

    def post_event(self, payload: dict) -> bool:
        return self.reachable

    def close(self) -> None:
        pass


def test_an_idle_duck_with_no_catchment_exits():
    """Ducks are spawned automatically; one whose Catchment is gone for good must not run, and bill,
    forever."""
    from duckstring.duck.__main__ import serve

    started = time.monotonic()
    serve(_IdleCore(), _Executor(), _Client(reachable=False), orphan_after=0.5)
    assert time.monotonic() - started < 5


def test_an_idle_duck_in_contact_stays():
    from duckstring.duck.__main__ import serve

    client = _Client(reachable=True, shutdown_after=5)  # ~1.5 s of answered polls, then a shutdown
    started = time.monotonic()
    serve(_IdleCore(), _Executor(), client, orphan_after=0.5)
    assert client.polls >= 5 and time.monotonic() - started > 1.0  # only the shutdown ended it


def test_the_orphan_limit_comes_from_the_environment(monkeypatch):
    from duckstring.duck.__main__ import _orphan_after_s

    monkeypatch.delenv("DUCKSTRING_DUCK_ORPHAN_MINUTES", raising=False)
    assert _orphan_after_s() == 3600
    monkeypatch.setenv("DUCKSTRING_DUCK_ORPHAN_MINUTES", "5")
    assert _orphan_after_s() == 300
    monkeypatch.setenv("DUCKSTRING_DUCK_ORPHAN_MINUTES", "0")
    assert _orphan_after_s() is None


def test_the_catchment_sends_its_orphan_limit_with_each_run(tmp_path, monkeypatch):
    """A cloud Duck doesn't inherit the Catchment's environment, so the setting rides the job."""
    from duckstring.catchment.db import connect, migrate
    from duckstring.catchment.driver import Driver
    from duckstring.catchment.launcher import NoopLauncher
    from duckstring.catchment.routes.deploy import _register

    db = connect(tmp_path / "duck.db")
    migrate(db)
    _register(db, "src", "1.0.0", "inlet", "ponds/src/1.0.0", _CFG, _RIPPLES)
    d = Driver(db, tmp_path, "http://x", NoopLauncher())
    monkeypatch.setenv("DUCKSTRING_DUCK_ORPHAN_MINUTES", "15")
    d.pulse("src@1")
    (job,) = [j for j in d.take_jobs("src@1") if j["kind"] == "begin_run"]
    assert job["orphan_minutes"] == "15"
