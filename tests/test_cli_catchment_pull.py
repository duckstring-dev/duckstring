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
