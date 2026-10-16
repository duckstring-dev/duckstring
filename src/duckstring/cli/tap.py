"""`duckstring tap add|ls|rm|rotate POND`: manage a Pond's Taps.

A Tap is an access point for an app: its own key, which reads only the Tap's tables, and a freshness policy.
A query through it sends the Pond a Refresh when the newest request, or the data, is older than
``--fresh-within``, so the data keeps up while the app is in use and the pipeline goes quiet when it isn't::

    duckstring tap add reports --name dashboard --fresh-within 5m
    duckstring tap ls reports
    duckstring tap rotate reports dashboard
    duckstring tap rm reports dashboard
"""

from __future__ import annotations

from typing import Optional

import typer

app = typer.Typer(help="Give apps an access point that keeps a Pond fresh while it's used.", no_args_is_help=True)

_CATCHMENT = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted).")
_MAJOR = typer.Option(None, "--major", "-m", help="Major version to target (default: latest).")
_VERSION = typer.Option(None, "--version", "-v", help="Specific semver to target.")


def _resolve(catchment: Optional[str]) -> tuple[str, dict]:
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    return cfg["url"], cfg


def _seconds(text: str) -> int:
    from .window import _parse_duration

    return 0 if text.strip() == "0" else _parse_duration(text)


def _print_key(key: str, pond: str, name: str) -> None:
    typer.echo(f"Key for Tap '{name}' on '{pond}' (shown once, so store it now):")
    typer.echo(f"  {key}")


@app.command("add")
def add(
    pond: str = typer.Argument(..., help="The Pond the app reads, usually an Outlet."),
    name: str = typer.Option(..., "--name", "-n", help="The Tap's name: letters, digits, '.', '_' and '-', up to 64 "
                                                       "characters."),
    fresh_within: str = typer.Option(
        "0", "--fresh-within", "-f",
        help="Send a Refresh on a query when the newest request on the Pond, or the data, is at least this old. "
             "0 refreshes on every query, except while a request is already waiting for the Pond to start a run."),
    wait: Optional[str] = typer.Option(
        None, "--wait", "-w",
        help="Hold a query up to this long until the data is fresh within --fresh-within, then answer it with what "
             "has been published. Without it a query is answered at once."),
    tables: Optional[str] = typer.Option(
        None, "--tables", "-t", help="Comma-separated tables the Tap can read (default: all exposed tables)."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
) -> None:
    """Add a Tap and print its key. The key is shown once; only a hash of it is kept."""
    from . import _http

    body = {
        "name": name,
        "fresh_within_ms": _seconds(fresh_within) * 1000,
        "wait_ms": _seconds(wait) * 1000 if wait else None,
        "tables": [t.strip() for t in tables.split(",") if t.strip()] if tables else None,
    }
    if wait and body["wait_ms"] <= 0:
        raise typer.BadParameter("--wait needs a positive timeout, e.g. 30s")
    url, cfg = _resolve(catchment)
    resp = _http.post(f"{url}/api/ponds/{pond}/taps", auth=cfg, params=_http.pond_params(major, version),
                      json=body).json()
    typer.echo(f"Tap '{name}' added on '{resp['pond']}'.")
    _print_key(resp["key"], resp["pond"], name)
    if resp.get("warning"):
        typer.secho(f"Warning: {resp['warning']}", fg=typer.colors.YELLOW)


@app.command("ls")
def ls(
    pond: str = typer.Argument(..., help="The Pond whose Taps to list."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
) -> None:
    """List the Pond's Taps with their tables, policy, last query, and counts of queries and Refreshes."""
    from rich.console import Console
    from rich.table import Table

    from . import _http
    from .status import _fmt_ts
    from .window import _fmt_duration

    url, cfg = _resolve(catchment)
    taps = _http.get(f"{url}/api/ponds/{pond}/taps", auth=cfg,
                     params=_http.pond_params(major, version)).json().get("taps", [])
    if not taps:
        typer.echo("No Taps.")
        return
    table = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 1))
    for col in ("Name", "Tables", "Fresh within", "Wait", "Last query", "Queries", "Refreshes"):
        table.add_column(col)
    for t in taps:
        fresh = t["fresh_within_ms"] // 1000
        table.add_row(
            t["name"], ", ".join(t["tables"]) if t.get("tables") else "[dim]all exposed[/dim]",
            _fmt_duration(fresh) if fresh else "every query",
            _fmt_duration(t["wait_ms"] // 1000) if t.get("wait_ms") else "[dim]no[/dim]",
            f"{_fmt_ts(t['last_query_at'])} ago" if t.get("last_query_at") else "[dim]never[/dim]",
            str(t.get("query_count", 0)), str(t.get("refresh_count", 0)),
        )
    Console().print(table)


@app.command("rm")
def rm(
    pond: str = typer.Argument(..., help="The Pond the Tap is on."),
    name: str = typer.Argument(..., help="The Tap's name (see `tap ls`)."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
) -> None:
    """Remove the Tap. Its key stops working at once, and runs it already caused finish."""
    from . import _http

    url, cfg = _resolve(catchment)
    _http.post(f"{url}/api/ponds/{pond}/taps/{name}/remove", auth=cfg, params=_http.pond_params(major, version),
               json={})
    typer.echo(f"Tap '{name}' removed from '{pond}'.")


@app.command("rotate")
def rotate(
    pond: str = typer.Argument(..., help="The Pond the Tap is on."),
    name: str = typer.Argument(..., help="The Tap's name (see `tap ls`)."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
) -> None:
    """Print a new key for the Tap. The old key stops working at once."""
    from . import _http

    url, cfg = _resolve(catchment)
    resp = _http.post(f"{url}/api/ponds/{pond}/taps/{name}/rotate", auth=cfg,
                      params=_http.pond_params(major, version), json={}).json()
    _print_key(resp["key"], resp["pond"], name)
