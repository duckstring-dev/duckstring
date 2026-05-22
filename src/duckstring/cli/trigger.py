from __future__ import annotations

from typing import Optional

import typer


def pulse(
    outlet: str = typer.Argument(..., help="Name of the Outlet Pond to trigger."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted)."),
    version: Optional[int] = typer.Option(None, "--version", "-v", help="Major version to run (default: latest available)."),
) -> None:
    """Emit a single Demand signal from an Outlet (executes the DAG once)."""
    from rich.console import Console

    from . import _http
    from .config import resolve_catchment

    _, cfg = resolve_catchment(catchment)
    url = cfg["url"]

    payload: dict = {}
    if version is not None:
        payload["version"] = version

    console = Console()
    console.print(f"Pulsing [bold]{outlet}[/bold]...")
    _http.post(f"{url}/api/outlets/{outlet}/pulse", json=payload)
    console.print("[green]Pulse sent.[/green]")


def wave(
    outlet: str = typer.Argument(..., help="Name of the Outlet Pond to trigger continuously."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted)."),
) -> None:
    """Start continuous Demand from an Outlet (runs at maximum frequency)."""
    from rich.console import Console

    from . import _http
    from .config import resolve_catchment

    _, cfg = resolve_catchment(catchment)
    url = cfg["url"]

    console = Console()
    console.print(f"Starting wave on [bold]{outlet}[/bold]...")
    _http.post(f"{url}/api/outlets/{outlet}/wave")
    console.print("[green]Wave started.[/green]")


def tide(
    outlet: str = typer.Argument(..., help="Name of the Outlet Pond to schedule."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted)."),
    cron: str = typer.Option(..., "--cron", help="Cron expression, e.g. '15 2 * * *'."),
    local: bool = typer.Option(False, "--local", help="Interpret the schedule in local time (default: UTC)."),
) -> None:
    """Schedule an Outlet to emit Demand on a cron schedule."""
    from rich.console import Console

    from . import _http
    from .config import resolve_catchment

    _, cfg = resolve_catchment(catchment)
    url = cfg["url"]

    tz_label = "local time" if local else "UTC"
    console = Console()
    console.print(f"Scheduling tide on [bold]{outlet}[/bold] ([dim]{cron}[/dim], {tz_label})...")
    _http.post(f"{url}/api/outlets/{outlet}/tide", json={"cron": cron, "local": local})
    console.print("[green]Tide scheduled.[/green]")
