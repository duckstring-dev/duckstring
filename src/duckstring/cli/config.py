from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".duckstring"
CONFIG_FILE = CONFIG_DIR / "config.toml"


def _load_toml(text: str) -> dict[str, Any]:
    if sys.version_info >= (3, 11):
        import tomllib
        return tomllib.loads(text)
    import tomli
    return tomli.loads(text)


def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        return {}
    return _load_toml(CONFIG_FILE.read_text(encoding="utf-8"))


def save_config(config: dict[str, Any]) -> None:
    import tomli_w
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(tomli_w.dumps(config), encoding="utf-8")


def register_catchment(name: str, url: str, kind: str = "local", root: str | None = None) -> None:
    config = load_config()
    config.setdefault("catchments", {})[name] = {
        "url": url,
        "type": kind,
        **({"root": root} if root else {}),
    }
    save_config(config)


def resolve_catchment(name: str) -> dict[str, Any]:
    catchments = load_config().get("catchments", {})
    if name not in catchments:
        import typer
        typer.echo(f"Error: no catchment '{name}' registered.", err=True)
        typer.echo(f"  duckstring catchment start --name {name}", err=True)
        typer.echo(f"  duckstring catchment connect --name {name} --path <url>", err=True)
        raise typer.Exit(1)
    return catchments[name]


def list_catchments() -> list[tuple[str, dict[str, Any]]]:
    return list(load_config().get("catchments", {}).items())
