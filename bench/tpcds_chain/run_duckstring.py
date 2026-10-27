"""The Duckstring run: deploy the chain to a fresh Catchment, build it, then apply each change batch.

    python run_duckstring.py --data LOC --root DIR [--data-root S3URI] [--batches 1] [--warm]
                             [--continuous K --every S] [--status-log FILE.jsonl]

Starts a Catchment on --root (deleted first) as a subprocess, deploys ponds/, and refreshes the three
Outlets. Each phase is timed from the Refresh request until every Pond has finished a Run at least as fresh
as the request. The first phase loads the base snapshot. Each later one releases the next change batch
(landing/RELEASED) and refreshes again.

With --warm, a daily Refresh is added to each Outlet after the first phase. It never comes due during the
benchmark, but it keeps every Pond's Duck running between phases, as a pipeline with a recurring Refresh
would. Without it each phase starts its Ducks cold.

--data may be local or s3:// (see paths.py). With --data-root, the Catchment publishes to that location (an
s3:// prefix, cleared first) instead of its state root: Ducks publish locally and a copy follows to the data
root, and a phase only ends once every Pond's copy has caught up (its persisted_f), so the time includes
getting the outputs onto S3, as the baselines' does when their --out is on S3.

With --continuous K, after the phases a continuous Refresh is added to each Outlet and K more batches are
released, one every --every seconds, on a fixed clock whether or not the pipeline has caught up (so work
stacks up when it can't keep up). The phase ends once every Pond is fresh past the last release (and, with a
data root, its copy has caught up); the Refreshes are then removed and the pipeline left to go idle. Each
batch's lag is the time from its release until all three Outlets were fresh past it.

With --status-log, /api/status is sampled every 0.25 s for the whole run and each change written as a JSON line
(wall-clock t, and per Pond its status, freshness stamps, memory slots and per-Ripple state), which shows
when each Pond waited for memory, started, finished and published.

Prints a JSON line per phase, and writes them all to --results, with the Catchment's complete Run history at
the end (every Pond Run and Ripple Run with its freshness, start and end, retries and errors) and the run's
events (phase starts, releases, Refreshes added and removed) on the wall clock.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import paths
import tomllib

HERE = Path(__file__).resolve().parent
PONDS = ("sales_feed", "dimensions", "sales_enriched", "category_month", "store_quarter", "customer_value")
OUTLETS = ("category_month", "store_quarter", "customer_value")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _zip(path: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(path.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                zf.write(f, f.relative_to(path))
    return buf.getvalue()


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


def start_catchment(root: Path, data: str, data_root: str | None) -> tuple[subprocess.Popen, str]:
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    env = {**os.environ, "DUCKSTRING_ROOT": str(root), "DUCKSTRING_CATCHMENT_URL": url,
           "TPCDS_BENCH_DATA": data}
    env.pop("DUCKSTRING_DISABLE_DUCKS", None)
    if data_root:
        env["DUCKSTRING_DATA_ROOT"] = data_root
    log = open(root.parent / f"{root.name}.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "duckstring.catchment.asgi:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{url}/api/health", timeout=1.0)
            return proc, url
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("the Catchment didn't start")


def deploy(url: str) -> None:
    for name in PONDS:
        path = HERE / "ponds" / name
        info = tomllib.loads((path / "pond.toml").read_text())["pond"]
        r = httpx.post(f"{url}/api/deploy",
                       files={"pond": ("pond.zip", _zip(path), "application/zip")},
                       data={"name": info["name"], "version": info["version"], "type": info.get("type", "pond")},
                       timeout=120.0)
        if r.is_error:
            raise RuntimeError(f"deploying {name}: {r.status_code} {r.text[:2000]}")


def status(url: str) -> dict[str, dict]:
    r = httpx.get(f"{url}/api/status", timeout=30.0)
    r.raise_for_status()
    return {p["name"]: p for p in r.json()["ponds"] if p["name"] in PONDS}


def wait_fresh(url: str, since: datetime, timeout: float, t0: float) -> float | None:
    """Wait until every Pond has completed a Run at least as fresh as ``since``, is idle, and (with a data root)
    has its copy there caught up. With no data root, persisted_f follows end_f. Returns the time, from ``t0``,
    at which every Pond had finished, before its copy caught up (None if both happened in one poll)."""
    deadline = time.monotonic() + timeout
    finished = None
    while time.monotonic() < deadline:
        ponds = status(url)
        failed = [n for n, p in ponds.items() if p.get("is_failed") or p.get("is_blocked")]
        if failed:
            failures = []  # the failed runs' errors and tracebacks, so the failure can be diagnosed afterwards
            for run in runs_since(url, since):
                for r in [run, *run.get("ripples", [])]:
                    if r.get("status") == "failed" or r.get("error"):
                        failures.append({"pond": run.get("pond"), "ripple": r.get("ripple"), "error": r.get("error"),
                                         "traceback": r.get("traceback")})
            raise RuntimeError(f"failed: {failed}\n" + json.dumps(failures, indent=1, default=str)[:20000])
        never = datetime.min.replace(tzinfo=timezone.utc)
        done = len(ponds) == len(PONDS) and all(
            (_parse(p.get("end_f")) or never) >= since and p.get("status") == "idle" for p in ponds.values())
        if done and finished is None:
            finished = time.perf_counter() - t0
        if done and all((_parse(p.get("persisted_f")) or never) >= since for p in ponds.values()):
            return finished
        time.sleep(0.05)
    raise TimeoutError(f"not fresh after {timeout}s: {json.dumps(status(url), default=str)[:2000]}")


def runs_since(url: str, since: datetime, limit: int = 200) -> list[dict]:
    r = httpx.get(f"{url}/api/runs", params={"ripples": "true", "limit": limit}, timeout=60.0)
    r.raise_for_status()
    return [run for run in r.json()["runs"] if (_parse(run.get("started_at")) or since) >= since]


def history(url: str) -> list[dict]:
    """Every Pond Run and Ripple Run the Catchment recorded, oldest first. Asked per Pond, so a long run isn't
    cut off by the route's limit; a Pond's runs come back with its upstream's, so they are deduplicated."""
    runs: dict[tuple[str, str], dict] = {}
    for name in PONDS:
        r = httpx.get(f"{url}/api/runs", params={"pond": name, "ripples": "true", "limit": 1000}, timeout=60.0)
        r.raise_for_status()
        for run in r.json()["runs"]:
            runs.setdefault((run["pond"], run["f"]), run)
    return sorted(runs.values(), key=lambda run: (run.get("started_at") or "", run["pond"]))


