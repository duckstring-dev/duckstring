from __future__ import annotations

import typer


def status(
    catchment: str = typer.Argument(..., help="Name of the registered Catchment."),
    all: bool = typer.Option(False, "--all", "-a", help="Include all Ponds, not just active ones."),
) -> None:
    """Print a summary of Pond activity in the Catchment."""
    from rich.console import Console
    from rich.table import Table

    from . import _http
    from .config import resolve_catchment

    cfg = resolve_catchment(catchment)
    url = cfg["url"]

    params = {"all": "true"} if all else {}
    resp = _http.get(f"{url}/api/status", params=params)

    data = resp.json()
    ponds = data.get("ponds", [])

    console = Console()
    if not ponds:
        console.print(f"[dim]No active Ponds in [bold]{catchment}[/bold].[/dim]")
        return

    table = Table(show_header=True, header_style="bold dim")
    table.add_column("Pond", style="bold")
    table.add_column("Version")
    table.add_column("Status")
    table.add_column("Demand")
    table.add_column("Last run", style="dim")

    for pond in ponds:
        table.add_row(
            pond.get("name", "?"),
            pond.get("version", "?"),
            pond.get("status", "?"),
            pond.get("demand", "—"),
            pond.get("last_run", "—"),
        )

    console.print(table)
