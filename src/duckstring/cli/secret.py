"""`duckstring secret set|ls|rm` — the write-only Catchment secret store.

Set a credential against a live Catchment and reference it from a Spout destination as ``${secret:NAME}``.
Write-only: ``ls`` shows names, there is no command to read a value back. The value is prompted (hidden) so
it never lands in shell history / argv; it travels in the request, so use an HTTPS Catchment.
"""

from __future__ import annotations

from typing import Optional

import typer

app = typer.Typer(help="Manage the Catchment's write-only secret store (referenced as ${secret:NAME}).",
                  no_args_is_help=True)


def _resolve(catchment: Optional[str]) -> tuple[str, dict]:
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    return cfg["url"], cfg


@app.command("set")
def set_(
    name: str = typer.Argument(..., help="Secret name (letters, digits, underscores)."),
    value: str = typer.Option(..., "--value", prompt="Secret value", hide_input=True,
                              help="The value (prompted hidden if omitted, so it stays out of argv/history)."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
) -> None:
    """Set (or overwrite) a secret. Stored write-only — it is never read back."""
    from . import _http

    url, cfg = _resolve(catchment)
    _http.post(f"{url}/api/secrets", auth=cfg, json={"name": name, "value": value})
    typer.echo(f"Secret '{name}' set.")


@app.command("ls")
def ls(
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
) -> None:
    """List secret names (never values)."""
    from rich.console import Console
    from rich.table import Table

    from . import _http

    url, cfg = _resolve(catchment)
    secrets = _http.get(f"{url}/api/secrets", auth=cfg).json().get("secrets", [])
    if not secrets:
        typer.echo("No secrets.")
        return
    table = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 1))
    table.add_column("Name")
    table.add_column("Set", style="dim")
    for s in secrets:
        table.add_row(s["name"], s.get("set_at") or "")
    Console().print(table)


@app.command("rm")
def rm(
    name: str = typer.Argument(..., help="Secret name to remove."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
) -> None:
    """Remove a secret."""
    from . import _http

    url, cfg = _resolve(catchment)
    _http.delete(f"{url}/api/secrets/{name}", auth=cfg)
    typer.echo(f"Secret '{name}' removed.")
