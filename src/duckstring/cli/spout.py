"""`duckstring spout add|ls|rm {pond}` — manage a Pond's egress Spouts.

A Spout pours a Pond's published output to an external destination (object store, a transactional
database). It is operational config (persisted, survives redeploys), not declared in pond.toml.
Credentials go in the destination URI as ``${env:NAME}`` references, resolved only at egress time::

    duckstring spout add sales --to 's3://bucket/sales?key=${env:AWS_KEY}'
    duckstring spout add sales --to 'postgres://app:${env:PGPASS}@db/analytics' --table revenue
    duckstring spout ls sales
    duckstring spout rm sales s3
"""

from __future__ import annotations

from typing import Optional

import typer

app = typer.Typer(help="Manage a Pond's egress Spouts (publish its output to external systems).", no_args_is_help=True)


def _resolve(catchment: Optional[str]) -> tuple[str, dict]:
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    return cfg["url"], cfg


@app.command("add")
def add(
    pond: str = typer.Argument(..., help="The Pond whose output to egress."),
    to: str = typer.Option(..., "--to", "-t", help="Destination URI (file://, s3://, gs://, postgres://); "
                                                   "credentials as ${env:NAME}."),
    table: Optional[str] = typer.Option(None, "--table", "-T", help="A single table to egress (default: all tables)."),
    all_tables: bool = typer.Option(False, "--all", help="Egress all of the Pond's tables (the default; explicit)."),
    mode: str = typer.Option("auto", "--mode", help="auto (incremental when possible) | full | append."),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Spout handle (default: derived from table/scheme)."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target."),
) -> None:
    """Bind a Spout to a Pond."""
    from . import _http

    if table and all_tables:
        raise typer.BadParameter("--table and --all are mutually exclusive")
    url, cfg = _resolve(catchment)
    resp = _http.post(
        f"{url}/api/ponds/{pond}/spouts", auth=cfg, params=_http.pond_params(major, version),
        json={"destination": to, "table": table, "mode": mode, "name": name},
    ).json()
    typer.echo(f"Spout '{resp['name']}' added on '{pond}' → {to}")


@app.command("ls")
def ls(
    pond: str = typer.Argument(..., help="The Pond whose Spouts to list."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target."),
) -> None:
    """List a Pond's Spouts."""
    from rich.console import Console
    from rich.table import Table

    from . import _http

    url, cfg = _resolve(catchment)
    spouts = _http.get(
        f"{url}/api/ponds/{pond}/spouts", auth=cfg, params=_http.pond_params(major, version),
    ).json().get("spouts", [])
    if not spouts:
        typer.echo("No spouts.")
        return

    table = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 1))
    for col in ("Name", "Table", "Destination", "Mode", "Delivered", "State"):
        table.add_column(col)
    for s in spouts:
        if s.get("is_failed"):
            state = f"[red]failed[/red] [dim]{s.get('error') or ''}[/dim]"
        elif s.get("failures"):
            state = f"[yellow]retrying ({s['failures']})[/yellow]"
        else:
            state = "[green]ok[/green]"
        table.add_row(
            s["name"], s.get("table") or "[dim]all[/dim]", s["destination"], s["mode"],
            s.get("watermark") or "[dim]never[/dim]", state,
        )
    Console().print(table)


@app.command("resync")
def resync(
    pond: str = typer.Argument(..., help="The Pond the Spout is on."),
    name: str = typer.Argument(..., help="The Spout's name (see `spout ls`)."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target."),
) -> None:
    """Force a full re-egress (clears the Spout's watermark + any failure)."""
    from . import _http

    url, cfg = _resolve(catchment)
    _http.post(
        f"{url}/api/ponds/{pond}/spouts/{name}/resync", auth=cfg,
        params=_http.pond_params(major, version), json={},
    )
    typer.echo(f"Spout '{name}' on '{pond}' will re-egress.")


@app.command("rm")
def rm(
    pond: str = typer.Argument(..., help="The Pond the Spout is on."),
    name: str = typer.Argument(..., help="The Spout's name (see `spout ls`)."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target."),
) -> None:
    """Remove a Spout from a Pond."""
    from . import _http

    url, cfg = _resolve(catchment)
    _http.post(
        f"{url}/api/ponds/{pond}/spouts/{name}/remove", auth=cfg,
        params=_http.pond_params(major, version), json={},
    )
    typer.echo(f"Spout '{name}' removed from '{pond}'.")
