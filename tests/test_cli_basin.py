from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from duckstring import Catchment
from duckstring import utils as ds_utils
from duckstring.cli import app

runner = CliRunner()


def _write_default_catchment(path: Path) -> None:
    catchment = Catchment(root_dir=".duckstring")
    path.write_text(json.dumps(catchment.to_dict(), indent=2, sort_keys=True), encoding="utf-8")


def test_basin_hydrate_uses_command_first_order(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["basin", "hydrate", "missing"])

    assert result.exit_code == 2
    assert "Unknown basin 'missing'." in result.output
    assert "Unknown basin 'hydrate'." not in result.output


def test_basin_create_direct_and_show(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_default_catchment(tmp_path / "catchment.json")

    result = runner.invoke(
        app,
        [
            "basin",
            "create",
            "demo",
            "--outlet",
            "orders=1.2.3",
            "--outlet",
            "customers=2.0.0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Wrote" in result.output

    spec_path = tmp_path / "basins" / "demo" / "basin.json"
    assert spec_path.exists()

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    assert spec["name"] == "demo"
    assert spec["mode"] == "pulse"
    assert spec["catchment"] == {"path": "catchment.json"}
    assert spec["outlets"] == {"orders": "1.2.3", "customers": "2.0.0"}

    show_result = runner.invoke(app, ["basin", "show", "demo"])
    assert show_result.exit_code == 0, show_result.output
    assert "Basin: demo" in show_result.output


def test_basin_create_interactive(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_default_catchment(tmp_path / "catchment.json")

    user_input = "\n".join(
        [
            "interactive_demo",  # basin name
            "",  # catchment path (default)
            "",  # basin mode (default pulse)
            "",  # add outlets now (default yes)
            "orders",  # outlet pond
            "1.2.3",  # outlet version
            "",  # add another outlet (default no)
            "",  # confirm (default yes)
        ]
    )

    result = runner.invoke(app, ["basin", "create", "-i"], input=f"{user_input}\n")

    assert result.exit_code == 0, result.output
    assert "Wrote" in result.output

    spec_path = tmp_path / "basins" / "interactive_demo" / "basin.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    assert spec["name"] == "interactive_demo"
    assert spec["outlets"] == {"orders": "1.2.3"}


@pytest.mark.skipif(not (ds_utils._HAVE_IBIS and ds_utils._HAVE_DUCKDB), reason="ibis/duckdb not installed")
def test_basin_run_auto_hydrates_and_pulses(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    pond_dir = tmp_path / "ponds" / "aggregated" / "1.0.0"
    pond_dir.mkdir(parents=True, exist_ok=True)
    (pond_dir / "pond.py").write_text(
        """
import ibis
from duckstring import Pond

def pond():
    p = Pond(name="aggregated", description=None, version="1.0.0")
    t = ibis.memtable([{"x": 1}], schema=ibis.schema({"x": "int64"}))
    p.sink({"out": t})
    p.flow([None])
    return p
""".lstrip(),
        encoding="utf-8",
    )

    (tmp_path / "catchment.json").write_text(
        json.dumps(
            {
                "default_species": "local",
                "modes": {"pulse": {"type": "pulse"}},
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

    basin_path = tmp_path / "basins" / "example" / "basin.json"
    basin_path.parent.mkdir(parents=True, exist_ok=True)
    basin_path.write_text(
        json.dumps(
            {
                "catchment": {"path": "catchment.json"},
                "ducks": {"default": None, "instances": {}, "ponds": {}},
                "hydrated": {},
                "mode": "pulse",
                "name": "example",
                "outlets": {"aggregated": "1.0.0"},
                "spec_version": 1,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["basin", "run", "example"])
    assert result.exit_code == 0, result.output
    assert "Completed pulse" in result.output
    assert (tmp_path / ".duckstring" / "data" / "aggregated" / "1.0.0" / "out.parquet").exists()
