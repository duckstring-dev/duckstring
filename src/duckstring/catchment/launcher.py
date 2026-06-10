"""Pond launcher: brings a Duck process to life and tears it down.

Local-subprocess only for now (one Duck per executing Pond). The Duck dials back to the Catchment, so
"remote" later is just a different launcher with the same interface — nothing else changes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


class SubprocessLauncher:
    manages_processes = True  # owns real Duck processes, so liveness can be checked via proc.poll()

    def __init__(self, root: Path, base_url: str, token: str = ""):
        self.root = root
        self.base_url = base_url
        self.token = token
        self._procs: dict[str, subprocess.Popen] = {}

    def is_running(self, pond_name: str) -> bool:
        proc = self._procs.get(pond_name)
        return proc is not None and proc.poll() is None

    def ensure(self, pond_name: str, version: str, source_path: str) -> None:
        if self.is_running(pond_name):
            return
        self._procs[pond_name] = subprocess.Popen(
            [
                sys.executable, "-m", "duckstring.duck",
                "--pond", pond_name,
                "--version", version,
                "--catchment", self.base_url,
                "--token", self.token,
                "--root", str(self.root),
                "--source-path", source_path,
            ]
        )

    def terminate(self, pond_name: str) -> None:
        proc = self._procs.pop(pond_name, None)
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def shutdown_all(self) -> None:
        for name in list(self._procs):
            self.terminate(name)


class NoopLauncher:
    """A launcher that never spawns anything — for tests/contexts that exercise the engine and
    persistence without running real Duck processes."""

    manages_processes = False  # nothing to watch — liveness checking is skipped

    def is_running(self, pond_name: str) -> bool:
        return False

    def ensure(self, pond_name: str, version: str, source_path: str) -> None:
        pass

    def terminate(self, pond_name: str) -> None:
        pass

    def shutdown_all(self) -> None:
        pass