def _slim(p: dict) -> dict:
    keep = ("status", "gen", "start_f", "end_f", "changed_f", "persisted_f", "demand_f", "memory", "failures",
            "is_failed", "is_blocked")
    out = {k: p.get(k) for k in keep}
    out["ripples"] = {r["name"]: [r["status"], r.get("gen"), r.get("start_f"), r.get("end_f")]
                      for r in p.get("ripples", [])}
    return out


class StatusSampler:
    """Samples /api/status every ``every`` seconds on a thread, keeping (and writing) a record each time a Pond's
    state changes. The records are what the continuous phase's lags are read from."""

    def __init__(self, url: str, path: Path | None, every: float = 0.25):
        self.url, self.every = url, every
        self.out = open(path, "w", buffering=1) if path else None
        self.records: list[dict] = []
        self.errors = 0
        self._last: dict = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="status-sampler", daemon=True)

    def start(self) -> StatusSampler:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        if self.out:
            self.out.close()

    def _loop(self) -> None:
        with httpx.Client(timeout=10.0) as client:
            while not self._stop.is_set():
                t = time.time()
                try:
                    r = client.get(f"{self.url}/api/status")
                    r.raise_for_status()
                    ponds = {p["name"]: _slim(p) for p in r.json()["ponds"] if p["name"] in PONDS}
                except (httpx.HTTPError, ValueError, KeyError):
                    self.errors += 1
                    ponds = None
                if ponds is not None:
                    changed = {n: v for n, v in ponds.items() if self._last.get(n) != v}
                    if changed:
                        rec = {"t": round(t, 3), "ponds": changed}
                        self.records.append(rec)
                        if self.out:
                            self.out.write(json.dumps(rec) + "\n")
                        self._last.update(changed)
                self._stop.wait(max(0.0, self.every - (time.time() - t)))

    def first_fresh(self, since: datetime, ponds: tuple[str, ...], on_root: bool = False) -> float | None:
        """The first sampled wall time at which every one of ``ponds`` was fresh at or past ``since`` (its
        end_f) and, with ``on_root``, its output on the data root too (see ``_on_root``)."""
        state: dict[str, dict] = {}
        for rec in self.records:
            state.update(rec["ponds"])
            if all(n in state and _fresh(state[n], since) and (not on_root or _on_root(state[n])) for n in ponds):
                return rec["t"]
        return None


