from __future__ import annotations

from typing import Optional

import typer

_SILENT_HELP = "Submit the trigger without showing status output."
_WATCH_HELP  = "Live status after triggering; never auto-exits (implies live mode)."


def _post_trigger(
    url: str, outlet: str, major: Optional[int], version: Optional[str],
    silent: bool, watch: bool, endpoint: str, payload: dict,
) -> None:
    from rich.console import Console

    from . import _http

    _http.post(f"{url}/api/outlets/{outlet}/{endpoint}", json=payload)

    if not silent:
        from .status import _run_live
        _run_live(url, all_versions=False, pond_name=outlet, major=major, version_str=version, watch=watch)
    else:
        Console().print("[green]Done.[/green]")


def pulse(
    outlet: str = typer.Argument(..., help="Name of the Outlet Pond to trigger."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to run (default: latest active)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target, e.g. 1.2.3."),
    silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
    watch: bool = typer.Option(False, "--watch", help=_WATCH_HELP),
) -> None:
    """Emit a single Demand signal from an Outlet (executes the pipeline once)."""
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    payload: dict = {}
    if major is not None:
        payload["version"] = major
    _post_trigger(cfg["url"], outlet, major, version, silent, watch, "pulse", payload)


def wave(
    outlet: str = typer.Argument(..., help="Name of the Outlet Pond to trigger continuously."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest active)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target, e.g. 1.2.3."),
    silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
    watch: bool = typer.Option(False, "--watch", help=_WATCH_HELP),
) -> None:
    """Start continuous Demand from an Outlet (runs at maximum frequency)."""
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _post_trigger(cfg["url"], outlet, major, version, silent, watch, "wave", {})


def tide(
    outlet: str = typer.Argument(..., help="Name of the Outlet Pond to schedule."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted)."),
    cron: str = typer.Option(..., "--cron", help="Cron expression, e.g. '15 2 * * *'."),
    local: bool = typer.Option(False, "--local", help="Interpret the schedule in local time (default: UTC)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest active)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target, e.g. 1.2.3."),
    silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
    watch: bool = typer.Option(False, "--watch", help=_WATCH_HELP),
) -> None:
    """Schedule an Outlet to emit Demand on a cron schedule."""
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _post_trigger(cfg["url"], outlet, major, version, silent, watch, "tide", {"cron": cron, "local": local})
