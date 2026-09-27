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
    """Run the Pond once if its Sources already have newer data, without asking them to run."""
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _post_trigger(cfg, pond, major, version, silent, watch, "wake", {}, "Woken.", one_shot=True)


def force(
    pond: str = typer.Argument(..., help="Name of the Pond to rerun."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
    silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
    watch: bool = typer.Option(False, "--watch", help=_WATCH_HELP),
) -> None:
    """Rerun the Pond now at its current freshness, even with no upstream change, e.g. after deploying a fix.

    Freshness doesn't change, so Ponds downstream don't rerun because of it.
    """
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _post_trigger(cfg, pond, major, version, silent, watch, "force", {}, "Forced.", one_shot=True)


def refresh(
    pond: str = typer.Argument(..., help="Name of the Pond to rebuild on its next run."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
    clear: bool = typer.Option(False, "--clear", help="Remove a pending refresh instead."),
) -> None:
    """Rebuild the Pond from scratch on its next run.

    Its working database is dropped and every Source is read in full, so Ponds downstream also read its
    Trickles in full. Nothing runs now. For an immediate rebuild, use `control repair`.
    """
    from . import _http
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _http.post(
        f"{cfg['url']}/api/ponds/{pond}/refresh", auth=cfg,
        params={**_http.pond_params(major, version), "clear": clear}, json={},
    )
    typer.echo(f"Refresh cleared on '{pond}'." if clear else f"'{pond}' will refresh on its next run.")


def reset(
    pond: str = typer.Argument(..., help="Name of the Pond to reset."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
    clear_history: bool = typer.Option(False, "--clear-history", help="Also delete the Pond's run history."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Return the Pond to its freshly deployed state.

    Deletes its published data, working database and run ledger, and resets its freshness. Keeps its code,
    configuration and demand, so it rebuilds from scratch the next time it runs. The Pond must be idle.
    """
    from . import _http
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    if not yes:
        typer.confirm(f"Reset '{pond}' (scrub its data + state; keeps code, config, demand)?", abort=True)
    _http.post(
        f"{cfg['url']}/api/ponds/{pond}/reset", auth=cfg,
        params={**_http.pond_params(major, version), "clear_history": clear_history}, json={},
    )
    typer.echo(f"Reset '{pond}'. It rebuilds from scratch when next demanded.")


def repair(
    ponds: list[str] = typer.Argument(..., help="Ponds to rebuild. Must be connected."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    downstream: bool = typer.Option(False, "--downstream", help="Also rebuild everything downstream."),
) -> None:
    """Rebuild a set of Ponds now, in dependency order, each reading its parents' rebuilt output.

    The set must be connected: a Pond linking two selected Ponds must be selected too. --downstream adds
    everything downstream.
    """
    from . import _http
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    body = {"ponds": [{"name": p, "major": major} for p in ponds], "downstream": downstream}
    resp = _http.post(f"{cfg['url']}/api/repair", auth=cfg, json=body)
    order = resp.json().get("scope", [])
    typer.echo(f"Repairing {len(order)} Pond(s) in order: {' → '.join(order)}")


def sleep(
    pond: str = typer.Argument(..., help="Name of the Pond to put to sleep."),
    catchment: Optional[str] = _CATCHMENT,
    upstream: bool = typer.Option(False, "--upstream", help="Also sleep every Pond upstream."),
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
) -> None:
    """Clear the Pond's demand and remove its standing trigger. Runs in progress finish."""
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
    """Stop the Pond's Duck immediately, abandoning its current run.

    The Pond stays killed, with no runs or retries, until it's woken, forced or cleared.
    """
    from . import _http
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _http.post(
        f"{cfg['url']}/api/ponds/{pond}/kill", auth=cfg,
        params=_http.pond_params(major, version), json={},
    )
    typer.echo(f"Killed '{pond}'.")


def clear(
    pond: str = typer.Argument(..., help="Name of the failed or killed Pond to clear."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
) -> None:
    """Reset a failed or killed Pond without running it, and unblock the Ponds downstream."""
    from . import _http
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    _http.post(
        f"{cfg['url']}/api/ponds/{pond}/clear", auth=cfg,
        params=_http.pond_params(major, version), json={},
    )
    typer.echo(f"Cleared failure from '{pond}'.")


def reset_contract(
    pond: str = typer.Argument(..., help="Pond whose recorded output schema to forget."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
) -> None:
    """Forget the output schema recorded for this major line, and clear the failure.

    The next successful run records the schema again. Use it when a run failed for narrowing a column's
    type and you accept the change within the same major version. Ponds downstream may rely on the old
    schema, so it asks for confirmation.
    """
    from . import _http
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    if not yes:
        typer.confirm(
            f"Drop the recorded output schema for '{pond}'? A Sink pinned to this major line loses the "
            "guarantee that its columns keep their types.",
            abort=True,
        )
    r = _http.post(
        f"{cfg['url']}/api/ponds/{pond}/reset-contract", auth=cfg,
        params=_http.pond_params(major, version), json={},
    ).json()
    typer.echo(f"Contract reset for '{pond}' ({r.get('columns_cleared', 0)} recorded columns dropped); "
               "the next accepted run re-freezes it.")


def failure_budget(
    pond: str = typer.Argument(..., help="Name of the Pond whose retry budgets to show or set."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
    immediate: Optional[int] = typer.Option(
        None, "--immediate", "-i", help="Retries of a failed Ripple within the same Pond Run."
    ),
    on_change: Optional[int] = typer.Option(
        None, "--on-change", "-o", help="Retries of a failed Pond Run when a Source next updates."
    ),
) -> None:
    """Show a Pond's retry budgets, or set them. They replace the values from pond.toml."""
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
