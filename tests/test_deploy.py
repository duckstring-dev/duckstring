from __future__ import annotations

import io
import zipfile
from unittest.mock import MagicMock, patch

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


def test_deploy_fails_without_pond_toml(runner, tmp_path, monkeypatch, dev_catchment, mock_post):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["deploy", "dev"])
    assert result.exit_code != 0
    assert "pond.toml" in result.output


def test_deploy_fails_unknown_catchment(runner, tmp_path, monkeypatch, mock_post):
    monkeypatch.chdir(tmp_path)
    _make_pond(tmp_path)
    result = runner.invoke(app, ["deploy", "nonexistent"])
    assert result.exit_code != 0
    assert mock_post.call_count == 0


def test_deploy_local_calls_api(runner, tmp_path, monkeypatch, dev_catchment, mock_post):
    monkeypatch.chdir(tmp_path)
    _make_pond(tmp_path, name="my_pond", version="1.2.3")
    result = runner.invoke(app, ["deploy", "dev"])
    assert result.exit_code == 0
    mock_post.assert_called_once()
    call_url = mock_post.call_args.args[0]
    assert "/api/deploy" in call_url
    files = mock_post.call_args.kwargs["files"]
    assert "pond" in files


def test_deploy_local_sends_pond_metadata(runner, tmp_path, monkeypatch, dev_catchment, mock_post):
    monkeypatch.chdir(tmp_path)
    _make_pond(tmp_path, name="my_pond", version="2.0.0")
    runner.invoke(app, ["deploy", "dev"])
    data = mock_post.call_args.kwargs["data"]
    assert data["name"] == "my_pond"
    assert data["version"] == "2.0.0"


def test_deploy_git_fails_without_remote(runner, tmp_path, monkeypatch, dev_catchment, mock_post):
    monkeypatch.chdir(tmp_path)
    _make_pond(tmp_path)
    # No git repo → subprocess will fail
    result = runner.invoke(app, ["deploy", "dev", "--git", "main"])
    assert result.exit_code != 0
    assert mock_post.call_count == 0


def test_deploy_local_passes_timeout_to_httpx(runner, tmp_path, monkeypatch, dev_catchment):
    """Caller-supplied timeout must not collide with the default timeout in _http.request."""
    monkeypatch.chdir(tmp_path)
    _make_pond(tmp_path, name="my_pond", version="1.0.0")

    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(kwargs)
        mock = MagicMock()
        mock.raise_for_status = lambda: None
        return mock

    with patch("httpx.request", side_effect=fake_request):
        result = runner.invoke(app, ["deploy", "dev"])

    assert result.exit_code == 0, result.output
    import httpx
    assert isinstance(captured.get("timeout"), httpx.Timeout)
    assert captured["timeout"].read == 120.0
