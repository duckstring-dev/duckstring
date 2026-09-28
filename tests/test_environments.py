"""Pond environments (plans/pond-environments.md): a Pond with a pyproject.toml + uv.lock runs in its own
environment, built by the Catchment; a Pond without one runs in the Catchment's own Python."""

from __future__ import annotations

import io
import sys
import textwrap
import zipfile
from importlib.util import find_spec
from pathlib import Path

import pytest

from duckstring import environments as envs
from duckstring.environments import ENV_MARKER, EnvError, ensure_env, env_hash, pond_env, python_for

pytestmark = pytest.mark.timeout(10)


def _write(base: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    return base


# ── the spec and its hash ─────────────────────────────────────────────────────


def test_no_pyproject_is_the_default_environment(tmp_path):
    assert pond_env(tmp_path) is None
    (tmp_path / ENV_MARKER).write_text("stale")
    assert ensure_env(tmp_path / "root", tmp_path) == Path(sys.executable)
    assert not (tmp_path / ENV_MARKER).exists()


def test_a_pyproject_needs_a_lock(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'p'\n")
    with pytest.raises(EnvError, match="uv lock"):
        pond_env(tmp_path)


def test_the_hash_follows_the_lock_and_the_python_pin(tmp_path):
    _write(tmp_path, {"pyproject.toml": "[project]\nname = 'p'\n", "uv.lock": "version = 1\n"})
    first = env_hash(pond_env(tmp_path))
    assert env_hash(pond_env(tmp_path)) == first  # stable
    (tmp_path / "uv.lock").write_text("version = 1\n# changed\n")
    changed = env_hash(pond_env(tmp_path))
    assert changed != first
    (tmp_path / ".python-version").write_text("3.12\n")
    assert env_hash(pond_env(tmp_path)) != changed


def test_duckstring_requirement_is_the_catchments_own():
    req = envs.duckstring_requirement()
    assert req[0] == "-e" or req[0].startswith("duckstring")  # a dev checkout, or a released version


# ── where a deployed Pond's Duck runs ──────────────────────────────────────────


def test_python_for_reads_the_marker(tmp_path):
    source = tmp_path / "ponds" / "p" / "1.0.0"
    source.mkdir(parents=True)
    assert python_for(tmp_path, "ponds/p/1.0.0") == sys.executable  # default environment

    (source / ENV_MARKER).write_text("abc")
    assert python_for(tmp_path, "ponds/p/1.0.0") == sys.executable  # not built here: fall back

    (tmp_path / "envs" / "abc").mkdir(parents=True)
    (tmp_path / "envs" / "abc" / envs._COMPLETE).write_text("abc")
    assert python_for(tmp_path, "ponds/p/1.0.0") == str(envs._python_in(tmp_path / "envs" / "abc"))


def test_the_launcher_spawns_the_ducks_in_the_ponds_environment(tmp_path, monkeypatch):
    from duckstring.catchment import launcher as launcher_mod

    source = tmp_path / "ponds" / "p" / "1.0.0"
    source.mkdir(parents=True)
    (source / ENV_MARKER).write_text("abc")
    (tmp_path / "envs" / "abc").mkdir(parents=True)
    (tmp_path / "envs" / "abc" / envs._COMPLETE).write_text("abc")

    spawned = []

    class FakePopen:
        def __init__(self, argv, **kwargs):
            spawned.append(argv)

        def poll(self):
            return None

    monkeypatch.setattr(launcher_mod.subprocess, "Popen", FakePopen)
    launcher = launcher_mod.SubprocessLauncher(tmp_path, "http://127.0.0.1:1", "token")
    launcher.ensure("p@1", "1.0.0", "ponds/p/1.0.0")
    assert spawned[0][0] == str(envs._python_in(tmp_path / "envs" / "abc"))


# ── local runs re-execute under the Pond's .venv ───────────────────────────────


def test_local_python(tmp_path, monkeypatch):
    monkeypatch.delenv(envs.REEXEC_ENV, raising=False)
    assert envs.local_python(tmp_path) is None  # no pyproject.toml
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'p'\n")
    assert envs.local_python(tmp_path) is None  # no .venv yet
    venv_python = envs._python_in(tmp_path / ".venv")
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("")
    assert envs.local_python(tmp_path) == venv_python
    monkeypatch.setenv(envs.REEXEC_ENV, "1")
    assert envs.local_python(tmp_path) is None  # already re-executed: never loop


# ── a real build (uv, and the package index or uv's cache) ─────────────────────

_TINYLIB = {
    "vendor/tinylib/pyproject.toml": """
        [project]
        name = "tinylib"
        version = "0.1.0"

        [build-system]
        requires = ["uv_build>=0.5"]
        build-backend = "uv_build"

        [tool.uv.build-backend]
        module-root = ""
    """,
    "vendor/tinylib/tinylib/__init__.py": "ANSWER = 42\n",
}

_ENV_POND = {
    "pond.toml": '[pond]\nname = "envp"\nversion = "1.0.0"\ntype = "inlet"\n',
    "pyproject.toml": """
        [project]
        name = "envp"
        version = "1.0.0"
        requires-python = ">=3.10"
        dependencies = ["tinylib"]

        [tool.uv.sources]
        tinylib = { path = "vendor/tinylib" }
    """,
    "src/pond.py": """
        import tinylib
        from duckstring import ripple

        @ripple
        def answer(pond):
            pond.write_table("answer", pond.con.sql(f"SELECT {tinylib.ANSWER} AS n"))
    """,
    **_TINYLIB,
}

_OFFLINE = ("Failed to fetch", "failed to lookup", "dns error", "Could not connect", "network", "offline")


def _locked_env_pond(tmp_path: Path) -> Path:
    pond = _write(tmp_path / "envp", _ENV_POND)
    try:
        envs._run_uv(["lock", "--quiet", "--python", sys.executable], cwd=pond)
    except EnvError as exc:
        if any(s.lower() in str(exc).lower() for s in _OFFLINE):
            pytest.skip(f"uv can't reach the package index: {exc}")
        raise
    return pond


def _zip(pond: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path in pond.rglob("*"):
            if path.is_file() and ".venv" not in path.parts:
                zf.write(path, path.relative_to(pond).as_posix())
    return buf.getvalue()


@pytest.mark.timeout(300)
def test_deploy_builds_the_ponds_environment(tmp_path, catchment_client):
    """The Pond imports a package the Catchment doesn't have. Deploy builds its environment, discovers
    its Ripple there, and points its Duck at that environment's Python."""
    assert find_spec("tinylib") is None
    pond = _locked_env_pond(tmp_path)

    r = catchment_client.post(
        "/api/deploy",
        files={"pond": ("pond.zip", _zip(pond), "application/zip")},
        data={"name": "envp", "version": "1.0.0", "type": "inlet"},
    )
    if r.status_code == 422 and any(s.lower() in r.text.lower() for s in _OFFLINE):
        pytest.skip("uv can't reach the package index")
    assert r.status_code == 200, r.text
    db = catchment_client.app.state.db
    assert [n for (n,) in db.execute("SELECT name FROM ripple")] == ["answer"]

    root = catchment_client.app.state.root
    python = python_for(root, "ponds/envp/1.0.0")
    assert python != sys.executable and Path(python).is_relative_to(root / "envs")

    # A stale lock (a dependency the lock doesn't have) is refused, and the deployed copy is kept.
    _write(pond, {
        "vendor/otherlib/pyproject.toml": _TINYLIB["vendor/tinylib/pyproject.toml"].replace("tinylib", "otherlib"),
        "vendor/otherlib/otherlib/__init__.py": "",
    })
    toml = (pond / "pyproject.toml").read_text()
    (pond / "pyproject.toml").write_text(
        toml.replace('["tinylib"]', '["tinylib", "otherlib"]')
        + 'otherlib = { path = "vendor/otherlib" }\n'
    )
    r = catchment_client.post(
        "/api/deploy",
        files={"pond": ("pond.zip", _zip(pond), "application/zip")},
        data={"name": "envp", "version": "1.0.0", "type": "inlet"},
    )
    assert r.status_code == 422
    assert "uv lock" in r.json()["detail"]
    assert python_for(root, "ponds/envp/1.0.0") == python


# ── which Duckstring goes into a Pond's environment ────────────────────────────


class _FakeDist:
    version = "9.9.9"

    def __init__(self, direct):
        self._direct = direct

    def read_text(self, name):
        return self._direct


def _requirement(monkeypatch, direct):
    import json
    from importlib import metadata

    monkeypatch.setattr(metadata, "distribution", lambda _name: _FakeDist(json.dumps(direct) if direct else None))
    monkeypatch.setattr(envs, "_extras", lambda: ["aws"])
    return envs.duckstring_requirement()


def test_requirement_for_a_released_install(monkeypatch):
    assert _requirement(monkeypatch, None) == ["duckstring[aws]==9.9.9"]


def test_requirement_for_an_editable_checkout(monkeypatch, tmp_path):
    direct = {"url": tmp_path.as_uri(), "dir_info": {"editable": True}}
    assert _requirement(monkeypatch, direct) == ["-e", f"{tmp_path}[aws]"]


def test_requirement_for_a_kept_wheel(monkeypatch, tmp_path):
    wheel = tmp_path / "duckstring-9.9.9-py3-none-any.whl"
    wheel.write_bytes(b"")
    assert _requirement(monkeypatch, {"url": wheel.as_uri(), "archive_info": {}}) == \
        [f"duckstring[aws] @ {wheel.as_uri()}"]


def test_requirement_falls_back_when_the_wheel_is_gone(monkeypatch, tmp_path):
    gone = (tmp_path / "deleted.whl").as_uri()
    assert _requirement(monkeypatch, {"url": gone, "archive_info": {}}) == ["duckstring[aws]==9.9.9"]


def test_requirement_for_a_vcs_install(monkeypatch):
    direct = {"url": "https://github.com/x/duckstring", "vcs_info": {"vcs": "git", "commit_id": "abc123"}}
    assert _requirement(monkeypatch, direct) == ["duckstring[aws] @ git+https://github.com/x/duckstring@abc123"]


# ── a Duck switching to its Pond's environment ─────────────────────────────────


def test_duck_python(tmp_path, monkeypatch):
    monkeypatch.delenv(envs.REEXEC_ENV, raising=False)
    assert envs.duck_python(tmp_path, tmp_path) is None  # default environment

    env_python = envs._python_in(tmp_path / "envs" / "abc")
    monkeypatch.setattr(envs, "ensure_env", lambda root, source, **kw: env_python)
    assert envs.duck_python(tmp_path, tmp_path) == env_python  # switch to it

    monkeypatch.setattr(envs.sys, "prefix", str(tmp_path / "envs" / "abc"))
    assert envs.duck_python(tmp_path, tmp_path) is None  # already in it

    monkeypatch.setenv(envs.REEXEC_ENV, "1")
    monkeypatch.setattr(envs, "ensure_env", lambda root, source, **kw: pytest.fail("must not build"))
    assert envs.duck_python(tmp_path, tmp_path) is None  # the re-executed Duck


def test_a_duck_reports_a_failed_build_and_exits(tmp_path, monkeypatch):
    from duckstring.duck.__main__ import _use_pond_env

    monkeypatch.delenv(envs.REEXEC_ENV, raising=False)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'p'\n")  # no uv.lock

    class Client:
        events: list = []

        def post_event(self, payload):
            self.events.append(payload)
            return True

        def close(self):
            pass

    client = Client()
    with pytest.raises(SystemExit) as exit_:
        _use_pond_env(tmp_path, tmp_path, client)
    assert exit_.value.code == 1
    failed = [e for e in client.events if e["kind"] == "pond_failed"]
    assert failed and failed[0]["error"].startswith("Building the Pond's environment failed")
    assert "uv lock" in failed[0]["traceback"]
