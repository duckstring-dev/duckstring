from __future__ import annotations

from typing import Optional

import typer

from .trigger import _SILENT_HELP, _WATCH_HELP, _post_trigger

_CATCHMENT = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted).")
_MAJOR = typer.Option(None, "--major", "-m", help="Major version to target (default: latest).")
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
    _post_trigger(cfg, pond, major, version, silent, watch, "wake", {}, "Woken.", one_shot=True)


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
    _post_trigger(cfg, pond, major, version, silent, watch, "force", {}, "Forced.", one_shot=True)


def refresh(
    pond: str = typer.Argument(..., help="Name of the Pond to flag for a refresh."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
    clear: bool = typer.Option(False, "--clear", help="Un-set a pending refresh instead."),
) -> None:
    """Refresh a Pond — flag its *next* run to be a cold wipe-and-rebuild (full recompute, clears the
    changelog so downstream reloads). Lazy: nothing runs now; it takes effect on the next run. For an
    immediate rebuild across a set of Ponds, use `control repair`."""
    from . import _http
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _http.post(
        f"{cfg['url']}/api/ponds/{pond}/refresh", auth=cfg,
        params={**_http.pond_params(major, version), "clear": clear}, json={},
    )
    typer.echo(f"Refresh cleared on '{pond}'." if clear else f"'{pond}' will refresh on its next run.")


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
    _http.post(
        f"{cfg['url']}/api/ponds/{pond}/sleep", auth=cfg,
        params=_http.pond_params(major, version), json={"upstream": upstream},
    )
    typer.echo("Asleep (with upstream)." if upstream else "Asleep.")


def kill(
    pond: str = typer.Argument(..., help="Name of the Pond to kill."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
) -> None:
    """Kill a Pond — terminate its Duck and cancel its running Pond Run. Terminal: it stays killed
    (no retries) until a Wake, Force, or `control clear`."""
    from . import _http
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _http.post(
        f"{cfg['url']}/api/ponds/{pond}/kill", auth=cfg,
        params=_http.pond_params(major, version), json={},
    )
    typer.echo(f"Killed '{pond}'.")


def clear(
    pond: str = typer.Argument(..., help="Name of the failed Pond to clear."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
) -> None:
    """Clear a failed Pond — reset its failure and unblock everything downstream (no run)."""
    from . import _http
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _http.post(
        f"{cfg['url']}/api/ponds/{pond}/clear", auth=cfg,
        params=_http.pond_params(major, version), json={},
    )
    typer.echo(f"Cleared failure from '{pond}'.")


def failure_budget(
    pond: str = typer.Argument(..., help="Name of the Pond whose retry budgets to show or set."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
    immediate: Optional[int] = typer.Option(
        None, "--immediate", "-i", help="Ripple-Run retries allowed within one Pond Run."
    ),
    on_change: Optional[int] = typer.Option(
        None, "--on-change", "-o", help="Pond Runs to retry after a Source updates."
    ),
) -> None:
    """Show or set a Pond's retry budgets. With no flags, prints the current values."""
    from . import _http
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    params = _http.pond_params(major, version)
    cur = _http.get(f"{cfg['url']}/api/ponds/{pond}/budget", auth=cfg, params=params).json()
    if immediate is None and on_change is None:
        typer.echo(f"immediate: {cur['immediate_retries']}   on-change: {cur['source_retries']}")
        return
    imm = cur["immediate_retries"] if immediate is None else immediate
    onc = cur["source_retries"] if on_change is None else on_change
    if imm < 0 or onc < 0:
        typer.echo("Error: budgets must be non-negative.", err=True)
        raise typer.Exit(1)
    _http.post(
        f"{cfg['url']}/api/ponds/{pond}/budget", auth=cfg, params=params,
        json={"immediate_retries": imm, "source_retries": onc},
    )
    typer.echo(f"Set '{pond}' — immediate: {imm}   on-change: {onc}")
