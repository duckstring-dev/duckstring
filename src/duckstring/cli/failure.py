from __future__ import annotations

from typing import Optional

import typer

app = typer.Typer(
    help="Manage Pond failures: clear a failed Pond and set its retry budgets.",
    add_completion=False,
    no_args_is_help=True,
)

_CATCHMENT = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted).")


@app.command()
def clear(
    pond: str = typer.Argument(..., help="Name of the failed Pond to clear."),
    catchment: Optional[str] = _CATCHMENT,
) -> None:
    """Clear a failed Pond — reset its failure and unblock everything downstream (no run)."""
    from . import _http
    from .config import resolve_catchment

    _, cfg = resolve_catchment(catchment)
    _http.post(f"{cfg['url']}/api/ponds/{pond}/clear", json={})
    typer.echo(f"Cleared '{pond}'.")


@app.command()
def budget(
    pond: str = typer.Argument(..., help="Name of the Pond whose retry budgets to show or set."),
    catchment: Optional[str] = _CATCHMENT,
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
    cur = _http.get(f"{cfg['url']}/api/ponds/{pond}/budget").json()
    if immediate is None and on_change is None:
        typer.echo(f"immediate: {cur['immediate_retries']}   on-change: {cur['source_retries']}")
        return
    imm = cur["immediate_retries"] if immediate is None else immediate
    onc = cur["source_retries"] if on_change is None else on_change
    if imm < 0 or onc < 0:
        typer.echo("Error: budgets must be non-negative.", err=True)
        raise typer.Exit(1)
    _http.post(f"{cfg['url']}/api/ponds/{pond}/budget", json={"immediate_retries": imm, "source_retries": onc})
    typer.echo(f"Set '{pond}' — immediate: {imm}   on-change: {onc}")
