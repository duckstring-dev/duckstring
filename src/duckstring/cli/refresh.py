"""`duckstring refresh now|add|ls|rm POND` and `duckstring refresh tap …`: ask for fresh data, now, on a
cadence, or whenever the Pond is queried.

A Refresh asks for a Pond's data to be at least as fresh as now: every older Pond upstream runs and the result
flows down. A recurring Refresh repeats it::

    duckstring refresh now reports
    duckstring refresh now reports --fresh-within 1d              # only what is more than a day old runs
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

from .wait import TIMEOUT_HELP, VIEW_HELP, WAIT_HELP

_SILENT_HELP = "Send the request and return, without following it."
_WATCH_HELP = "Keep the status view open after the Pond settles."
_CATCHMENT = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted).")
_MAJOR = typer.Option(None, "--major", "-m", help="Major version to target (default: latest).")
_VERSION = typer.Option(None, "--version", "-v", help="Specific semver to target, e.g. 1.2.3.")
_PUDDLE = "Move the Pond's Puddle Sources, then refresh: "


app = typer.Typer(help="Request for a Pond's data to be refreshed.", no_args_is_help=True)


def _resolve(catchment: Optional[str]) -> tuple[str, dict]:
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    return cfg["url"], cfg


def send_and_watch(cfg: dict, pond: str, major: Optional[int], version: Optional[str], silent: bool, watch: bool,
                   endpoint: str, payload: dict, success_msg: str, one_shot: bool, params: Optional[dict] = None,
                   wait: bool = False, timeout: Optional[str] = None, view: bool = False, goal=None) -> None:
    """POST to a Pond route, then (unless ``silent``) follow it. A person gets the live status view: a one-shot
    request closes it once the Pond settles, a recurring one keeps it open until Ctrl+C. A one-shot request
    waits for its outcome instead with ``wait``, or by default without a terminal (``cli/wait.py``), where a
    live view only fills a log; ``view`` keeps the view anyway. ``goal(response, entry)`` says when the target
    has done what was asked (default: once it settles)."""
    from . import _http
    from .wait import Waiter, interactive, parse_timeout

    url = cfg["url"]
    waiting = not silent and one_shot and (wait or (not watch and not view and not interactive()))
    seconds = parse_timeout(timeout)
    waiter = Waiter(cfg, pond, major, version) if waiting else None
    resp = _http.post(f"{url}/api/ponds/{pond}/{endpoint}", auth=cfg,
                      params={**_http.pond_params(major, version), **(params or {})}, json=payload)
    if waiter is not None:
        body = resp.json() if resp.content else {}
        what = {"refresh": "Refresh", "wake": "Wake", "force": "Force"}.get(endpoint, endpoint)
        waiter.wait((lambda e: goal(body, e)) if goal is not None else (lambda e: True), seconds, what)
        return
    if silent or (not one_shot and not view and not interactive()):
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
    fresh_within: Optional[str] = typer.Option(
        None, "--fresh-within", "-f",
        help="Ask for data no older than this (e.g. 1h or 1d) instead of as fresh as now: only Ponds older than "
             "this run."),
    next_change: bool = typer.Option(False, "--next-change", help=_PUDDLE + "to their next change in production."),
    next_run: bool = typer.Option(False, "--next-run", help=_PUDDLE + "to this Pond's next Run in production."),
    most_recent: bool = typer.Option(False, "--most-recent", help=_PUDDLE + "as far as all of them have got."),
    to: Optional[str] = typer.Option(None, "--to", help=_PUDDLE + "to this freshness (ISO 8601)."),
    hydrate: bool = typer.Option(False, "--hydrate", help="Take a new Puddle baseline from --from and run the "
                                                          "Ponds this Refresh reaches from it."),
    live: bool = typer.Option(False, "--live", help="With --hydrate: read each Puddle Source through production's "
                                                    "query API, copying nothing."),
    as_at: Optional[str] = typer.Option(None, "--as-at", help="With --hydrate: the baseline as of this time "
                                                              "(ISO 8601; default now)."),
    from_: Optional[str] = typer.Option(None, "--from", help="With --hydrate: the registered Catchment the Puddle "
                                                             "Sources live on."),
    rewind: bool = typer.Option(False, "--rewind", help="Go back to the Puddle baseline and run from it."),
    silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
    watch: bool = typer.Option(False, "--watch", help=_WATCH_HELP),
    wait: bool = typer.Option(False, "--wait", help=WAIT_HELP),
    timeout: Optional[str] = typer.Option(None, "--timeout", help=TIMEOUT_HELP),
    view: bool = typer.Option(False, "--view", help=VIEW_HELP),
) -> None:
    """Ask for the Pond's data to be at least as fresh as now.

    Every older Pond upstream runs (an optional Source only if it has never run), and the result flows down
    to this Pond. With --fresh-within, Ponds whose data is already that fresh don't run.

    On a Catchment where the Pond reads Sources as Puddles, a Refresh at now is refused: take a baseline with
    --hydrate --live --from CATCHMENT, then move through production's history with --next-change,
    --next-run, --most-recent or --to. --rewind goes back to the baseline. A target after --hydrate or --rewind
    runs once the baseline Run has finished.
    """
    targets = [t for t in (next_change and "next-change", next_run and "next-run", most_recent and "most-recent",
                           to and "to") if t]
    if len(targets) > 1:
        raise typer.BadParameter("give one target: --next-change, --next-run, --most-recent or --to")
    if hydrate and rewind:
        raise typer.BadParameter("give --hydrate or --rewind, not both")
    if not hydrate and (live or as_at or from_):
        raise typer.BadParameter("--live, --as-at and --from go with --hydrate")
    if hydrate and not from_:
        raise typer.BadParameter("--hydrate needs --from CATCHMENT: the Catchment the Puddle Sources live on")
    if fresh_within is not None and (targets or hydrate or rewind):
        raise typer.BadParameter("--fresh-within is for a Refresh at now")
    _, cfg = _resolve(catchment)
    if not (targets or hydrate or rewind):
        params = {}
        if fresh_within is not None:
            from .tap import _seconds
            params["fresh_within_ms"] = _seconds(fresh_within) * 1000
        send_and_watch(cfg, pond, major, version, silent, watch, "refresh", {}, "Refresh sent.", one_shot=True,
                       params=params, wait=wait, timeout=timeout, view=view, goal=reached_request)
        return
    if hydrate or rewind:
        if hydrate:
            from ..core import resolve_catchment_auth

            try:
                url, headers = resolve_catchment_auth(from_)
            except ValueError as exc:
                raise typer.BadParameter(str(exc)) from None
            payload = {"origin": {"name": from_, "url": url, "headers": headers}, "live": live,
                       "as_at": _iso_point(as_at, "--as-at")}
            endpoint, msg = "puddles/hydrate", "Hydrated; the baseline Run is on its way."
        else:
            payload, endpoint, msg = {}, "puddles/rewind", "Rewound; the baseline Run is on its way."
        # Followed by a target: that moves the Puddles on from the baseline, so wait for the baseline Run first.
        send_and_watch(cfg, pond, major, version, silent and not targets, watch and not targets, endpoint, payload,
                       msg, one_shot=True, wait=wait or bool(targets), timeout=timeout, view=view and not targets,
                       goal=reached_request)
        if not targets:
            return
    body = {"to": _iso_point(to, "--to")} if to else {"target": targets[0]}
    send_and_watch(cfg, pond, major, version, silent, watch, "puddles/refresh", body, "Refresh sent.",
                   one_shot=True, wait=wait, timeout=timeout, view=view, goal=reached_request)


def _iso_point(value: Optional[str], label: str) -> Optional[str]:
    if value is None:
        return None
    from datetime import datetime, timezone

    try:
        f = datetime.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter(f"{label}: expected an ISO 8601 time, like 2026-11-20T06:00") from None
    return (f if f.tzinfo is not None else f.replace(tzinfo=timezone.utc)).isoformat()


def reached_request(body: dict, entry: dict) -> bool:
    """Whether a Refresh's target is at least as fresh as the request asked (the route returns it)."""
    from datetime import datetime

    asked, end = body.get("requested"), entry.get("end_f")
    return asked is None or (end is not None and datetime.fromisoformat(end) >= datetime.fromisoformat(asked))


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
    view: bool = typer.Option(False, "--view", help=VIEW_HELP),
) -> None:
    """Add a recurring Refresh on a schedule or a frequency.

    Without --start, a Refresh is due EVERY after the later of the last request and the last refresh of the
    data, so the data can be up to EVERY plus the pipeline's run time old. A Pond has one such Refresh: adding
    another replaces it. With --start, it fires on that schedule whatever else refreshes the Pond, and a Pond
    can have several.
    """
    from .wait import interactive

    payload = add_payload(every, continuous, start, on, until, name)
    _, cfg = _resolve(catchment)
    if silent or not (view or interactive()):
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
    as_json: bool = typer.Option(False, "--json", help="Print the Refreshes as JSON."),
) -> None:
    """List the Pond's recurring Refreshes with their details."""
    from rich.console import Console
    from rich.table import Table

    from . import _http
    from .window import _fmt_duration

    url, cfg = _resolve(catchment)
    refreshes = _http.get(f"{url}/api/ponds/{pond}/refreshes", auth=cfg,
                          params=_http.pond_params(major, version)).json().get("refreshes", [])
    if as_json:
        import json

        typer.echo(json.dumps(refreshes, indent=2))
        return
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
