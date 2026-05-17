from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from duckstring.cli import app

runner = CliRunner()


def _write_local_pond_catalog(tmp_path: Path) -> None:
    pond_dir = tmp_path / "ponds" / "aggregated" / "1.0.0"
    pond_dir.mkdir(parents=True, exist_ok=True)
    (pond_dir / "pond.py").write_text(
        """
from duckstring import Pond

def pond():
    p = Pond(name="aggregated", description=None, version="1.0.0")
    p.sink({"out": object()})
    p.flow([None])
    return p
""".lstrip(),
        encoding="utf-8",
    )


def _write_catchment_json(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "default_species": "local",
                "modes": {
                    "pulse": {
                        "type": "pulse",
                    }
                },
                "pond_sources": [
                    {
                        "entrypoint": "pond.py",
                        "root": "./ponds",
                        "structure": "catalog",
                        "type": "local",
                    }
                ],
                "pond_species": {},
                "root_dir": ".duckstring",
                "spec_version": 1,
                "species": {
                    "local": {
                        "engine": "duckdb",
                        "kind": "local",
                        "options": {},
                    }
                },
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _write_basin_json(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "catchment": {
                    "path": "catchment.json",
                },
                "ducks": {
                    "default": None,
                    "instances": {},
                    "ponds": {},
                },
                "hydrated": {},
                "mode": "pulse",
                "name": "example",
                "outlets": {
                    "aggregated": "1.0.0",
                },
                "spec_version": 1,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def test_catchment_ponds_pull_populates_root_catalog(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_local_pond_catalog(tmp_path)
    _write_catchment_json(tmp_path / "catchment.json")

    result = runner.invoke(app, ["catchment", "ponds", "pull", "-f", "catchment.json"])

    assert result.exit_code == 0, result.output
    assert "Pulled 1 pond version(s) across 1 pond(s)" in result.output
    assert (tmp_path / ".duckstring" / "ponds" / "aggregated" / "1.0.0" / "pond.py").exists()


def test_catchment_ponds_list_sources_shows_pond_sources(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_catchment_json(tmp_path / "catchment.json")

    result = runner.invoke(app, ["catchment", "ponds", "list-sources", "-f", "catchment.json"])

    assert result.exit_code == 0, result.output
    assert "local_catalog root=./ponds" in result.output


def test_catchment_ponds_list_pulled_uses_root_dir_ponds_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_local_pond_catalog(tmp_path)
    _write_catchment_json(tmp_path / "catchment.json")

    pull_result = runner.invoke(app, ["catchment", "ponds", "pull", "-f", "catchment.json"])
    assert pull_result.exit_code == 0, pull_result.output

    result = runner.invoke(app, ["catchment", "ponds", "list-pulled", "-f", "catchment.json"])

    assert result.exit_code == 0, result.output
    expected_root = (tmp_path / ".duckstring").resolve().as_posix()
    assert f"Catchment Root: {expected_root}/" in result.output
    assert "aggregated@1.0.0" in result.output


def test_basin_hydrate_pulls_sources_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_local_pond_catalog(tmp_path)
    _write_catchment_json(tmp_path / "catchment.json")
    _write_basin_json(tmp_path / "basins" / "example" / "basin.json")

    result = runner.invoke(app, ["basin", "hydrate", "example"])

    assert result.exit_code == 0, result.output
    assert "Wrote hydrated" in result.output
    assert (tmp_path / ".duckstring" / "ponds" / "aggregated" / "1.0.0" / "pond.py").exists()

    basin_data = json.loads((tmp_path / "basins" / "example" / "basin.json").read_text(encoding="utf-8"))
    assert basin_data["hydrated"]["ponds"]["aggregated"]["version"] == "1.0.0"


def test_basin_hydrate_no_pull_keeps_old_failure_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_local_pond_catalog(tmp_path)
    _write_catchment_json(tmp_path / "catchment.json")
    _write_basin_json(tmp_path / "basins" / "example" / "basin.json")

    result = runner.invoke(app, ["basin", "hydrate", "example", "--no-pull"])

    assert result.exit_code != 0
    assert isinstance(result.exception, KeyError)
    assert "Outlet pond 'aggregated' is not present in catchment pond catalog." in str(result.exception)


def test_catchment_ponds_add_force_non_interactive(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_local_pond_catalog(tmp_path)
    _write_catchment_json(tmp_path / "catchment.json")

    result = runner.invoke(
        app,
        [
            "catchment",
            "ponds",
            "add",
            "--source-type",
            "local",
            "--scope",
            "catalog",
            "--root",
            "./ponds",
            "--force",
            "-f",
            "catchment.json",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "catchment.json").read_text(encoding="utf-8"))
    assert len(data["pond_sources"]) == 2
    assert data["pond_sources"][-1]["type"] == "local"
    assert data["pond_sources"][-1]["structure"] == "catalog"
    assert data["pond_sources"][-1]["root"] == "./ponds"


def test_catchment_ponds_add_git_monorepo_accepts_root_and_ssh_repo_url(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_catchment_json(tmp_path / "catchment.json")

    result = runner.invoke(
        app,
        [
            "catchment",
            "ponds",
            "add",
            "--source-type",
            "git",
            "--scope",
            "catalog",
            "--repo-structure",
            "monorepo",
            "--repo",
            "git@github.com:acme/duckstring-ponds.git",
            "--ref-type",
            "branch",
            "--ref-pattern",
            "main",
            "--root",
            "catalog",
            "--force",
            "-f",
            "catchment.json",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads((tmp_path / "catchment.json").read_text(encoding="utf-8"))
    git_sources = [s for s in data["pond_sources"] if s.get("type") == "git" and s.get("structure") == "catalog"]
    assert git_sources
    source = git_sources[-1]
    assert source["repo"] == "git@github.com:acme/duckstring-ponds.git"
    assert source["repo_structure"] == "monorepo"
    assert source["root"] == "catalog"


def test_catchment_inlets_cli_lifecycle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_catchment_json(tmp_path / "catchment.json")

    add_result = runner.invoke(
        app,
        [
            "catchment",
            "inlets",
            "add",
            "landing_orders",
            "--path",
            "./landing/orders",
            "--glob",
            "*.parquet",
            "-f",
            "catchment.json",
        ],
    )
    assert add_result.exit_code == 0, add_result.output

    list_result = runner.invoke(app, ["catchment", "inlets", "list", "-f", "catchment.json"])
    assert list_result.exit_code == 0, list_result.output
    assert "landing_orders" in list_result.output
    assert "path=./landing/orders" in list_result.output

    show_result = runner.invoke(app, ["catchment", "inlets", "show", "landing_orders", "-f", "catchment.json"])
    assert show_result.exit_code == 0, show_result.output
    assert "\"format\": \"parquet\"" in show_result.output
    assert "\"glob\": \"*.parquet\"" in show_result.output

    remove_result = runner.invoke(app, ["catchment", "inlets", "remove", "landing_orders", "-f", "catchment.json"])
    assert remove_result.exit_code == 0, remove_result.output

    post_list = runner.invoke(app, ["catchment", "inlets", "list", "-f", "catchment.json"])
    assert post_list.exit_code == 0, post_list.output
    assert "No inlet locations configured." in post_list.output
