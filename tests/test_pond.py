from __future__ import annotations

import sys
from pathlib import Path

import pytest

from duckstring.cli import app

EXPECTED_FILES = {"pond.toml", "src/pond.py", "__main__.py", ".gitignore", "README.md"}

_DEMO_DIR = Path(__file__).parent.parent / "src" / "duckstring" / "demo"


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


_DEMO_PONDS = ("transactions", "products", "sales", "reports")


def test_demo_creates_all_subdirs(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["pond", "demo"], input="y\n")
    assert result.exit_code == 0
    for name in _DEMO_PONDS:
        assert EXPECTED_FILES.issubset(_file_names(tmp_path / name)), f"Missing files in {name}/"


def test_demo_copies_gitignore(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["pond", "demo"], input="y\n")
    for name in _DEMO_PONDS:
        assert (tmp_path / name / ".gitignore").exists(), f".gitignore missing in {name}/"


def test_demo_aborts_on_no(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["pond", "demo"], input="n\n")
    assert result.exit_code != 0
    assert not (tmp_path / "transactions").exists()


def test_demo_fails_if_subdir_exists(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "transactions").mkdir()
    result = runner.invoke(app, ["pond", "demo"], input="y\n")
    assert result.exit_code != 0


def test_demo_inlet_toml(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["pond", "demo"], input="y\n")
    content = (tmp_path / "transactions" / "pond.toml").read_text()
    assert 'name = "transactions"' in content
    assert 'type = "inlet"' in content
    assert "sources" not in content


def test_demo_pond_toml(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["pond", "demo"], input="y\n")
    content = (tmp_path / "sales" / "pond.toml").read_text()
    assert 'name = "sales"' in content
    assert "[sources]" in content
    assert "transactions" in content
    assert "products" in content


def test_demo_outlet_toml(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["pond", "demo"], input="y\n")
    content = (tmp_path / "reports" / "pond.toml").read_text()
    assert 'name = "reports"' in content
    assert 'type = "outlet"' in content
    assert "[sources]" in content
    assert "sales" in content


def test_demo_pond_py_imports_ripple(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["pond", "demo"], input="y\n")
    for name in _DEMO_PONDS:
        content = (tmp_path / name / "src" / "pond.py").read_text()
        assert "from duckstring import ripple" in content, f"Missing import in {name}/src/pond.py"


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib stdlib only in 3.11+")
def test_demo_pond_toml_valid_toml(runner, tmp_path, monkeypatch):
    import tomllib

    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["pond", "demo"], input="y\n")
    parsed = tomllib.loads((tmp_path / "reports" / "pond.toml").read_text())
    assert parsed["pond"]["name"] == "reports"
    assert parsed["pond"]["type"] == "outlet"
    assert parsed["sources"]["sales"] == "1.0.0"


# ── demo source files (direct, no CLI) ───────────────────────────────────────
# These tests verify the source files in src/duckstring/demo/ directly,
# so deployment tests can reference them without going through the CLI.


@pytest.mark.parametrize("name", ["transactions", "products", "sales", "reports"])
def test_demo_source_files_complete(name):
    pond_dir = _DEMO_DIR / name
    for rel in ("pond.toml", "src/pond.py", "__main__.py", ".gitignore", "README.md"):
        assert (pond_dir / rel).exists(), f"{name}/{rel} missing from demo source"


@pytest.mark.parametrize("name", ["transactions", "products", "sales", "reports"])
def test_demo_source_pond_py_imports_ripple(name):
    content = (_DEMO_DIR / name / "src" / "pond.py").read_text()
    assert "from duckstring import ripple" in content
    assert "@ripple" in content
