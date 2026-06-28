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


# ─── Spout windows (throttle the standing Wake) ──────────────────────────────

window_app = typer.Typer(help="Throttle a Spout's standing Wake to a window cadence.", no_args_is_help=True)
app.add_typer(window_app, name="window")


@window_app.callback()
def _window_main(
    ctx: typer.Context,
    pond: str = typer.Argument(..., help="The Pond the Spout is on."),
    spout: str = typer.Argument(..., help="The Spout's name."),
) -> None:
    ctx.obj = {"pond": pond, "spout": spout}


@window_app.command("add")
def window_add(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", "-n", help="Unique identifier for the window rule."),
    every: str = typer.Option(..., "--every", "-e", help="Recurrence interval (single unit), e.g. 1d, 12h, 30m."),
    start: Optional[str] = typer.Option(None, "--start", "-s", help="Window start (ISO 8601 or HH:MM); default 00:00 today."),
    duration: Optional[str] = typer.Option(None, "--duration", "-d", help="Window length; default = --every (back-to-back)."),
    on: Optional[str] = typer.Option(None, "--on", "-o", help="Restrict to weekdays, e.g. MON,WED,FRI."),
    until: Optional[str] = typer.Option(None, "--until", "-u", help="Expiration (ISO 8601)."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target."),
) -> None:
    """Throttle the Spout to deliver at most once per window."""
    from . import _http
    from .window import _parse_days, _parse_dt, _parse_duration, _parse_every

    unit, interval = _parse_every(every)
    payload = {
        "name": name,
        "start_anchor": _parse_dt(start or "00:00", allow_hhmm=True),
        "duration_seconds": _parse_duration(duration) if duration else _parse_duration(every),
        "freq_unit": unit, "freq_interval": interval,
        "valid_days": _parse_days(on),
        "until_time": _parse_dt(until, allow_hhmm=False) if until else None,
    }
    url, cfg = _resolve(catchment)
    o = ctx.obj
    _http.post(
        f"{url}/api/ponds/{o['pond']}/spouts/{o['spout']}/windows", auth=cfg,
        params=_http.pond_params(major, version), json=payload,
    )
    typer.echo(f"Window '{name}' added to spout '{o['spout']}'.")


@window_app.command("list")
def window_list(
    ctx: typer.Context,
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target."),
) -> None:
    """List the Spout's throttle windows."""
    from rich.console import Console
    from rich.table import Table

    from . import _http
    from .window import _UNIT_ABBREV, _fmt_duration

    url, cfg = _resolve(catchment)
    o = ctx.obj
    windows = _http.get(
        f"{url}/api/ponds/{o['pond']}/spouts/{o['spout']}/windows", auth=cfg,
        params=_http.pond_params(major, version),
    ).json().get("windows", [])
    if not windows:
        typer.echo("No windows.")
        return
    table = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 1))
    for col in ("Name", "Start", "Duration", "Every", "Days", "Until"):
        table.add_column(col)
    for w in windows:
        table.add_row(
            w["name"], w["start_anchor"], _fmt_duration(w["duration_seconds"]),
            f"{w['freq_interval']}{_UNIT_ABBREV.get(w['freq_unit'], '?')}",
            w.get("valid_days") or "[dim]all[/dim]", w.get("until_time") or "[dim]—[/dim]",
        )
    Console().print(table)


@window_app.command("remove")
def window_remove(
    ctx: typer.Context,
    window_name: str = typer.Argument(..., help="Name of the window rule to remove."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target."),
) -> None:
    """Remove a throttle window from the Spout."""
    from . import _http

    url, cfg = _resolve(catchment)
    o = ctx.obj
    _http.post(
        f"{url}/api/ponds/{o['pond']}/spouts/{o['spout']}/windows/{window_name}/remove", auth=cfg,
        params=_http.pond_params(major, version), json={},
    )
    typer.echo(f"Window '{window_name}' removed from spout '{o['spout']}'.")


def _control(action: str, pond: str, name: str, catchment, major, version, done: str) -> None:
    from . import _http

    url, cfg = _resolve(catchment)
    _http.post(
        f"{url}/api/ponds/{pond}/spouts/{name}/{action}", auth=cfg,
        params=_http.pond_params(major, version), json={},
    )
    typer.echo(f"Spout '{name}' on '{pond}' {done}.")


def _control_command(action: str, done: str, help_text: str):
    def cmd(
        pond: str = typer.Argument(..., help="The Pond the Spout is on."),
        name: str = typer.Argument(..., help="The Spout's name (see `spout ls`)."),
        catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
        major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
        version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target."),
    ) -> None:
        _control(action, pond, name, catchment, major, version, done)

    cmd.__doc__ = help_text
    return cmd


# A Spout's Control set (its standing Wake). Demand verbs don't apply.
app.command("resync")(_control_command("resync", "will re-egress", "Force a full re-egress (clears watermark + failure)."))
app.command("wake")(_control_command("wake", "armed", "Re-arm the standing Wake (deliver on the next source advance)."))
app.command("force")(_control_command("force", "will re-egress now", "Re-arm and re-deliver the current freshness now."))
app.command("sleep")(_control_command("sleep", "asleep", "Disarm the standing Wake — no new deliveries."))
app.command("kill")(_control_command("kill", "killed", "Disarm and park the Spout until wake/force/clear."))
app.command("clear")(_control_command("clear", "cleared", "Clear a failed/killed Spout."))


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