_NEVER = datetime.min.replace(tzinfo=timezone.utc)


def _fresh(p: dict, since: datetime) -> bool:
    return (_parse(p.get("end_f")) or _NEVER) >= since


def _on_root(p: dict) -> bool:
    """Whether a Pond's newest output is on the data root: its copy (persisted_f) has caught up with the last Run
    that changed it (changed_f). A pass changes nothing and copies nothing, so persisted_f can stay behind end_f
    after one; comparing with end_f would wait for a copy that never comes."""
    return (_parse(p.get("persisted_f")) or _NEVER) >= (_parse(p.get("changed_f")) or _NEVER)


EVENTS: list[dict] = []


def event(kind: str, **kw) -> dict:
    ev = {"t": round(time.time(), 3), "event": kind, **kw}
    EVENTS.append(ev)
    print(f"[event] {json.dumps(ev)}", file=sys.stderr)
    return ev


def continuous(url: str, data: str, first: int, count: int, every: float, timeout: float,
               sampler: StatusSampler) -> dict:
    """Releases batches first..first+count-1 every ``every`` seconds under a continuous Refresh on each Outlet."""
    t_start = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    event("continuous_start", first=first, count=count, every=every)
    for outlet in OUTLETS:
        httpx.post(f"{url}/api/ponds/{outlet}/refreshes", json={"continuous": True, "name": "bench"},
                   timeout=30.0).raise_for_status()
    event("refreshes_added")
    releases = []
    for i in range(count):
        k = first + i
        delay = t0 + i * every - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        paths.write_text(paths.join(data, "landing", "RELEASED"), str(k))
        ev = event("release", batch=k)  # after the write: a Run starting after this reads batch k
        releases.append((k, datetime.fromtimestamp(ev["t"], timezone.utc)))
    last = releases[-1][1]
    deadline, finished = time.monotonic() + timeout, None
    while True:  # every Pond fresh past the last release, then its output on the data root
        ponds = status(url)
        failed = [n for n, p in ponds.items() if p.get("is_failed") or p.get("is_blocked")]
        if failed:
            raise RuntimeError(f"failed during the continuous phase: {failed}")
        if len(ponds) == len(PONDS) and all(_fresh(p, last) for p in ponds.values()):
            finished = finished or time.perf_counter() - t0
            if all(_on_root(p) for p in ponds.values()):
                break
        if time.monotonic() > deadline:
            raise TimeoutError(f"not caught up after {timeout}s: {json.dumps(ponds, default=str)[:2000]}")
        time.sleep(0.05)
    seconds = time.perf_counter() - t0
    event("caught_up", seconds=round(seconds, 2))
    for outlet in OUTLETS:
        httpx.post(f"{url}/api/ponds/{outlet}/refreshes/bench/remove", timeout=30.0).raise_for_status()
    event("refreshes_removed")
    wait_idle(url, timeout)
    event("idle")
    time.sleep(1.0)  # one more status sample after going idle
    lags = []
    for k, t_rel in releases:
        done = sampler.first_fresh(t_rel, OUTLETS)
        on_root = sampler.first_fresh(t_rel, OUTLETS, on_root=True)
        lags.append({"batch": k, "released": t_rel.isoformat(),
                     "lag": round(done - t_rel.timestamp(), 2) if done else None,
                     "lag_on_root": round(on_root - t_rel.timestamp(), 2) if on_root else None})
    print(f"[continuous] caught up {seconds:.2f}s after the first release; lags "
          f"{[x['lag'] for x in lags]}", file=sys.stderr)
    return {"phase": "continuous", "seconds": round(seconds, 2), "finished_seconds": round(finished or seconds, 2),
            "every": every, "first_batch": first, "batches": count, "lags": lags,
            "runs": runs_since(url, t_start, limit=1000)}


