from __future__ import annotations

import io
import zipfile

import httpx

from duckstring.cli import app
from duckstring.cli.deploy import _zip_pond


def _make_pond(path, name="test_pond", version="1.0.0", kind="pond"):
    """Create a minimal pond project at path."""
    toml = f'[pond]\nname = "{name}"\nversion = "{version}"\n'
    if kind != "pond":
        toml += f'type = "{kind}"\n'
    (path / "pond.toml").write_text(toml)
    (path / "src").mkdir(exist_ok=True)
    (path / "src" / "pond.py").write_text("from duckstring import ripple\n")


# ── _zip_pond unit tests ──────────────────────────────────────────────────────


def test_zip_includes_source_files(tmp_path):
    _make_pond(tmp_path)
    archive = _zip_pond(tmp_path)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        names = zf.namelist()
    assert "pond.toml" in names
    assert "src/pond.py" in names


def test_zip_excludes_git(tmp_path):
    _make_pond(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main")
    archive = _zip_pond(tmp_path)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        names = zf.namelist()
    assert not any(".git" in n for n in names)


def test_zip_excludes_pycache(tmp_path):
    _make_pond(tmp_path)
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "pond.cpython-311.pyc").write_bytes(b"\x00")
    archive = _zip_pond(tmp_path)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        names = zf.namelist()
    assert not any("__pycache__" in n for n in names)
    assert not any(".pyc" in n for n in names)


def test_zip_excludes_venv(tmp_path):
    _make_pond(tmp_path)
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("#!/usr/bin/env python")
    archive = _zip_pond(tmp_path)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        names = zf.namelist()
    assert not any(".venv" in n for n in names)


def test_zip_includes_gitignore(tmp_path):
    _make_pond(tmp_path)
    (tmp_path / ".gitignore").write_text("__pycache__/\n")
    archive = _zip_pond(tmp_path)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        names = zf.namelist()
    assert ".gitignore" in names


# ── CLI integration ───────────────────────────────────────────────────────────


def test_deploy_fails_without_pond_toml(runner, tmp_path, monkeypatch, dev_catchment):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["deploy", "-c", "dev"])
    assert result.exit_code != 0
    assert "pond.toml" in result.output


def test_deploy_fails_unknown_catchment(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_pond(tmp_path)
    result = runner.invoke(app, ["deploy", "-c", "nonexistent"])
    assert result.exit_code != 0


def test_deploy_no_catchment_no_default_exits(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_pond(tmp_path)
    result = runner.invoke(app, ["deploy", "--yes"])
    assert result.exit_code != 0


def test_deploy_local_registers_pond(runner, tmp_path, monkeypatch, live_catchment):
    monkeypatch.chdir(tmp_path)
    _make_pond(tmp_path, name="my_pond", version="1.2.3")
    result = runner.invoke(app, ["deploy", "--yes"])
    assert result.exit_code == 0, result.output
    r = httpx.get(f"{live_catchment}/api/ponds/my_pond/versions/1.2.3")
    assert r.status_code == 200
    assert r.json()["version"] == "1.2.3"


def test_deploy_explicit_catchment_overrides_default(runner, tmp_path, monkeypatch, live_catchment):
    monkeypatch.chdir(tmp_path)
    _make_pond(tmp_path, name="my_pond", version="2.0.0")
    result = runner.invoke(app, ["deploy", "-c", "dev", "--yes"])
    assert result.exit_code == 0, result.output
    r = httpx.get(f"{live_catchment}/api/ponds/my_pond/versions/2.0.0")
    assert r.status_code == 200


def test_deploy_aborts_on_no(runner, tmp_path, monkeypatch, live_catchment):
    monkeypatch.chdir(tmp_path)
    _make_pond(tmp_path, name="my_pond", version="1.0.0")
    result = runner.invoke(app, ["deploy"], input="n\n")
    assert result.exit_code != 0
    r = httpx.get(f"{live_catchment}/api/ponds/my_pond/versions/1.0.0")
    assert r.status_code == 404


def test_deploy_shows_overwrite_warning(runner, tmp_path, monkeypatch, live_catchment):
    monkeypatch.chdir(tmp_path)
    _make_pond(tmp_path, name="my_pond", version="1.0.0")
    runner.invoke(app, ["deploy", "--yes"])
    result = runner.invoke(app, ["deploy"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "overwritten" in result.output


def test_deploy_yes_flag_skips_prompt(runner, tmp_path, monkeypatch, live_catchment):
    monkeypatch.chdir(tmp_path)
    _make_pond(tmp_path, name="my_pond", version="3.0.0")
    result = runner.invoke(app, ["deploy", "-y"])
    assert result.exit_code == 0, result.output


def test_deploy_git_fails_without_remote(runner, tmp_path, monkeypatch, live_catchment):
    monkeypatch.chdir(tmp_path)
    _make_pond(tmp_path)
    result = runner.invoke(app, ["deploy", "--yes", "--git", "main"])
    assert result.exit_code != 0
