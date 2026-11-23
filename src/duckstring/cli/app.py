"""`duckstring app instances|logs|restart {app}`: an App's instances and their output (plans/apps.md).

An App is deployed with `duckstring pond deploy` like any Pond, and its Refreshes and Control verbs are the usual
ones. These commands show its instances (one process each, from start to stop), read their logs, and restart it::

    duckstring app instances sales_dashboard
    duckstring app logs sales_dashboard --follow
    duckstring app restart sales_dashboard
"""

from __future__ import annotations

import time
from typing import Optional

import typer

app = typer.Typer(help="Inspect an App's instances and logs, restart it, and manage its data across majors.",
                  no_args_is_help=True)


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


@app.command("restart")
def restart(
    name: str = typer.Argument(..., help="The App."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target."),
) -> None:
    """Stop the App's instance and start a fresh one. Clears a failure or a Kill first."""
    from . import _http

    url, cfg = _resolve(catchment)
    _http.post(f"{url}/api/ponds/{name}/app/restart", auth=cfg, params=_http.pond_params(major, version))
    typer.echo(f"Restarting {name}: see `duckstring app instances {name}`.")


@app.command("migrate")
def migrate(
    name: str = typer.Argument(..., help="The App."),
    major: int = typer.Option(..., "--major", "-m", help="The trial major to migrate again."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
) -> None:
    """Migrate a trial major again from the live one.

    Its Records and Media are copied afresh from the live major (what was written to the trial is discarded) and
    its `[app] migrate` command runs over the copy. `pond promote` runs the final migration and makes it live.
    """
    from . import _http

    url, cfg = _resolve(catchment)
    got = _http.post(f"{url}/api/ponds/{name}/app/migrate", auth=cfg, params={"major": major}).json()
    typer.echo(f"Migrated {got['migrated']} again from v{got['from']}.")


@app.command("purge")
def purge(
    line: str = typer.Argument(..., help="The App line, as name@major."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
) -> None:
    """Delete an App line's Records and Media for good.

    For a retired line, a trial, or an App that was removed (its data is kept until purged). The live line of a
    deployed App can't be purged.
    """
    from . import _http
    from .config import resolve_catchment

    app_name, at, major = line.partition("@")
    if not at or not major.isdigit():
        typer.echo("Error: name the line as name@major, such as feedback@1.", err=True)
        raise typer.Exit(1)
    cname, cfg = resolve_catchment(catchment)
    meta = _http.get(f"{cfg['url']}/api/catchment/identity", auth=cfg).json()
    confirm = meta.get("name") or meta.get("id") or cname
    if not yes:
        typer.confirm(f"Delete {line}'s Records and Media on '{cname}' for good?", default=False, abort=True)
    _http.post(f"{cfg['url']}/api/app-data/{app_name}/{major}/purge", auth=cfg, json={"confirm": confirm})
    typer.echo(f"Purged {line}: its Records and Media are deleted.")
