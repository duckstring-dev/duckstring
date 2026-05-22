from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(help="Work with Catchments.", add_completion=False, no_args_is_help=True)


def _offer_default(name: str, yes: bool) -> None:
    """Prompt to set name as the default catchment, unless it already is."""
    from .config import get_default_catchment, set_default_catchment

    if get_default_catchment() == name:
        return
    if yes or typer.confirm(f"Set '{name}' as default catchment?", default=True):
        set_default_catchment(name)
        typer.echo(f"Default catchment set to '{name}'.")


@app.command()
def start(
    name: str = typer.Option(..., "--name", "-n", prompt="Catchment name", help="Name to register this Catchment under."),
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind to."),
    port: int = typer.Option(7474, "--port", "-p", help="Port to listen on."),
    root: Optional[Path] = typer.Option(None, "--root", help="Root directory for Catchment data."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Automatically set as default catchment."),
) -> None:
    """Start a local Catchment server and register it by name."""
    import uvicorn
    from rich.console import Console
    from rich.panel import Panel

    from .config import CONFIG_DIR, register_catchment

    root_dir = Path(root) if root else CONFIG_DIR / name
    root_dir.mkdir(parents=True, exist_ok=True)

    url = f"http://{host}:{port}"
    register_catchment(name, url=url, kind="local", root=str(root_dir))
    _offer_default(name, yes)

    console = Console()
    console.print(
        Panel(
            f"[bold white]duckstring catchment[/bold white] [bold cyan]{name}[/bold cyan]\n\n"
            f"  [dim]url:  {url}[/dim]\n"
            f"  [dim]root: {root_dir}[/dim]\n\n"
            f"  Press [bold]Ctrl-C[/bold] to stop.",
            border_style="bright_black",
            padding=(1, 2),
        )
    )
    from duckstring.catchment.app import create_app

    uvicorn.run(create_app(root_dir), host=host, port=port, reload=False, log_level="warning")


@app.command()
def connect(
    name: str = typer.Option(..., "--name", "-n", help="Name to register this Catchment under."),
    path: str = typer.Option(..., "--path", help="URL of the remote Catchment server."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Automatically set as default catchment."),
) -> None:
    """Register a remote Catchment server by name."""
    from rich.console import Console

    from .config import register_catchment

    register_catchment(name, url=path, kind="remote")
    console = Console()
    console.print(f"[green]Registered[/green] catchment [bold]{name}[/bold] → {path}")
    _offer_default(name, yes)


@app.command(name="list")
def list_cmd() -> None:
    """List all registered Catchments."""
    from rich.console import Console
    from rich.table import Table

    from .config import get_default_catchment, list_catchments

    items = list_catchments()
    if not items:
        typer.echo("No catchments registered.")
        typer.echo("  duckstring catchment start --name <name>")
        typer.echo("  duckstring catchment connect --name <name> --path <url>")
        return

    default = get_default_catchment()
    # If no explicit default but only one catchment, that one is implicitly default.
    if default is None and len(items) == 1:
        default = items[0][0]

    console = Console()
    table = Table(show_header=True, header_style="bold dim")
    table.add_column("", width=1)
    table.add_column("Name", style="bold")
    table.add_column("Type")
    table.add_column("URL")
    table.add_column("Root", style="dim")

    for n, cfg in items:
        marker = "[green]●[/green]" if n == default else ""
        table.add_row(marker, n, cfg.get("type", "?"), cfg.get("url", "?"), cfg.get("root", ""))

    console.print(table)


@app.command(name="set-default")
def set_default(
    name: str = typer.Argument(..., help="Name of the registered Catchment to use as default."),
) -> None:
    """Set the default Catchment used when none is specified on a command."""
    from .config import list_catchments, set_default_catchment

    registered = {n for n, _ in list_catchments()}
    if name not in registered:
        typer.echo(f"Error: no catchment '{name}' registered.", err=True)
        raise typer.Exit(1)
    set_default_catchment(name)
    typer.echo(f"Default catchment set to '{name}'.")
