"""Serving an App locally against its Puddles (plans/apps.md): ``duckstring pond run`` in an App's directory.

The App's process gets the same environment the Catchment gives it, with its handle rooted at ``puddles/`` (so
``read_table("sales.revenue")`` reads the Puddle), its state directory at ``puddles/app/state/`` and an empty base
path, so it serves at ``http://127.0.0.1:{port}/``. A static-only App is served with Python's own file server.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ..app import ENV, app_config
from .project import Project


def app_env(project: Project, port: int) -> dict[str, str]:
    cfg = app_config(project.info, project.dir)
    env = dict(os.environ)
    # As on the Catchment: the environment's bin first, so a command it installs (streamlit, gunicorn) is found.
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["PORT"] = str(port)
    env["DUCKSTRING_APP_PATH"] = ""
    env["PYTHONUNBUFFERED"] = "1"
    if cfg["state"] == "local":
        state = project.puddles_dir / "app" / "state"
        state.mkdir(parents=True, exist_ok=True)
        env["DUCKSTRING_APP_STATE"] = str(state)
    env[ENV] = json.dumps({"name": project.name, "major": int(project.version.split(".")[0]),
                           "version": project.version, "root": str(project.puddles_dir),
                           "sources": list(project.sources)})
    return env


def serve_app(project: Project, port: int) -> int:
    """Run the App in the foreground until it exits or is interrupted. Returns its exit code."""
    cfg = app_config(project.info, project.dir)
    env = app_env(project, port)
    if cfg["command"]:
        argv = ["/bin/sh", "-c", "exec " + cfg["command"]]
    else:
        argv = [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1",
                "--directory", str(project.dir / cfg["static"])]
    proc = subprocess.Popen(argv, cwd=project.dir, env=env)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            return proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.wait()
