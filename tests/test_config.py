from __future__ import annotations

import pytest

from duckstring.cli.config import (
    list_catchments,
    load_config,
    register_catchment,
    resolve_catchment,
    save_config,
)


def test_register_and_resolve():
    register_catchment("dev", url="http://localhost:7474", kind="local")
    _, cfg = resolve_catchment("dev")
    assert cfg["url"] == "http://localhost:7474"
    assert cfg["type"] == "local"


def test_register_with_root(tmp_path):
    register_catchment("dev", url="http://localhost:7474", kind="local", root=str(tmp_path))
    _, cfg = resolve_catchment("dev")
    assert cfg["root"] == str(tmp_path)


def test_register_remote():
    register_catchment("prod", url="https://example.com", kind="remote")
    _, cfg = resolve_catchment("prod")
    assert cfg["type"] == "remote"
    assert "root" not in cfg


def test_resolve_unknown_exits():
    # typer.Exit, not click's: newer typer vendors click (typer._click), so the separately
    # installed click's Exit can be a different class than the one typer.Exit subclasses.
    import typer

    with pytest.raises(typer.Exit):
        resolve_catchment("nonexistent")


def test_list_empty():
    assert list_catchments() == []


def test_list_multiple():
    register_catchment("dev", url="http://localhost:7474", kind="local")
    register_catchment("prod", url="https://example.com", kind="remote")
    items = list_catchments()
    names = [n for n, _ in items]
    assert "dev" in names
    assert "prod" in names


def test_register_overwrites():
    register_catchment("dev", url="http://localhost:7474", kind="local")
    register_catchment("dev", url="http://localhost:9000", kind="local")
    _, cfg = resolve_catchment("dev")
    assert cfg["url"] == "http://localhost:9000"


def test_save_and_reload(tmp_path):
    from duckstring.cli.config import CONFIG_FILE

    config = {"catchments": {"dev": {"url": "http://localhost:7474", "type": "local"}}}
    save_config(config)
    assert CONFIG_FILE.exists()
    reloaded = load_config()
    assert reloaded["catchments"]["dev"]["url"] == "http://localhost:7474"
