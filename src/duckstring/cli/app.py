"""`duckstring app instances|logs {app}`: an App's instances and their output (plans/apps.md).

An App is deployed with `duckstring pond deploy` like any Pond, and its Refreshes and Control verbs are the usual
ones. These commands show its instances (one process each, from start to stop) and read their logs::

    duckstring app instances sales_dashboard
    duckstring app logs sales_dashboard --follow
"""

from __future__ import annotations

import time
from typing import Optional

import typer

app = typer.Typer(help="Inspect an App's instances and logs.", no_args_is_help=True)


def _resolve(catchment: Optional[str]) -> tuple[str, dict]:
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    return cfg["url"], cfg


@app.command("instances")
def instances(
    name: str = typer.Argument(..., help="The App."),
    limit: int = typer.Option(20, "--limit", "-n", help="How many instances to show, newest first."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target."),
) -> None:
    """List an App's recent instances: when each started, became ready and stopped, and why it stopped."""
    from rich.console import Console
    from rich.table import Table

    from . import _http

    url, cfg = _resolve(catchment)
    params = {**_http.pond_params(major, version), "limit": limit}
    rows = _http.get(f"{url}/api/ponds/{name}/app/instances", auth=cfg, params=params).json()["instances"]
    if not rows:
        typer.echo("No instances yet: the App starts on its first request.")
        return
    table = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 1))
    for col in ("Instance", "Version", "Started", "Ready", "Stopped", "Reason"):
        table.add_column(col)
    for r in rows:
        reason = r["stop_reason"] or "[green]running[/green]"
        if r["exit_code"] is not None and r["stop_reason"] in ("exited", "start_failed"):
            reason = f"[red]{r['stop_reason']}[/red] [dim](exit code {r['exit_code']})[/dim]"
        table.add_row(str(r["id"]), r["version"], r["started_at"][:19], (r["ready_at"] or "")[:19],
                      (r["stopped_at"] or "")[:19], reason)
    Console().print(table)


@app.command("logs")
def logs(
    name: str = typer.Argument(..., help="The App."),
    instance: Optional[int] = typer.Option(None, "--instance", "-i", help="Which instance (default: the newest)."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Keep printing new output until the instance stops."),
    tail: int = typer.Option(65536, "--tail", help="How many bytes of the end of the log to show first."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target."),
) -> None:
    """Print an instance's output (stdout and stderr)."""
    from . import _http

    url, cfg = _resolve(catchment)
    params = {**_http.pond_params(major, version), "tail": tail}
    if instance is not None:
        params["instance"] = instance
    resp = _http.get(f"{url}/api/ponds/{name}/app/logs", auth=cfg, params=params)
    typer.echo(resp.text, nl=False)
    if not follow:
        return
    params = {**_http.pond_params(major, version), "instance": resp.headers["x-log-instance"]}
    offset = resp.headers["x-log-offset"]
    running = resp.headers.get("x-log-running") == "1"
    try:
        while running:
            time.sleep(1.0)
            resp = _http.get(f"{url}/api/ponds/{name}/app/logs", auth=cfg, params={**params, "offset": offset})
            typer.echo(resp.text, nl=False)
            offset = resp.headers["x-log-offset"]
            running = resp.headers.get("x-log-running") == "1"
    except KeyboardInterrupt:
        pass
