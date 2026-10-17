"""`duckstring refresh now|add|ls|rm POND` and `duckstring refresh tap …`: ask for fresh data, now, on a
cadence, or whenever the Pond is queried.

A Refresh asks for a Pond's data to be at least as fresh as now: every older Pond upstream runs and the result
flows down. A recurring Refresh repeats it::

    duckstring refresh now reports
    duckstring refresh add reports --every 30m                    # 30 min after the last request or refresh
    duckstring refresh add reports --every 1d --start 06:00 --on MON,TUE,WED,THU,FRI
    duckstring refresh add reports --continuous                   # as fresh as the pipeline allows
    duckstring refresh ls reports
    duckstring refresh rm reports "Every 30 min"
    duckstring refresh tap add reports --name dashboard   # refresh when an app queries it (cli/tap.py)
"""

from __future__ import annotations

from typing import Optional

import typer

_SILENT_HELP = "Send the request without opening the live status view."
_WATCH_HELP = "Keep the status view open after the Pond settles."
_CATCHMENT = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted).")
_MAJOR = typer.Option(None, "--major", "-m", help="Major version to target (default: latest).")
_VERSION = typer.Option(None, "--version", "-v", help="Specific semver to target, e.g. 1.2.3.")


app = typer.Typer(help="Request for a Pond's data to be refreshed.", no_args_is_help=True)


def _resolve(catchment: Optional[str]) -> tuple[str, dict]:
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    return cfg["url"], cfg


def send_and_watch(cfg: dict, pond: str, major: Optional[int], version: Optional[str], silent: bool, watch: bool,
                   endpoint: str, payload: dict, success_msg: str, one_shot: bool) -> None:
    """POST to a Pond route, then (unless ``silent``) open the live status view on it: a one-shot request
    closes once the Pond settles, a recurring one stays open until Ctrl+C."""
    from . import _http

    url = cfg["url"]
    _http.post(f"{url}/api/ponds/{pond}/{endpoint}", auth=cfg, params=_http.pond_params(major, version), json=payload)
    if silent:
        typer.echo(success_msg)
        return
    from .status import _run_live
    stay = watch or not one_shot
    _run_live(url, auth=cfg, pond_name=pond, major=major, version_str=version,
              watch=stay, until_idle_pond=None if stay else pond)


