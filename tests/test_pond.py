from __future__ import annotations

import sys

import pytest

from duckstring.cli import app

EXPECTED_FILES = {"pond.toml", "src/pond.py", "__main__.py", ".gitignore", "README.md"}


def _file_names(path):
    return {str(f.relative_to(path)) for f in path.rglob("*") if f.is_file()}


# ── pond init ────────────────────────────────────────────────────────────────


def test_init_creates_all_files(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["pond", "init", "my_pond"])
    assert result.exit_code == 0
    assert EXPECTED_FILES.issubset(_file_names(tmp_path))


def test_init_pond_toml_content(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["pond", "init", "my_pond"])
    content = (tmp_path / "pond.toml").read_text()
    assert 'name = "my_pond"' in content
    assert 'version = "0.1.0"' in content


def test_init_pond_py_imports_ripple(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["pond", "init", "my_pond"])
    content = (tmp_path / "src" / "pond.py").read_text()
    assert "from duckstring import ripple" in content
    assert "@ripple" in content


def test_init_fails_if_pond_toml_exists(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pond.toml").write_text("[pond]\n")
    result = runner.invoke(app, ["pond", "init", "my_pond"])
    assert result.exit_code != 0


# ── pond demo ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("demo_type", ["inlet", "pond", "outlet"])
def test_demo_creates_all_files(runner, tmp_path, monkeypatch, demo_type):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["pond", "demo", demo_type])
    assert result.exit_code == 0
    assert EXPECTED_FILES.issubset(_file_names(tmp_path))


def test_demo_inlet_toml(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["pond", "demo", "inlet"])
    content = (tmp_path / "pond.toml").read_text()
    assert 'name = "inlet"' in content
    assert 'type = "inlet"' in content
    assert "sources" not in content


def test_demo_pond_toml(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["pond", "demo", "pond"])
    content = (tmp_path / "pond.toml").read_text()
    assert 'name = "pond"' in content
    assert "[sources]" in content
    assert "inlet" in content


def test_demo_outlet_toml(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["pond", "demo", "outlet"])
    content = (tmp_path / "pond.toml").read_text()
    assert 'name = "outlet"' in content
    assert 'type = "outlet"' in content
    assert "[sources]" in content
    assert "pond" in content


def test_demo_fails_if_pond_toml_exists(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pond.toml").write_text("[pond]\n")
    result = runner.invoke(app, ["pond", "demo", "inlet"])
    assert result.exit_code != 0


def test_demo_invalid_type(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["pond", "demo", "bogus"])
    assert result.exit_code != 0


def test_demo_pond_py_imports_ripple(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["pond", "demo", "inlet"])
    content = (tmp_path / "src" / "pond.py").read_text()
    assert "from duckstring import ripple" in content


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib stdlib only in 3.11+")
def test_demo_pond_toml_valid_toml(runner, tmp_path, monkeypatch):
    import tomllib

    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["pond", "demo", "outlet"])
    parsed = tomllib.loads((tmp_path / "pond.toml").read_text())
    assert parsed["pond"]["name"] == "outlet"
    assert parsed["pond"]["type"] == "outlet"
    assert parsed["sources"]["pond"] == "1.0.0"
