"""Pond environments: each Pond runs in the Python environment its own ``pyproject.toml`` and ``uv.lock``
declare (see plans/pond-environments.md).

A Pond without a ``pyproject.toml`` runs in the default environment, the Catchment's own Python. A Pond
with one must also have a ``uv.lock``; the Catchment builds the environment it describes, plus the
Catchment's own Duckstring, into ``{root}/envs/{hash}/``. The hash covers everything that decides the
environment's contents, so Ponds with identical locks share one environment and an unchanged redeploy
reuses it. uv links packages from its cache rather than copying them, so builds are quick.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ENV_MARKER = ".duckstring_env"  # written into a deployed Pond's source dir: the hash of its environment
_COMPLETE = ".complete"          # written into an environment once it's fully built


class EnvError(RuntimeError):
    """A Pond's environment can't be built: a missing or stale lock, or a failed install (uv's output)."""


@dataclass(frozen=True)
class EnvSpec:
    source_dir: Path
    pyproject: bytes
    lock: bytes
    python_version: bytes | None  # the Pond's .python-version pin; None = the Catchment's own interpreter


def pond_env(source_dir: Path) -> EnvSpec | None:
    """The environment a Pond declares, or ``None`` for the default environment (no ``pyproject.toml``)."""
    source_dir = Path(source_dir)
    pyproject = source_dir / "pyproject.toml"
    if not pyproject.is_file():
        return None
    lock = source_dir / "uv.lock"
    if not lock.is_file():
        raise EnvError(
            "the Pond has a pyproject.toml but no uv.lock: run `uv lock` in the Pond's directory and deploy again"
        )
    pin = source_dir / ".python-version"
    return EnvSpec(source_dir, pyproject.read_bytes(), lock.read_bytes(), pin.read_bytes() if pin.is_file() else None)


def _extras() -> list[str]:
    """Duckstring extras the Catchment itself has, so a Duck never lacks one it relies on."""
    from importlib.util import find_spec

    extras = []
    if find_spec("s3fs") and find_spec("boto3"):
        extras.append("aws")
    if find_spec("sqlglot"):
        extras.append("lineage")
    return extras


def duckstring_requirement() -> list[str]:
    """``uv pip install`` arguments installing the Catchment's own Duckstring (with its extras): the local
    source for a development checkout, otherwise the same released version from the package index."""
    from importlib import metadata
    from urllib.parse import unquote, urlparse

    dist = metadata.distribution("duckstring")
    extras = _extras()
    suffix = f"[{','.join(extras)}]" if extras else ""
    direct = dist.read_text("direct_url.json")
    if direct:
        info = json.loads(direct)
        url = info.get("url", "")
        if url.startswith("file://"):
            path = unquote(urlparse(url).path)
            if info.get("dir_info", {}).get("editable"):
                return ["-e", f"{path}{suffix}"]
            return [f"duckstring{suffix} @ {url}"]
    return [f"duckstring{suffix}=={dist.version}"]


def _interpreter(spec: EnvSpec) -> list[str]:
    """uv's ``--python`` for the environment: the Catchment's own interpreter, unless the Pond pins a
    version in ``.python-version`` (which uv then honours, installing that Python if it must)."""
    return [] if spec.python_version is not None else ["--python", sys.executable]


def env_hash(spec: EnvSpec) -> str:
    """Identifies an environment by everything that decides its contents."""
    interpreter = spec.python_version or f"{sys.executable}|{sys.version}".encode()
    h = hashlib.sha256()
    for part in (spec.pyproject, spec.lock, interpreter, json.dumps(duckstring_requirement()).encode()):
        h.update(hashlib.sha256(part).digest())
    return h.hexdigest()[:24]


def _python_in(env_dir: Path) -> Path:
    return env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _uv() -> str:
    try:
        from uv import find_uv_bin

        return find_uv_bin()
    except ImportError:
        found = shutil.which("uv")
        if found:
            return found
        raise EnvError("uv isn't installed; it's needed to build Pond environments (pip install uv)") from None


def _run_uv(args: list[str], *, cwd: Path, env: dict | None = None) -> None:
    full_env = {**os.environ, **(env or {})}
    full_env.pop("VIRTUAL_ENV", None)  # the Catchment's own venv; uv would warn that it's ignoring it
    result = subprocess.run([_uv(), *args], cwd=cwd, env=full_env, capture_output=True, text=True)
    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip()
        raise EnvError(f"`uv {args[0]}` failed:\n{output[-4000:]}")


class _FileLock:
    """An exclusive lock on a file, so concurrent deploys of one environment build it once."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w")  # noqa: SIM115 — held until __exit__
        try:
            import fcntl

            fcntl.flock(self._fh, fcntl.LOCK_EX)
        except ImportError:  # no fcntl (Windows): best effort, no lock
            pass
        return self

    def __exit__(self, *exc):
        self._fh.close()


def ensure_env(root: Path, source_dir: Path) -> Path:
    """The Python interpreter a Pond runs in, building its environment first if it isn't built yet.
    ``sys.executable`` for the default environment. Raises :class:`EnvError` on a missing/stale lock or a
    failed build (with uv's output). Also records the environment in the Pond's source dir
    (:data:`ENV_MARKER`), for :func:`python_for`."""
    source_dir = Path(source_dir)
    marker = source_dir / ENV_MARKER
    spec = pond_env(source_dir)
    if spec is None:
        marker.unlink(missing_ok=True)
        return Path(sys.executable)
    digest = env_hash(spec)
    env_dir = Path(root) / "envs" / digest
    with _FileLock(Path(root) / "envs" / f"{digest}.lock"):
        if not (env_dir / _COMPLETE).exists():
            shutil.rmtree(env_dir, ignore_errors=True)  # a previous build that didn't finish
            _run_uv(["sync", "--locked", "--no-install-project", "--no-dev", "--quiet", *_interpreter(spec)],
                    cwd=source_dir, env={"UV_PROJECT_ENVIRONMENT": str(env_dir)})
            _run_uv(["pip", "install", "--quiet", "--python", str(_python_in(env_dir)), *duckstring_requirement()],
                    cwd=source_dir)
            (env_dir / _COMPLETE).write_text(digest)
    marker.write_text(digest)
    return _python_in(env_dir)


def python_for(root: Path, source_path: str | Path) -> str:
    """The interpreter a deployed Pond's Duck runs with: its built environment's, or the Catchment's own
    for the default environment (or an environment that isn't built on this machine)."""
    source_dir = Path(root) / source_path
    marker = source_dir / ENV_MARKER
    if marker.is_file():
        env_dir = Path(root) / "envs" / marker.read_text().strip()
        if (env_dir / _COMPLETE).exists():
            return str(_python_in(env_dir))
    return sys.executable


REEXEC_ENV = "DUCKSTRING_IN_POND_ENV"  # set on a re-executed local command, so it never re-executes again


def local_python(pond_dir: Path) -> Path | None:
    """The interpreter a local command (``pond run``/``hydrate``) should re-run itself under: the Pond's
    ``.venv`` (created by ``uv sync``) when the Pond declares an environment and this process isn't
    already running in it. ``None`` means carry on in this process."""
    pond_dir = Path(pond_dir)
    if os.environ.get(REEXEC_ENV) or not (pond_dir / "pyproject.toml").is_file():
        return None
    venv = pond_dir / ".venv"
    python = _python_in(venv)
    if not python.exists():
        return None
    if Path(sys.prefix).resolve() == venv.resolve():
        return None
    return python


def has_duckstring(python: Path) -> bool:
    result = subprocess.run([str(python), "-c", "import duckstring"], capture_output=True)
    return result.returncode == 0
