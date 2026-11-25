"""A stand-in for remote compute (plans/app-scaling.md): each spawn runs as a subprocess with its own empty root, so
it shares nothing with the Catchment but the network, as a machine elsewhere would. It reports every Duck as off
this machine, so App instances take the remote path: the agent, the tunnel, data reached directly."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psutil

from duckstring.keys import split_pond_key, worker_module


class FakeRemoteLauncher:
    manages_processes = True

    def __init__(self, base_url: str, token: str, machines: Path, data_root: str | None = None):
        self.base_url, self.token, self.machines, self.data_root = base_url, token, Path(machines), data_root
        self.procs: dict[str, subprocess.Popen] = {}
        self.spawned: list[tuple[str, dict | None]] = []

    def on_this_machine(self, duck: dict | None) -> bool:
        return False

    def ensure(self, pond_key: str, version: str, source_path: str, duck: dict | None = None) -> None:
        if self.is_running(pond_key):
            return
        name, major = split_pond_key(pond_key)
        root = self.machines / pond_key.replace("@", "-").replace("~", "-")
        root.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("DUCKSTRING_") or k in ("DUCKSTRING_SLEEP_MULTIPLIER", "DUCKSTRING_APP_ORPHAN_MINUTES")}
        argv = [sys.executable, "-m", worker_module(duck), "--pond", name, "--major", str(major), "--version", version,
                "--catchment", self.base_url, f"--token={self.token}", "--root", str(root),
                "--source-path", source_path, f"--data-root={self.data_root or ''}"]
        self.spawned.append((pond_key, duck))
        self.procs[pond_key] = subprocess.Popen(argv, env=env, stdout=open(root / "agent.log", "ab"),
                                                stderr=subprocess.STDOUT)

    def is_running(self, pond_key: str) -> bool:
        proc = self.procs.get(pond_key)
        return proc is not None and proc.poll() is None

    def launch_error(self, pond_key: str) -> str | None:
        return None

    def exit_code(self, pond_key: str) -> int | None:
        proc = self.procs.get(pond_key)
        return proc.poll() if proc is not None else None

    def release(self, pond_key: str) -> None:
        pass

    def terminate(self, pond_key: str, wait: bool = False) -> None:
        proc = self.procs.get(pond_key)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()

    def kill(self, pond_key: str) -> None:
        """The machine vanishing: the agent gets no say, and nothing it started survives (the App runs in its own
        process group, so killing the agent alone would leave it serving, as no vanished machine does)."""
        proc = self.procs.get(pond_key)
        if proc is not None:
            if proc.poll() is None:
                for child in psutil.Process(proc.pid).children(recursive=True):
                    child.kill()
            proc.kill()
            proc.wait()

    def shutdown_all(self) -> None:
        for key in list(self.procs):
            self.terminate(key)

    def set_base_url(self, url: str) -> None:
        self.base_url = url

    def set_data_root(self, data_root: str | None) -> None:
        self.data_root = data_root
