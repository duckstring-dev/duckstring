"""The CLI's layout after the 2026-10 regrouping: thirteen top-level commands, tables and Objects named as
POND.NAME under `data`, catalog SQL through the top-level `query`, multi-Pond `control` verbs, `pond promote`
and `pond puddle`, and `catchment duct open|close`."""

from __future__ import annotations

from test_data import _seed
from test_execute import _deploy_outlet

from duckstring.cli import app

TOP_LEVEL = {"status", "query", "get", "refresh", "window", "control", "data", "pond", "catchment", "duck", "spout",
             "secret", "alert"}


def test_the_top_level_is_thirteen_commands():
    import typer.main

    group = typer.main.get_command(app)
    assert set(group.commands) == TOP_LEVEL


def test_the_old_commands_are_gone(runner):
    for args in (["do", "--all", "--kill"], ["serve", "status"], ["objects", "outlet"], ["get-object", "outlet", "x"],
                 ["delete-table", "outlet", "x"], ["delete-object", "outlet", "x"], ["lineage"], ["trace", "a.b"],
                 ["puddle", "ls"], ["tap", "ls", "outlet"], ["catchment", "open", "outlet"]):
        assert runner.invoke(app, args).exit_code != 0, args


def test_query_runs_catalog_sql_and_writes_files(runner, tmp_path, monkeypatch, live_catchment):
    result = runner.invoke(app, ["query", "SELECT 42 AS answer"])
    assert result.exit_code == 0, result.output
    assert "42" in result.output
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["query", "SELECT 42 AS answer", "--csv", "answer.csv"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "answer.csv").read_text().splitlines() == ["answer", "42"]


def test_data_expose_ls_and_delete(runner, catchment_root, live_catchment):
    _deploy_outlet(live_catchment)
    _seed(catchment_root, "outlet", "daily")

    result = runner.invoke(app, ["data", "expose", "outlet.daily", "--on"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["data", "ls", "outlet"])
    assert result.exit_code == 0, result.output
    assert "daily" in result.output and "yes" in result.output

    assert runner.invoke(app, ["data", "delete", "outlet.nothing", "-y"]).exit_code != 0  # neither kind
    assert runner.invoke(app, ["data", "delete", "outlet", "-y"]).exit_code != 0  # needs POND.NAME
    result = runner.invoke(app, ["data", "delete", "outlet.daily", "-y"])
    assert result.exit_code == 0, result.output
    assert not (catchment_root / "ponds" / "outlet" / "m1" / "data" / "daily.parquet").exists()


def test_control_verbs_take_several_ponds(runner, live_catchment):
    _deploy_outlet(live_catchment)
    result = runner.invoke(app, ["control", "kill", "--all"])
    assert result.exit_code == 0, result.output
    assert "outlet@1" in result.output
    result = runner.invoke(app, ["control", "clear", "outlet@1"])
    assert result.exit_code == 0, result.output
    assert runner.invoke(app, ["control", "kill", "--all", "-m", "1"]).exit_code != 0  # -m is for one Pond
    assert runner.invoke(app, ["control", "kill"]).exit_code != 0  # nothing selected
    result = runner.invoke(app, ["control", "wipe", "outlet", "--confirm", "nope"])
    assert result.exit_code != 0  # the Catchment's name is required


def test_pond_promote_and_duct_open(runner, live_catchment):
    _deploy_outlet(live_catchment)
    result = runner.invoke(app, ["pond", "promote", "outlet", "--major", "1"])
    assert result.exit_code == 0, result.output
    assert "v1" in result.output
    result = runner.invoke(app, ["catchment", "duct", "open", "outlet"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["catchment", "duct", "close", "outlet"])
    assert result.exit_code == 0, result.output
