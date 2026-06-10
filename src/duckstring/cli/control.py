from __future__ import annotations

from typing import Optional

import typer

from .trigger import _SILENT_HELP, _WATCH_HELP, _post_trigger

_CATCHMENT = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted).")
_MAJOR = typer.Option(None, "--major", "-m", help="Major version to target (default: latest active).")
_VERSION = typer.Option(None, "--version", "-v", help="Specific semver to target, e.g. 1.2.3.")


def wake(
    pond: str = typer.Argument(..., help="Name of the Pond to wake."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
    silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
    watch: bool = typer.Option(False, "--watch", help=_WATCH_HELP),
) -> None:
    """Wake a Pond — run once if its Sources already hold fresher data (no upstream solicit). Gentle."""
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _post_trigger(cfg["url"], pond, major, version, silent, watch, "wake", {}, "Woken.", one_shot=True)


def force(
    pond: str = typer.Argument(..., help="Name of the Pond to force a recompute on."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
    silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
    watch: bool = typer.Option(False, "--watch", help=_WATCH_HELP),
) -> None:
    """Force a Pond to recompute now at its current freshness, even with no upstream change (e.g. after
    a patch). Does not propagate downstream — freshness is unchanged."""
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _post_trigger(cfg["url"], pond, major, version, silent, watch, "force", {}, "Forced.", one_shot=True)


def sleep(
    pond: str = typer.Argument(..., help="Name of the Pond to put to sleep."),
    catchment: Optional[str] = _CATCHMENT,
    upstream: bool = typer.Option(False, "--upstream", help="Also sleep all upstream (source) Ponds."),
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
) -> None:
    """Sleep a Pond — clear its demand (push + pull); started Pond Runs still complete. Gentle."""
    from . import _http
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _http.post(f"{cfg['url']}/api/ponds/{pond}/sleep", json={"upstream": upstream})
    typer.echo("Asleep (with upstream)." if upstream else "Asleep.")


def kill(
    pond: str = typer.Argument(..., help="Name of the Pond to kill."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
) -> None:
    """Kill a Pond — terminate its Duck and cancel its running Pond Run. Terminal: it stays killed
    (no retries) until a Wake, Force, or `failure clear`."""
    from . import _http
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _http.post(f"{cfg['url']}/api/ponds/{pond}/kill", json={})
    typer.echo(f"Killed '{pond}'.")