@app.command("now")
def now(
    pond: str = typer.Argument(..., help="The Pond to refresh, usually an Outlet."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
    silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
    watch: bool = typer.Option(False, "--watch", help=_WATCH_HELP),
) -> None:
    """Ask for the Pond's data to be at least as fresh as now.

    Every older Pond upstream runs (an optional Source only if it has never run), and the result flows down
    to this Pond.
    """
    _, cfg = _resolve(catchment)
    send_and_watch(cfg, pond, major, version, silent, watch, "refresh", {}, "Refresh sent.", one_shot=True)


def add_payload(every: Optional[str], continuous: bool, start: Optional[str], on: Optional[str],
                until: Optional[str], name: Optional[str]) -> dict:
    """The request body for a recurring Refresh, validated on the client."""
    from .window import _parse_days, _parse_dt, _parse_duration

    if continuous and every and _parse_duration(every) != 0:
        raise typer.BadParameter("--continuous is --every 0s; give one or the other")
    if not continuous and every is None:
        raise typer.BadParameter("give --every (e.g. 30m, 1d) or --continuous")
    seconds = 0 if continuous else _parse_duration(every)
    if start and seconds == 0:
        raise typer.BadParameter("a scheduled Refresh (--start) needs a period (--every)")
    if (on or until) and not start:
        raise typer.BadParameter("--on and --until apply to a scheduled Refresh: add --start")
    return {
        "every_seconds": seconds,
        "start": _parse_dt(start, allow_hhmm=True) if start else None,
        "days": _parse_days(on),
        "until": _parse_dt(until, allow_hhmm=False) if until else None,
        "name": name,
    }


@app.command("add")
def add(
    pond: str = typer.Argument(..., help="The Pond to keep fresh, usually an Outlet."),
    every: Optional[str] = typer.Option(None, "--every", "-e", help="How often, e.g. 30s, 30m, 12h, 1d or 1h30m. "
                                                                    "0s is continuous."),
    continuous: bool = typer.Option(False, "--continuous", help="As fresh as the pipeline allows (= --every 0s)."),
    start: Optional[str] = typer.Option(None, "--start", "-s", help="Pin to a schedule from this time: HH:MM (UTC, "
                                                                    "today) or ISO 8601."),
    on: Optional[str] = typer.Option(None, "--on", "-o", help="With --start: only on these weekdays, e.g. "
                                                              "MON,TUE,WED,THU,FRI."),
    until: Optional[str] = typer.Option(None, "--until", "-u", help="With --start: when the schedule ends (ISO 8601)."),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="A name, unique on this Pond. Defaults to one "
                                                                  "describing it, e.g. 'Every 30 min'."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
    silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
) -> None:
    """Add a recurring Refresh on a schedule or a frequency.

    Without --start, a Refresh is due EVERY after the later of the last request and the last refresh of the
    data, so the data can be up to EVERY plus the pipeline's run time old. A Pond has one such Refresh: adding
    another replaces it. With --start, it fires on that schedule whatever else refreshes the Pond, and a Pond
    can have several.
    """
    payload = add_payload(every, continuous, start, on, until, name)
    _, cfg = _resolve(catchment)
    if silent:
        from . import _http

        resp = _http.post(f"{cfg['url']}/api/ponds/{pond}/refreshes", auth=cfg,
                          params=_http.pond_params(major, version), json=payload).json()
        typer.echo(f"Refresh '{resp['refresh']['name']}' added on '{pond}'.")
        return
    send_and_watch(cfg, pond, major, version, False, False, "refreshes", payload, "", one_shot=False)


@app.command("ls")
def ls(
    pond: str = typer.Argument(..., help="The Pond whose recurring Refreshes to list."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
) -> None:
    """List the Pond's recurring Refreshes with their details."""
    from rich.console import Console
    from rich.table import Table

    from . import _http
    from .window import _fmt_duration

    url, cfg = _resolve(catchment)
    refreshes = _http.get(f"{url}/api/ponds/{pond}/refreshes", auth=cfg,
                          params=_http.pond_params(major, version)).json().get("refreshes", [])
    if not refreshes:
        typer.echo("No recurring Refreshes.")
        return
    table = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 1))
    for col in ("Name", "Every", "Start (UTC)", "Days", "Until (UTC)", "Next (UTC)"):
        table.add_column(col, no_wrap=col == "Name")
    dash = "[dim]—[/dim]"
    for r in refreshes:
        every = r["every_ms"] / 1000
        table.add_row(
            r["name"],
            "continuous" if r.get("continuous") else (_fmt_duration(int(every)) if every.is_integer() else f"{every:g}s"),
            _short(r.get("start")) or dash,
            r.get("days") or ("[dim]all[/dim]" if r.get("start") else dash),
            _short(r.get("until")) or dash,
            _short(r.get("next_due")) or ("[dim]as often as it can[/dim]" if r.get("continuous") else dash),
        )
    Console().print(table)


def _short(iso: Optional[str]) -> Optional[str]:
    """An ISO timestamp as ``YYYY-MM-DD HH:MM`` in UTC."""
    if not iso:
        return None
    from datetime import datetime, timezone

    return datetime.fromisoformat(iso).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


@app.command("rm")
def rm(
    pond: str = typer.Argument(..., help="The Pond whose Refresh to remove."),
    name: str = typer.Argument(..., help="The Refresh's name (see `refresh ls`)."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
) -> None:
    """Remove a recurring Refresh (runs in progress still finish)."""
    from . import _http

    url, cfg = _resolve(catchment)
    _http.post(f"{url}/api/ponds/{pond}/refreshes/{name}/remove", auth=cfg,
               params=_http.pond_params(major, version), json={})
    typer.echo(f"Refresh '{name}' removed from '{pond}'.")


def _register_taps() -> None:
    from . import tap

    app.add_typer(tap.app, name="tap")


_register_taps()
