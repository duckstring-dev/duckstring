"""Pond launcher: brings a Duck process to life and tears it down.

Local-subprocess only for now (one Duck per executing Pond — that is, per ``name@major`` line). The
Duck dials back to the Catchment, so "remote" later is just a different launcher with the same
interface — nothing else changes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ..keys import split_pond_key


class SubprocessLauncher:
    manages_processes = True  # owns real Duck processes, so liveness can be checked via proc.poll()

    def __init__(self, root: Path, base_url: str, token: str = ""):
        self.root = root
        self.base_url = base_url
        self.token = token
        self._procs: dict[str, subprocess.Popen] = {}  # pond key (name@major) → process

    def is_running(self, pond_key: str) -> bool:
        proc = self._procs.get(pond_key)
        return proc is not None and proc.poll() is None

    def ensure(self, pond_key: str, version: str, source_path: str) -> None:
        if self.is_running(pond_key):
            return
        name, major = split_pond_key(pond_key)
        self._procs[pond_key] = subprocess.Popen(
            [
                sys.executable, "-m", "duckstring.duck",
                "--pond", name,
                "--major", str(major),
                "--version", version,
                "--catchment", self.base_url,
                "--token", self.token,
                "--root", str(self.root),
                "--source-path", source_path,
            ]
        )

    def terminate(self, pond_key: str) -> None:
        proc = self._procs.pop(pond_key, None)
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def shutdown_all(self) -> None:
        for key in list(self._procs):
            self.terminate(key)


class NoopLauncher:
    """A launcher that never spawns anything — for tests/contexts that exercise the engine and
    persistence without running real Duck processes."""

    manages_processes = False  # nothing to watch — liveness checking is skipped

    def is_running(self, pond_key: str) -> bool:
        return False

    def ensure(self, pond_key: str, version: str, source_path: str) -> None:
        pass

    def terminate(self, pond_key: str) -> None:
        pass

    def shutdown_all(self) -> None:
        pass
