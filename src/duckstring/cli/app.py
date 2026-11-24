"""`duckstring app instances|logs|restart|scale|drain {app}`: an App's instances and their output (plans/apps.md),
and how many it runs (plans/app-scaling.md).

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
    for col in ("Instance", "Version", "Where", "Started", "Ready", "Stopped", "Reason"):
        table.add_column(col)
    for r in rows:
        reason = r["stop_reason"] or "[green]running[/green]"
        if r["exit_code"] is not None and r["stop_reason"] in ("exited", "start_failed"):
            reason = f"[red]{r['stop_reason']}[/red] [dim](exit code {r['exit_code']})[/dim]"
        if r.get("start_reason") and r["start_reason"] != "a request":
            reason += f" [dim](started for {r['start_reason']})[/dim]"
        where = "elsewhere" if r.get("place") == "remote" else "Catchment"
        table.add_row(str(r["id"]), r["version"], where, r["started_at"][:19], (r["ready_at"] or "")[:19],
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


@app.command("scale")
def scale(
    name: str = typer.Argument(..., help="The App."),
    min_instances: Optional[int] = typer.Option(None, "--min", help="Instances kept running (0: none when idle)."),
    max_instances: Optional[int] = typer.Option(None, "--max", help="The most instances the App runs."),
    concurrency: Optional[int] = typer.Option(None, "--concurrency", help="Active requests and sockets one instance "
                                                                         "takes before the App grows."),
    connections: Optional[int] = typer.Option(None, "--connections", help="PostgreSQL connections one instance may "
                                                                         "hold (default: its concurrency)."),
    requests: Optional[float] = typer.Option(None, "--requests", help="Grow at this share of concurrency (0.7)."),
    cpu: Optional[float] = typer.Option(None, "--cpu", help="Grow at this share of an instance's CPU (0.7)."),
    memory: Optional[float] = typer.Option(None, "--memory", help="Grow at this share of an instance's memory (0.8)."),
    load: Optional[float] = typer.Option(None, "--load", help="Grow at this load the App reports itself (0.7)."),
    to: Optional[int] = typer.Option(None, "--to", help="Set the number of instances now."),
    hold: Optional[str] = typer.Option(None, "--hold", help="With --to: keep exactly that many for this long "
                                                            "(e.g. 2h)."),
    release: bool = typer.Option(False, "--release", help="Drop a count set with --to."),
    clear: bool = typer.Option(False, "--clear", help="Drop every override: back to pond.toml."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target."),
) -> None:
    """Show or change how many instances an App runs. With no options, shows its settings (and where each comes
    from), its running instances and recent decisions. Overrides are kept across redeploys."""
    from . import _http

    url, cfg = _resolve(catchment)
    params = _http.pond_params(major, version)
    body = {"min_instances": min_instances, "max_instances": max_instances, "concurrency": concurrency,
            "connections": connections, "requests": requests, "cpu": cpu, "memory": memory, "load": load,
            "to": to, "release": release, "clear": clear}
    if hold is not None:
        if to is None:
            raise typer.BadParameter("--hold goes with --to")
        from ..app import _duration_s

        try:
            body["hold_s"] = _duration_s(hold, "--hold")
        except ValueError as e:
            raise typer.BadParameter(str(e).replace("pond.toml ", "")) from None
    if any(v not in (None, False) for v in body.values()):
        state = _http.post(f"{url}/api/ponds/{name}/app/scale", auth=cfg, params=params,
                           json={k: v for k, v in body.items() if v is not None}).json()
    else:
        state = _http.get(f"{url}/api/ponds/{name}/app/scale", auth=cfg, params=params).json()
    _print_scale(state)


def _print_scale(state: dict) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    s, origin = state["settings"], state["origin"]

    def mark(key: str) -> str:
        o = origin.get(key, "default")
        return "" if o == "default" else f" [dim]({o})[/dim]"

    where = "its own compute" if state["compute"] == "elsewhere" else "the Catchment"
    console.print(f"[bold]{state['pond']}[/bold] runs on {where}")
    console.print(f"  instances {s['min_instances']}{mark('min_instances')} to {s['instances']}{mark('instances')}, "
                  f"concurrency {s['concurrency']}{mark('concurrency')}, connections {s['connections']}"
                  f"{mark('connections')}")
    t = s["scale"]
    console.print(f"  grows at {round(t['requests'] * 100)}% of concurrency{mark('scale.requests')}, CPU "
                  f"{round(t['cpu'] * 100)}%{mark('scale.cpu')}, memory {round(t['memory'] * 100)}%{mark('scale.memory')}"
                  f" or load {round(t['load'] * 100)}%{mark('scale.load')}, held {t['after_s']:g}s")
    if state.get("hold"):
        until = state["hold"].get("until")
        console.print(f"  [yellow]held at {state['hold']['count']}[/yellow]"
                      + (f" until {until[:19]}" if until else " until reached"))
    if state.get("pressed"):
        console.print(f"  [yellow]at its maximum under pressure: {state['pressed']}[/yellow]")
    if state["instances"]:
        table = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 1))
        for col in ("Instance", "Where", "Active", "CPU", "Memory", "Load", "State"):
            table.add_column(col)
        pct = lambda v: "" if v is None else f"{round(v * 100)}%"  # noqa: E731
        for r in state["instances"]:
            st = "draining" if r["draining"] else ("ready" if r["ready"] else "starting")
            table.add_row(str(r["id"]), "elsewhere" if r["where"] == "remote" else "Catchment",
                          f"{r['active']} ({r['open_sockets']} sockets)" if r["open_sockets"] else str(r["active"]),
                          pct(r["cpu"]), pct(r["memory"]), pct(r["load"]), st)
        console.print(table)
    for d in state.get("decisions", [])[-5:]:
        console.print(f"  [dim]{d['at'][:19]}  {d['reason']}[/dim]")


@app.command("drain")
def drain(
    name: str = typer.Argument(..., help="The App."),
    instance: int = typer.Argument(..., help="The instance (see `duckstring app scale`)."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target."),
) -> None:
    """Send no new sessions to one instance; it stops once its work is done."""
    from . import _http

    url, cfg = _resolve(catchment)
    _http.post(f"{url}/api/ponds/{name}/app/instances/{instance}/drain", auth=cfg, params=_http.pond_params(major, version))
    typer.echo(f"Draining instance {instance} of {name}: it stops once its work is done.")


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
