"""`duckstring runs [POND]`: recent Pond Runs, newest first, with their Ripple Runs on request."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import typer


def _duration(start: Optional[str], end: Optional[str]) -> str:
    if not start or not end:
        return ""
    seconds = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}m{secs:02d}s" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}m"


def _when(iso: Optional[str]) -> str:
    if not iso:
        return ""
    from datetime import timezone

    return datetime.fromisoformat(iso).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _first_line(text: Optional[str], width: int = 100) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= width else line[: width - 3] + "..."


def _attach_reports(cfg: dict, history: list[dict], limit: int) -> None:
    """Add each Run's own report (``reports``, or None) from its Pond's reports route, one request per Pond line."""
    from . import _http

    by_line: dict[tuple, dict] = {}
    for r in history:
        line = (r.get("pond"), r.get("major"))
        if line[0] is None or line in by_line:
            continue
        got = _http.get(f"{cfg['url']}/api/ponds/{line[0]}/reports", auth=cfg,
                        params={"major": line[1], "limit": max(limit, 50)}).json().get("reports", [])
        by_line[line] = {datetime.fromisoformat(x["f"]): x["reports"] for x in got}
    for r in history:
        found = by_line.get((r.get("pond"), r.get("major")), {})
        r["reports"] = found.get(datetime.fromisoformat(r["f"])) if r.get("f") else None


def runs(
    pond: Optional[str] = typer.Argument(None, help="Only this Pond's Runs (default: every Pond)."),
    upstream: bool = typer.Option(False, "--upstream", help="With POND: also the Runs of the Ponds upstream of it."),
    ripples: bool = typer.Option(False, "--ripples", help="List each Run's Ripple Runs, with retries and errors."),
    reports: bool = typer.Option(False, "--reports", help="Show what each Run reported with pond.report()."),
    limit: int = typer.Option(20, "--limit", "-n", help="How many Pond Runs, newest first (at most 1000)."),
    as_json: bool = typer.Option(False, "--json", help="Print the Runs as JSON, as /api/runs gives them."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="With POND: the major line (default: latest)."),
) -> None:
    """Show recent Pond Runs, newest first: version, freshness, status, start, duration and error.

    A superseded Run never finished because a later Run did its work; its detail names that Run."""
    from . import _http
    from .config import resolve_catchment

    _, cfg = resolve_catchment(catchment)
    params: dict = {"limit": limit, "ripples": "true" if ripples else "false",
                    "lineage": "true" if upstream else "false"}
    if pond is not None:
        params["pond"] = pond
        if major is not None:
            params["major"] = major
    history = _http.get(f"{cfg['url']}/api/runs", auth=cfg, params=params).json().get("runs", [])
    if reports:
        _attach_reports(cfg, history, limit)
    if as_json:
        import json

        typer.echo(json.dumps(history, indent=2))
        return
    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table

    from .wait import interactive

    # Without a terminal Rich assumes 80 columns and cuts the Error column to "..."; a script or agent wants it.
    console = Console() if interactive() else Console(width=240)
    if not history:
        console.print("[dim]No Runs.[/dim]")
        return
    colour = {"success": "green", "failed": "red", "killed": "red", "running": "cyan", "superseded": "dim"}
    table = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 1))
    for col in ("Pond", "Version", "Freshness (UTC)", "Status", "Started (UTC)", "Took", "Detail"):
        table.add_column(col, no_wrap=col != "Detail")
    for r in history:
        st = r.get("status") or ""
        if st == "superseded" and r.get("superseded_by"):
            detail = f"[dim]superseded by the Run at {_when(r['superseded_by'])}[/dim]"
        else:
            detail = f"[red]{escape(_first_line(r.get('error')))}[/red]" if r.get("error") else ""
        table.add_row(r["id"], r.get("version") or "", _when(r.get("f")), f"[{colour.get(st, 'white')}]{st}[/]",
                      _when(r.get("started_at")), _duration(r.get("started_at"), r.get("finished_at")), detail)
        if r.get("reports"):
            facts = ", ".join(f"{k}={v}" for k, v in r["reports"].items())
            table.add_row("  reports", "", "", "", "", "", f"[dim]{escape(facts)}[/dim]")
        for rr in r.get("ripples") or []:
            rst = rr.get("status") or ""
            retry = f" (retry {rr['retry']})" if rr.get("retry") else ""
            table.add_row(f"  {rr['ripple']}{retry}", "", "", f"[{colour.get(rst, 'white')}]{rst}[/]",
                          _when(rr.get("started_at")), _duration(rr.get("started_at"), rr.get("finished_at")),
                          escape(_first_line(rr.get("error"))))
    console.print(table)
