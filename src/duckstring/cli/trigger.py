from __future__ import annotations

from typing import Optional

import typer

_SILENT_HELP = "Send the request without opening the live status view."
_WATCH_HELP  = "Keep the status view open after the Pond settles."


def _post_trigger(
    cfg: dict, outlet: str, major: Optional[int], version: Optional[str],
    silent: bool, watch: bool, endpoint: str, payload: dict, success_msg: str, one_shot: bool,
) -> None:
    from . import _http

    url = cfg["url"]
    _http.post(
        f"{url}/api/ponds/{outlet}/{endpoint}", auth=cfg,
        params=_http.pond_params(major, version), json=payload,
    )

    if silent:
        typer.echo(success_msg)
        return

    # Open the live status focused on the target Pond. One-shot triggers (Tap/Pulse) close once it
    # settles back to idle; standing triggers (Wave/Tide) stay open until Ctrl+C.
    from .status import _run_live
    stay = watch or not one_shot
    _run_live(
        url, auth=cfg, pond_name=outlet, major=major, version_str=version,
        watch=stay, until_idle_pond=None if stay else outlet,
    )


def remove(
    outlet: str = typer.Argument(..., help="The Pond whose standing trigger to remove."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target, e.g. 1.2.3."),
) -> None:
    """Remove the Pond's standing Wave or Tide. Runs in progress finish."""
    from . import _http
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _http.post(
        f"{cfg['url']}/api/ponds/{outlet}/untrigger", auth=cfg,
        params=_http.pond_params(major, version), json={},
    )
    typer.echo("Trigger removed.")


def tap(
    outlet: str = typer.Argument(..., help="The Pond to tap, usually an Outlet."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target, e.g. 1.2.3."),
    silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
    watch: bool = typer.Option(False, "--watch", help=_WATCH_HELP),
) -> None:
    """Ask the Pond to run once with fresher data than it has.

    If its Sources have nothing newer, the request passes upstream until it reaches Ponds that can run.
    """
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _post_trigger(cfg, outlet, major, version, silent, watch, "tap", {}, "Tap sent.", one_shot=True)


def pulse(
    outlet: str = typer.Argument(..., help="The Pond to pulse, usually an Outlet."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target, e.g. 1.2.3."),
    silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
    watch: bool = typer.Option(False, "--watch", help=_WATCH_HELP),
) -> None:
    """Ask for data at least as fresh as now.

    Every older Pond upstream runs, and the result flows down to this Pond.
    """
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _post_trigger(cfg, outlet, major, version, silent, watch, "pulse", {}, "Pulse sent.", one_shot=True)


def wave(
    outlet: str = typer.Argument(..., help="The Pond to wave, usually an Outlet."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target, e.g. 1.2.3."),
    silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
) -> None:
    """Tap the Pond again every time it finishes, until removed."""
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _post_trigger(cfg, outlet, major, version, silent, False, "wave", {}, "Wave started.", one_shot=False)


def tide(
    outlet: str = typer.Argument(..., help="The Pond to keep fresh, usually an Outlet."),
    bound: str = typer.Argument(..., help="How old the data may get, e.g. 30s, 12h, 1d or 1h30m."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target, e.g. 1.2.3."),
    silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
) -> None:
    """Keep the Pond no older than BOUND, e.g. `tide reports 1d` to refresh it daily.

    Sends a Pulse whenever the data would otherwise become older than BOUND.
    """
    from .config import resolve_catchment
    from .window import _parse_duration
    _, cfg = resolve_catchment(catchment)
    _post_trigger(
        cfg, outlet, major, version, silent, False, "tide",
        {"bound_seconds": _parse_duration(bound)}, "Tide started.", one_shot=False,
    )
