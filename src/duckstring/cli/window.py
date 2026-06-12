"""`duckstring trigger window {pond} add|list|remove` — manage batch-availability windows on a Pond.

Windows describe when an Inlet's source data is available (an RFC-5545-flavoured recurrence). The
``pond`` argument comes right after ``window`` (a group callback), e.g.::

    duckstring trigger window transactions add -n nightly -s 02:00 -d 3h -e 1d
    duckstring trigger window transactions list
    duckstring trigger window transactions remove nightly
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

import typer

app = typer.Typer(help="Manage batch-availability windows on a Pond.", no_args_is_help=True)

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_EVERY_UNIT = {"s": "SECOND", "m": "MINUTE", "h": "HOUR", "d": "DAY", "w": "WEEK"}
_UNIT_ABBREV = {"SECOND": "s", "MINUTE": "m", "HOUR": "h", "DAY": "d", "WEEK": "w"}
_WEEKDAYS = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}


def _parse_duration(text: str) -> int:
    """Total seconds from a (possibly combined) duration string like '3h', '45m', '1h30m'."""
    s = text.strip().lower()
    parts = re.findall(r"(\d+)([smhdw])", s)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != s:
        raise typer.BadParameter(f"Invalid duration '{text}' — use e.g. 3h, 45m, 15s, 1h30m")
    return sum(int(n) * _UNIT_SECONDS[u] for n, u in parts)


def _parse_every(text: str) -> tuple[str, int]:
    """A single-unit interval like '1d', '12h', '10s' → (FREQ_UNIT, interval)."""
    m = re.fullmatch(r"(\d+)([smhdw])", text.strip().lower())
    if not m:
        raise typer.BadParameter(f"Invalid interval '{text}' — single unit, e.g. 1d, 12h, 10s")
    return _EVERY_UNIT[m.group(2)], int(m.group(1))


def _parse_dt(text: str, *, allow_hhmm: bool) -> str:
    """ISO-8601 (or, if allowed, HH:MM anchored to today UTC) → ISO-8601 UTC string."""
    text = text.strip()
    if allow_hhmm:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
        if m:
            now = datetime.now(timezone.utc)
            return now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0).isoformat()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        hint = "ISO 8601" + (" or HH:MM" if allow_hhmm else "")
        raise typer.BadParameter(f"Invalid datetime '{text}' — use {hint}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _parse_days(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    days = [d.strip().upper() for d in text.split(",") if d.strip()]
    bad = [d for d in days if d not in _WEEKDAYS]
    if bad:
        raise typer.BadParameter(f"Invalid weekday(s): {', '.join(bad)} — use MON..SUN")
    return ",".join(days)


def _fmt_duration(seconds: int) -> str:
    for abbrev, unit in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= unit and seconds % unit == 0:
            return f"{seconds // unit}{abbrev}"
    return f"{seconds}s"


def _resolve(catchment: Optional[str]) -> tuple[str, dict]:
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    return cfg["url"], cfg


@app.callback()
def _main(ctx: typer.Context, pond: str = typer.Argument(..., help="The Pond whose windows to manage.")):
    ctx.obj = {"pond": pond}


@app.command("add")
def add(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", "-n", help="Unique identifier for the window rule."),
    every: str = typer.Option(..., "--every", "-e", help="Recurrence interval (single unit), e.g. 1d, 12h, 10s."),
    start: Optional[str] = typer.Option(None, "--start", "-s", help="Window start (ISO 8601 or HH:MM); default 00:00 today."),
    duration: Optional[str] = typer.Option(None, "--duration", "-d", help="Window length; default = --every (back-to-back)."),
    on: Optional[str] = typer.Option(None, "--on", "-o", help="Restrict to weekdays, e.g. MON,WED,FRI."),
    until: Optional[str] = typer.Option(None, "--until", "-u", help="Expiration (ISO 8601)."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target."),
) -> None:
    """Add a recurring batch-availability window to the Pond."""
    from . import _http

    unit, interval = _parse_every(every)
    payload = {
        "name": name,
        "start_anchor": _parse_dt(start or "00:00", allow_hhmm=True),
        "duration_seconds": _parse_duration(duration) if duration else _parse_duration(every),
        "freq_unit": unit,
        "freq_interval": interval,
        "valid_days": _parse_days(on),
        "until_time": _parse_dt(until, allow_hhmm=False) if until else None,
    }
    url, cfg = _resolve(catchment)
    _http.post(
        f"{url}/api/ponds/{ctx.obj['pond']}/windows", auth=cfg,
        params=_http.pond_params(major, version), json=payload,
    )
    typer.echo(f"Window '{name}' added.")


@app.command("list")
def list_(
    ctx: typer.Context,
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target."),
) -> None:
    """List the Pond's windows."""
    from rich.console import Console
    from rich.table import Table

    from . import _http

    url, cfg = _resolve(catchment)
    windows = _http.get(
        f"{url}/api/ponds/{ctx.obj['pond']}/windows", auth=cfg,
        params=_http.pond_params(major, version),
    ).json().get("windows", [])
    if not windows:
        typer.echo("No windows.")
        return

    table = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 1))
    for col in ("Name", "Start", "Duration", "Every", "Days", "Until"):
        table.add_column(col)
    for w in windows:
        every = f"{w['freq_interval']}{_UNIT_ABBREV.get(w['freq_unit'], '?')}"
        table.add_row(
            w["name"],
            w["start_anchor"],
            _fmt_duration(w["duration_seconds"]),
            every,
            w.get("valid_days") or "[dim]all[/dim]",
            w.get("until_time") or "[dim]—[/dim]",
        )
    Console().print(table)


@app.command("remove")
def remove(
    ctx: typer.Context,
    window_name: str = typer.Argument(..., help="Name of the window rule to remove."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver to target."),
) -> None:
    """Remove a window from the Pond."""
    from . import _http

    url, cfg = _resolve(catchment)
    _http.post(
        f"{url}/api/ponds/{ctx.obj['pond']}/windows/{window_name}/remove", auth=cfg,
        params=_http.pond_params(major, version), json={},
    )
    typer.echo(f"Window '{window_name}' removed.")
