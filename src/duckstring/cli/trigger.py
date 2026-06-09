from __future__ import annotations

from typing import Optional

import typer

_SILENT_HELP = "Submit the trigger without opening the live status view."
_WATCH_HELP  = "Keep the status view open even for one-shot triggers (never auto-close)."


def _post_trigger(
    url: str, outlet: str, major: Optional[int], version: Optional[str],
    silent: bool, watch: bool, endpoint: str, payload: dict, success_msg: str, one_shot: bool,
) -> None:
    from . import _http

    _http.post(f"{url}/api/ponds/{outlet}/{endpoint}", json=payload)

    if silent:
        typer.echo(success_msg)
        return

    # Open the live status focused on the target Pond. One-shot triggers (Tap/Pulse) close once it
    # settles back to idle; standing triggers (Wave/Tide) stay open until Ctrl+C.
    from .status import _run_live
    stay = watch or not one_shot
    _run_live(
        url, all_versions=False, pond_name=outlet, major=major, version_str=version,
        watch=stay, until_idle_pond=None if stay else outlet,
    )


def start(
    outlet: str = typer.Argument(..., help="Name of the Pond to start (one direct run)."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest active)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target, e.g. 1.2.3."),
    silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
    watch: bool = typer.Option(False, "--watch", help=_WATCH_HELP),
) -> None:
    """Inject demand directly into a Pond — one run against current inputs, no upstream propagation."""
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _post_trigger(cfg["url"], outlet, major, version, silent, watch, "start", {}, "Started.", one_shot=True)


def stop(
    outlet: str = typer.Argument(..., help="Name of the Pond to stop."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted)."),
    upstream: bool = typer.Option(False, "--upstream", help="Also stop all upstream (source) Ponds."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest active)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target, e.g. 1.2.3."),
) -> None:
    """Clear demand from a Pond (push + pull); started Pond Runs still complete."""
    from . import _http
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _http.post(f"{cfg['url']}/api/ponds/{outlet}/stop", json={"upstream": upstream})
    typer.echo("Stopped (upstream)." if upstream else "Stopped.")


def remove(
    outlet: str = typer.Argument(..., help="Name of the Outlet Pond whose standing trigger to remove."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest active)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target, e.g. 1.2.3."),
) -> None:
    """Remove the standing Wave/Tide trigger from an Outlet (existing work drains)."""
    from . import _http
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _http.post(f"{cfg['url']}/api/ponds/{outlet}/untrigger", json={})
    typer.echo("Trigger removed.")


def tap(
    outlet: str = typer.Argument(..., help="Name of the Outlet Pond to resupply once."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to run (default: latest active)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target, e.g. 1.2.3."),
    silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
    watch: bool = typer.Option(False, "--watch", help=_WATCH_HELP),
) -> None:
    """Pull an Outlet once (a single resupply from its sources)."""
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _post_trigger(cfg["url"], outlet, major, version, silent, watch, "tap", {}, "Tap sent.", one_shot=True)


def pulse(
    outlet: str = typer.Argument(..., help="Name of the Outlet Pond to trigger."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to run (default: latest active)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target, e.g. 1.2.3."),
    silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
    watch: bool = typer.Option(False, "--watch", help=_WATCH_HELP),
) -> None:
    """Push an Outlet once to current freshness (runs the pipeline through to it)."""
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _post_trigger(cfg["url"], outlet, major, version, silent, watch, "pulse", {}, "Pulse sent.", one_shot=True)


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
    _post_trigger(cfg["url"], outlet, major, version, silent, watch, "wave", {}, "Wave started.", one_shot=False)


def tide(
    outlet: str = typer.Argument(..., help="Name of the Outlet Pond to keep fresh."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted)."),
    bound: float = typer.Option(..., "--bound", help="Maximum staleness in seconds; the Outlet is kept no older than this."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest active)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target, e.g. 1.2.3."),
    silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
    watch: bool = typer.Option(False, "--watch", help=_WATCH_HELP),
) -> None:
    """Keep an Outlet no more stale than a bound (a staleness-clocked Pulse)."""
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _post_trigger(
        cfg["url"], outlet, major, version, silent, watch, "tide",
        {"bound_seconds": bound}, "Tide started.", one_shot=False,
    )
