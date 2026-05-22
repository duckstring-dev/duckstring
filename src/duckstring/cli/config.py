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


def get_default_catchment() -> str | None:
    return load_config().get("default_catchment")


def set_default_catchment(name: str) -> None:
    config = load_config()
    config["default_catchment"] = name
    save_config(config)


def resolve_catchment(name: str | None) -> dict[str, Any]:
    import typer

    config = load_config()
    catchments = config.get("catchments", {})
    effective = name or config.get("default_catchment")

    # Auto-select when there is exactly one registered catchment.
    if not effective and len(catchments) == 1:
        effective = next(iter(catchments))

    if not effective:
        typer.echo("Error: no catchment specified and no default set.", err=True)
        typer.echo("  Pass one explicitly: duckstring deploy -c <name>", err=True)
        typer.echo("  Or set a default:    duckstring catchment set-default <name>", err=True)
        raise typer.Exit(1)
    if effective not in catchments:
        typer.echo(f"Error: no catchment '{effective}' registered.", err=True)
        typer.echo(f"  duckstring catchment start --name {effective}", err=True)
        typer.echo(f"  duckstring catchment connect --name {effective} --path <url>", err=True)
        raise typer.Exit(1)
    return catchments[effective]


def list_catchments() -> list[tuple[str, dict[str, Any]]]:
    return list(load_config().get("catchments", {}).items())
