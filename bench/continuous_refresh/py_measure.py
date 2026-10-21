"""A continuous refresh (Tide, bound 0) against a Wave under noisy run times, in the Python engine.

Run from the repository root (it uses the test harness in tests/test_engine.py):

    python bench/continuous_refresh/py_measure.py 600 8 > bench/continuous_refresh/results/py.jsonl

Each Ripple run takes its base duration times exp(sigma * z), z standard normal, drawn from a hash of
(seed, Ripple, run number): both modes, and both engines (ts_measure.mts), see the same duration for the
k-th run of a Ripple.
"""

import json
import math
import sys
from datetime import timedelta
from multiprocessing import Pool

sys.path.insert(0, "tests")

from test_engine import THROTTLE_TOPOLOGIES, Driver, _throttle_graph, secs  # noqa: E402

from duckstring.engine import NEVER, Trigger, sentinel  # noqa: E402

M32 = 0xFFFFFFFF


def fmix(x: int) -> int:
    """murmur3's 32-bit finaliser."""
    x &= M32
    x ^= x >> 16
    x = (x * 0x85EBCA6B) & M32
    x ^= x >> 13
    x = (x * 0xC2B2AE35) & M32
    return x ^ (x >> 16)


def fnv(s: str) -> int:
    h = 0x811C9DC5
    for ch in s.encode():
        h = ((h ^ ch) * 0x01000193) & M32
    return h


def mult(seed: int, rid: str, k: int, sigma: float) -> float:
    """The k-th run's duration multiplier for a Ripple (lognormal, Box-Muller from two hashed uniforms)."""
    if sigma == 0:
        return 1.0
    key = fmix(fnv(rid) ^ fmix(seed * 0x9E3779B9 + k))
    u1 = (fmix(key ^ 0x68E31DA4) + 1) / 4294967297
    u2 = (fmix(key ^ 0xB5297A4D) + 1) / 4294967297
    return math.exp(sigma * math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2))


class Noisy(Driver):
    """The test Driver, with each Ripple run's duration drawn from ``mult``."""

    def __init__(self, state, durations, seed: int, sigma: float):
        super().__init__(state, durations)
        self.seed, self.sigma, self.work_ms = seed, sigma, 0

    def _react(self) -> None:
        self.state, started = sentinel(self.now, self.state)
        for rid in started:
            k = self.state.ripple_states[rid].runs_started
            ms = max(1, round(self.durations[rid] / timedelta(milliseconds=1) * mult(self.seed, rid, k, self.sigma)))
            self.inflight[rid] = self.now + timedelta(milliseconds=ms)
            self.work_ms += ms


def run(args: tuple) -> dict:
    name, mode, sigma, seed, seconds = args
    _, outlet, inlet, bottleneck = THROTTLE_TOPOLOGIES[name]
    s, dur = _throttle_graph(name)
    s.triggers = {outlet: Trigger(outlet, "wave") if mode == "wave" else Trigger(outlet, "tide", secs(0))}
    d = Noisy(s, dur, seed, sigma)
    last, ages, half = NEVER, [], {}
    for i in range(seconds * 10):
        if i == seconds * 5:
            half = {p: ps.runs_completed for p, ps in d.state.pond_states.items()}
        d.advance()
        d.process()
        end = d.state.pond_states[outlet].end_f
        if end != last:
            last = end
            if i >= seconds * 5:
                ages.append((d.now - end).total_seconds())
    runs = {p: ps.runs_completed for p, ps in d.state.pond_states.items()}
    ages.sort()
    return dict(
        engine="py", topo=name, mode=mode, sigma=sigma, seed=seed,
        outlet=runs[outlet] - half[outlet], bottleneck=runs[bottleneck] - half[bottleneck],
        inlet=runs[inlet] - half[inlet], total=sum(runs[p] - half[p] for p in runs), work_s=d.work_ms / 1000,
        age=sum(ages) / max(1, len(ages)), age95=ages[int(0.95 * (len(ages) - 1))] if ages else None,
    )


if __name__ == "__main__":
    seconds, seeds = int(sys.argv[1]), int(sys.argv[2])
    jobs = [(name, mode, sigma, seed, seconds) for name in THROTTLE_TOPOLOGIES for mode in ("wave", "cont")
            for sigma in (0.0, 0.25, 0.5, 1.0) for seed in range(seeds)]
    with Pool() as pool:
        for r in pool.imap(run, jobs, chunksize=4):
            print(json.dumps(r), flush=True)