def wait_idle(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ponds = status(url)
        if all(p.get("status") == "idle" and _on_root(p) for p in ponds.values()):
            return
        time.sleep(0.1)
    raise TimeoutError("the pipeline didn't go idle")


def phase(url: str, label: str, timeout: float) -> dict:
    t_req = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    event("phase_start", phase=label, at=t_req.isoformat())
    # One freshness for all three, so the phase is one wave of Runs rather than one per request.
    for outlet in OUTLETS:
        httpx.post(f"{url}/api/ponds/{outlet}/refresh", params={"at": t_req.isoformat()},
                   timeout=30.0).raise_for_status()
    finished = wait_fresh(url, t_req, timeout, t0)
    seconds = time.perf_counter() - t0
    event("phase_end", phase=label, seconds=round(seconds, 2))
    t1 = time.perf_counter()
    runs = runs_since(url, t_req)
    print(f"[{label}] {seconds:.2f}s, run history {time.perf_counter() - t1:.2f}s", file=sys.stderr)
    per_pond = {}
    for run in runs:
        start, end = _parse(run.get("started_at")), _parse(run.get("finished_at"))
        if start and end:
            per_pond[run["pond"]] = round((end - start).total_seconds(), 2)
    # seconds: until every Pond's output was on its data root (S3); finished: until every Pond had finished,
    # its output published on this machine, where Sinks here and queries read it.
    return {"phase": label, "seconds": round(seconds, 2), "finished_seconds": round(finished or seconds, 2),
            "pond_run_seconds": per_pond, "runs": runs}


def du(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="the generated data: a local path or s3:// URI")
    ap.add_argument("--root", type=Path, required=True, help="the Catchment's state root (deleted first)")
    ap.add_argument("--data-root", help="an s3:// prefix for the Catchment to publish to (cleared first)")
    ap.add_argument("--batches", type=int, default=1)
    ap.add_argument("--warm", action="store_true")
    ap.add_argument("--continuous", type=int, default=0, help="batches to release under a continuous Refresh")
    ap.add_argument("--every", type=float, default=10.0, help="seconds between the continuous phase's releases")
    ap.add_argument("--status-log", type=Path)
    ap.add_argument("--timeout", type=float, default=7200)
    ap.add_argument("--results", type=Path)
    args = ap.parse_args()
    data = args.data if paths.is_remote(args.data) else str(Path(args.data).resolve())
    root = args.root.resolve()
    if args.data_root:
        paths.remove(args.data_root)

    paths.write_text(paths.join(data, "landing", "RELEASED"), "0")
    proc, url = start_catchment(root, data, args.data_root)
    event("catchment_started", url=url)
    results, runs, sampler = [], [], None
    try:
        deploy(url)
        event("deployed")
        sampler = StatusSampler(url, args.status_log).start()
        results.append(phase(url, "initial", args.timeout))
        if args.warm:
            t1 = time.perf_counter()
            for outlet in OUTLETS:
                httpx.post(f"{url}/api/ponds/{outlet}/refreshes", json={"every_seconds": 86400},
                           timeout=30.0).raise_for_status()
            print(f"[warm] refreshes added in {time.perf_counter() - t1:.2f}s", file=sys.stderr)
        for k in range(1, args.batches + 1):
            paths.write_text(paths.join(data, "landing", "RELEASED"), str(k))
            event("release", batch=k)
            results.append(phase(url, f"batch_{k}", args.timeout))
        if args.continuous:
            results.append(continuous(url, data, args.batches + 1, args.continuous, args.every, args.timeout,
                                      sampler))
        runs = history(url)
        for r in results:
            print(json.dumps({k: v for k, v in r.items() if k != "runs"}))
        sizes = {"root_bytes": du(root), "data_root_bytes": paths.size(args.data_root) if args.data_root else None}
        print(json.dumps(sizes))
    finally:
        if sampler is not None:
            sampler.stop()
            if sampler.errors:
                print(f"[status] {sampler.errors} failed samples", file=sys.stderr)
        if not runs:  # a failed run: keep what history there is
            try:
                runs = history(url)
            except (httpx.HTTPError, ValueError):
                pass
        if args.results:
            args.results.write_text(json.dumps({"warm": args.warm, "data": data, "data_root": args.data_root,
                                                "phases": results, "root_bytes": du(root), "events": EVENTS,
                                                "history": runs, "env": _env()}, indent=1, default=str))
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


def _env() -> dict:
    """The settings a run depends on: the memory slots and budget, and the measurement shims."""
    secret = ("KEY", "TOKEN", "SECRET", "PASSWORD")
    return {k: v for k, v in os.environ.items()
            if k.startswith(("DUCKSTRING_", "DS_")) and not any(w in k for w in secret)}


if __name__ == "__main__":
    main()
