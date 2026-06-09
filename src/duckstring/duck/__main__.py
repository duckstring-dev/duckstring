"""Duck entrypoint: ``python -m duckstring.duck --pond NAME --version V --catchment URL --token T``.

Wires :class:`DuckCore` (engine + ledger) to the :class:`RippleExecutor` and the long-poll
:class:`CatchmentClient`, then serves until the Catchment tells it to shut down (Pond idle). The Duck
keeps draining in-flight Pond Runs and buffering events regardless of Catchment availability.
"""

from __future__ import annotations

import argparse
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..engine import pond as ledger
from .client import CatchmentClient
from .core import DuckCore
from .executor import RippleExecutor, load_topology


def _now() -> datetime:
    return datetime.now(timezone.utc)


def serve(core: DuckCore, executor: RippleExecutor, client: CatchmentClient) -> None:
    """Single-threaded event loop fed by a poll thread (jobs) and executor callbacks (completions)."""
    q: queue.Queue = queue.Queue()
    stop = threading.Event()

    def _poll_loop():
        while not stop.is_set():
            jobs = client.poll_jobs()
            for job in jobs:
                q.put(("job", job))
            if not jobs:
                stop.wait(0.1)  # short poll interval; avoids busy-spinning the Catchment

    def _launch(names):
        for name in names:
            executor.submit(
                name,
                on_done=lambda n, started, finished: q.put(("done", (n, started, finished))),
                on_error=lambda n, exc, started, finished: q.put(("error", (n, exc, started, finished))),
            )

    poller = threading.Thread(target=_poll_loop, daemon=True)
    poller.start()

    shutdown_requested = False
    try:
        while True:
            try:
                kind, data = q.get(timeout=1.0)
            except queue.Empty:
                kind, data = None, None

            if kind == "job":
                if data.get("kind") == "shutdown":
                    shutdown_requested = True
                elif data.get("kind") == "begin_run":
                    _launch(core.begin_run(
                        datetime.fromisoformat(data["f"]), _now(),
                        retry_immediately=data.get("immediate_retries", 0),
                    ))
            elif kind == "done":
                name, started, finished = data
                _launch(core.ripple_completed(
                    name, _now(), started_at=started, finished_at=finished, export=executor.export
                ))
            elif kind == "error":
                name, exc, started, finished = data
                print(f"[duck:{core.pond_name}] ripple {name} failed: {exc}", flush=True)
                _launch(core.ripple_failed(name, _now(), started_at=started, finished_at=finished))

            core.flush(client.post_event)

            if shutdown_requested and core.idle() and not core.events:
                break
    finally:
        stop.set()
        executor.shutdown()
        client.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="duckstring.duck")
    ap.add_argument("--pond", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--catchment", required=True)
    ap.add_argument("--token", default="")
    ap.add_argument("--root", required=True)
    ap.add_argument("--source-path", required=True, help="pond source dir relative to root")
    args = ap.parse_args()

    root = Path(args.root)
    parents = load_topology(root / args.source_path)
    con = ledger.connect(root / "ponds" / args.pond / "pond.db")
    core = DuckCore(args.pond, con, parents)
    executor = RippleExecutor(args.pond, args.version, args.source_path, root)
    client = CatchmentClient(args.catchment, args.pond, args.token)
    serve(core, executor, client)


if __name__ == "__main__":
    main()
