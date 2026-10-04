""".pondignore: which files of a Pond project a deploy uploads (and a --git deploy keeps)."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

from typer.testing import CliRunner

from duckstring.cli import app as cli_app
from duckstring.cli.deploy import _zip_pond
from duckstring.pondignore import DEFAULT_PATTERNS, IGNORE_FILE, deployed_files, prune


def _tree(root: Path, files: list[str]) -> None:
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")


_PROJECT = [
    "pond.toml", "README.md", ".gitignore", "src/pond.py", "src/puddles.py", "src/sql/report.sql",
    "puddles/ponds/sales/data/sale_line.parquet", "puddles/out/registry.duckdb",
    ".env", ".env.local", ".venv/lib/site.py", ".git/HEAD", "src/__pycache__/pond.cpython-313.pyc",
    "dist/pkg.whl", "notes.pyc",
]


def _names(root: Path) -> set[str]:
    return {p.as_posix() for p in deployed_files(root)}


def test_defaults_exclude_local_data_secrets_and_caches(tmp_path):
    _tree(tmp_path, _PROJECT)
    assert _names(tmp_path) == {
        "pond.toml", "README.md", ".gitignore", "src/pond.py", "src/puddles.py", "src/sql/report.sql",
    }


def test_a_pondignore_replaces_the_defaults(tmp_path):
    _tree(tmp_path, _PROJECT + ["data/big.csv", "data/keep.csv"])
    (tmp_path / IGNORE_FILE).write_text("data/*\n!data/keep.csv\n.git/\n")
    names = _names(tmp_path)
    assert "data/keep.csv" in names and "data/big.csv" not in names  # negation works
    assert "puddles/out/registry.duckdb" in names  # no longer excluded: the file replaced the defaults
    assert ".git/HEAD" not in names


def test_zip_contains_exactly_the_deployed_files(tmp_path):
    _tree(tmp_path, _PROJECT)
    with zipfile.ZipFile(io.BytesIO(_zip_pond(tmp_path))) as zf:
        assert set(zf.namelist()) == _names(tmp_path)


def test_prune_removes_excluded_files_and_git_metadata(tmp_path):
    _tree(tmp_path, _PROJECT)
    prune(tmp_path)
    remaining = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()}
    assert remaining == _names(tmp_path)
    assert not (tmp_path / ".git").exists() and not (tmp_path / "puddles").exists()


def test_pond_init_writes_the_default_pondignore(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli_app, ["pond", "init", "sales"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / IGNORE_FILE).read_text() == DEFAULT_PATTERNS


def test_deploy_dry_run_lists_files_without_a_catchment(tmp_path, monkeypatch):
    _tree(tmp_path, _PROJECT)
    (tmp_path / "pond.toml").write_text('[pond]\nname = "sales"\nversion = "1.0.0"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no registered Catchment anywhere
    result = CliRunner().invoke(cli_app, ["pond", "deploy", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "src/pond.py" in result.output
    listed = [line.strip().split()[0] for line in result.output.splitlines() if line.startswith("  ")]
    assert not any(name.startswith((".env", "puddles/", ".git/")) for name in listed)
    assert "6 files" in result.output
