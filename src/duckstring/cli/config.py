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
    CONFIG_FILE.chmod(0o600)  # may hold API keys / auth headers


def auth_headers(cfg: dict[str, Any]) -> dict[str, str]:
    """The auth headers to attach to every request to a registered Catchment: its custom ``headers``
    table (``catchment connect --header``), with ``Authorization: Bearer {key}`` filled in from
    ``key`` when no explicit Authorization header is set."""
    headers = {str(k): str(v) for k, v in cfg.get("headers", {}).items()}
    key = cfg.get("key")
    if key and not any(h.lower() == "authorization" for h in headers):
        headers["Authorization"] = f"Bearer {key}"
    return headers


class CatchmentConflict(Exception):
    """Raised when a URL or root path is already registered under a different name."""

    def __init__(self, value: str, existing_name: str) -> None:
        self.value = value
        self.existing_name = existing_name
        super().__init__(f"'{value}' is already registered as catchment '{existing_name}'")


def register_catchment(
    name: str, url: str, kind: str = "local", root: str | None = None, key: str | None = None,
    headers: dict[str, str] | None = None, data_root: str | None = None,
    state_backup: str | None = None, checkpoint_every: str | None = None,
) -> None:
    config = load_config()
    catchments = config.setdefault("catchments", {})

    for existing_name, cfg in catchments.items():
        if existing_name == name:
            continue
        # For local catchments the port can be shared (servers don't always run simultaneously).
        # Only URL conflicts between remote catchments are meaningful.
        if kind == "remote" and cfg.get("type") == "remote" and cfg.get("url") == url:
            raise CatchmentConflict(url, existing_name)
        if root and cfg.get("root") == root:
            raise CatchmentConflict(root, existing_name)

    # ``data_root`` / ``state_backup`` are environment-specific operational config (like windows/spouts):
    # where the data plane lands and where hot state is backed up — never in pond.toml, stored here as
    # written (``${env:NAME}`` credential refs are kept verbatim and resolved only at runtime).
    catchments[name] = {
        "url": url,
        "type": kind,
        **({"root": root} if root else {}),
        **({"key": key} if key else {}),
        **({"headers": headers} if headers else {}),
        **({"data_root": data_root} if data_root else {}),
        **({"state_backup": state_backup} if state_backup else {}),
        **({"checkpoint_every": checkpoint_every} if checkpoint_every else {}),
    }
    save_config(config)


def update_catchment_key(name: str, key: str) -> None:
    """Replace the stored API key for a registered catchment (e.g. after rotating the full key, so the
    operator's own CLI keeps working)."""
    config = load_config()
    cfg = config.get("catchments", {}).get(name)
    if cfg is None:
        return
    cfg["key"] = key
    save_config(config)


def unregister_catchment(name: str) -> None:
    config = load_config()
    config.get("catchments", {}).pop(name, None)
    if config.get("default_catchment") == name:
        del config["default_catchment"]
    save_config(config)


def get_default_catchment() -> str | None:
    return load_config().get("default_catchment")


def set_default_catchment(name: str) -> None:
    config = load_config()
    config["default_catchment"] = name
    save_config(config)


def resolve_catchment(name: str | None) -> tuple[str, dict[str, Any]]:
    import typer

    config = load_config()
    catchments = config.get("catchments", {})
    effective = name or config.get("default_catchment")

    # Auto-select when there is exactly one registered catchment.
    if not effective and len(catchments) == 1:
        effective = next(iter(catchments))

    if not effective:
        typer.echo("Error: no catchment specified and no default set.", err=True)
        typer.echo("  Pass one explicitly: duckstring pond deploy -c <name>", err=True)
        typer.echo("  Or set a default:    duckstring catchment set-default <name>", err=True)
        raise typer.Exit(1)
    if effective not in catchments:
        typer.echo(f"Error: no catchment '{effective}' registered.", err=True)
        typer.echo(f"  duckstring catchment init --name {effective}", err=True)
        typer.echo(f"  duckstring catchment connect --name {effective} --path <url>", err=True)
        raise typer.Exit(1)
    return effective, catchments[effective]


def list_catchments() -> list[tuple[str, dict[str, Any]]]:
    return list(load_config().get("catchments", {}).items())
