from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Optional

import typer


def _topo_sort(nodes: list[str], edges: list[tuple[str, str]]) -> list[str]:
    node_set = set(nodes)
    in_degree = {n: 0 for n in nodes}
    adj: dict[str, list[str]] = defaultdict(list)
    for src, snk in edges:
        if src not in node_set or snk not in node_set:
            continue
        adj[src].append(snk)
        in_degree[snk] += 1
    queue = deque(sorted(n for n in nodes if in_degree[n] == 0))
    result: list[str] = []
    while queue:
        n = queue.popleft()
        result.append(n)
        for m in sorted(adj[n]):
            in_degree[m] -= 1
            if in_degree[m] == 0:
                queue.append(m)
    return result if len(result) == len(nodes) else nodes


def _connected_components(sorted_nodes: list[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    adj: dict[str, set[str]] = defaultdict(set)
    for a, b in edges:
        adj[a].add(b)
        adj[b].add(a)
    visited: set[str] = set()
    components: list[list[str]] = []
    for node in sorted_nodes:
        if node in visited:
            continue
        members: set[str] = set()
        stack = [node]
        while stack:
            n = stack.pop()
            if n in members:
                continue
            members.add(n)
            stack.extend(adj[n] - members)
        visited |= members
        components.append([n for n in sorted_nodes if n in members])
    return components


def _fmt_ts(iso: Optional[str]) -> str:
    """Render a freshness as a compact relative age (e.g. ``3s``, ``5m``, ``2h``, ``4d``), ``now`` at
    the current instant, or a ``-`` prefix when the freshness is in the future ("fresh until", from
    windows). ``—`` when absent."""
    if not iso:
        return "[dim]—[/dim]"
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    sign = "-" if secs < 0 else ""
    s = abs(secs)
    if s < 1:
        return "now"
    if s < 60:
        return f"{sign}{int(s)}s"
    if s < 3600:
        return f"{sign}{int(s // 60)}m"
    if s < 86400:
        return f"{sign}{int(s // 3600)}h"
    return f"{sign}{int(s // 86400)}d"


def _make_table(component_ponds: list[dict]) -> object:
    from rich.table import Table

    _status_fmt = {
        "running": "[bold green]running[/bold green]",
        "queued":  "[yellow]queued[/yellow]",
        "idle":    "[dim]idle[/dim]",
        "failed":  "[bold red]failed[/bold red]",
        "killed":  "[bold red]killed[/bold red]",
        "blocked": "[red dim]blocked[/red dim]",
    }

    table = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 1))
    table.add_column("Pond", style="bold")
    table.add_column("Kind", style="dim")
    table.add_column("Version")
    table.add_column("Status")
    table.add_column("Gen", justify="right")
    table.add_column("Pull", justify="center")
    table.add_column("TargetF")
    table.add_column("StartF")
    table.add_column("EndF")

    for pond in component_ponds:
        status_val = pond.get("status", "")
        status_str = _status_fmt.get(status_val, status_val or "?")
        if status_val == "failed":  # show on-change usage against the budget, e.g. "failed 2/1"
            status_str += f" [dim]{pond.get('failures', 0)}/{pond.get('source_retries', 0)}[/dim]"
        gen = pond.get("gen", 0)
        gen_str = str(gen) if gen else "[dim]—[/dim]"
        pull_str = "[orange3]●[/orange3]" if pond.get("has_pull") else ""
        target = pond.get("target_f")
        target_str = f"[orange3]{_fmt_ts(target)}[/orange3]" if target else "[dim]—[/dim]"

        table.add_row(
            pond.get("name", "?"),
            pond.get("kind", "?"),
            pond.get("version", "?"),
            status_str,
            gen_str,
            pull_str,
            target_str,
            _fmt_ts(pond.get("start_f")),
            _fmt_ts(pond.get("end_f")),
        )

    return table


def _ancestors(name: str, edges: list[tuple[str, str]]) -> set[str]:
    """Return name and all upstream pond names reachable from name (BFS)."""
    upstream: dict[str, list[str]] = defaultdict(list)
    for src, snk in edges:
        upstream[snk].append(src)
    visited: set[str] = {name}
    queue: deque[str] = deque([name])
    while queue:
        n = queue.popleft()
        for src in upstream.get(n, []):
            if src not in visited:
                visited.add(src)
                queue.append(src)
    return visited


def _filter_for_pond(
    ponds: list[dict],
    edges: list[tuple[str, str]],
    pond_name: str,
    major: Optional[int],
    version_str: Optional[str],
) -> tuple[list[dict], list[tuple[str, str]]]:
    """Filter ponds and edges to pond_name and its upstream sources."""
    ancestor_names = _ancestors(pond_name, edges)

    filtered: list[dict] = []
    for p in ponds:
        if p["name"] not in ancestor_names:
            continue
        if p["name"] == pond_name:
            if version_str is not None and p["version"] != version_str:
                continue
            if major is not None and int(p["version"].split(".")[0]) != major:
                continue
        filtered.append(p)

    included = {p["name"] for p in filtered}
    filtered_edges = [e for e in edges if e[0] in included and e[1] in included]
    return filtered, filtered_edges


def _build_renderable(ponds: list[dict], edges: list[tuple[str, str]]) -> object:
    """Build a Rich renderable (Table or Group of Panels) from pond status data."""
    from rich.console import Group
    from rich.panel import Panel

    unique_names = list(dict.fromkeys(p["name"] for p in ponds))
    sorted_names = _topo_sort(unique_names, edges)
    components = _connected_components(sorted_names, edges)

    def _expand(names: list[str]) -> list[dict]:
        by_name: dict[str, list[dict]] = {}
        for p in ponds:
            by_name.setdefault(p["name"], []).append(p)
        result = []
        for name in names:
            result.extend(sorted(by_name.get(name, []), key=lambda p: p["version"]))
        return result

    if len(components) == 1:
        return _make_table(_expand(components[0]))

    panels = []
    for component in components:
        comp_set = set(component)
        sinks_in_comp = {b for a, b in edges if a in comp_set and b in comp_set}
        roots = [n for n in component if n not in sinks_in_comp]
        title = "  →  ".join(roots) if roots else component[0]
        panels.append(Panel(_make_table(_expand(component)), title=title, border_style="dim"))
    return Group(*panels)


def _fetch_status(url: str, all_versions: bool) -> tuple[list[dict], list[tuple[str, str]]]:
    from . import _http

    params = {"all": "true"} if all_versions else {}
    resp = _http.get(f"{url}/api/status", params=params)
    data = resp.json()
    ponds = data.get("ponds", [])
    edges = [tuple(e) for e in data.get("edges", [])]
    return ponds, edges


def _run_live(
    url: str,
    all_versions: bool,
    pond_name: Optional[str],
    major: Optional[int],
    version_str: Optional[str],
    watch: bool,
    until_idle_pond: Optional[str] = None,
) -> None:
    from datetime import datetime, timezone

    from rich.console import Group
    from rich.live import Live
    from rich.text import Text

    def _build() -> tuple[object, bool]:
        try:
            ponds, edges = _fetch_status(url, all_versions)
            if pond_name:
                ponds, edges = _filter_for_pond(ponds, edges, pond_name, major, version_str)
            done = False
            if not ponds:
                body = Text("No active Ponds.", style="dim")
            else:
                body = _build_renderable(ponds, edges)
                if not watch and until_idle_pond:
                    # One-shot trigger (Tap/Pulse): close once the target Pond settles — back to idle,
                    # or stuck failed/blocked (which never returns to idle on its own).
                    tgt = next((p for p in ponds if p["name"] == until_idle_pond), None)
                    done = tgt is not None and tgt.get("status") in ("idle", "failed", "killed", "blocked")
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            footer = " · settled" if done else " · Ctrl+C to stop"
            header = Text(f"Updated {ts}{footer}", style="dim")
            return Group(header, body), done
        except Exception as exc:
            return Text(f"Error fetching status: {exc}", style="red"), False

    try:
        with Live(auto_refresh=False, screen=False) as live:
            while True:
                renderable, done = _build()
                live.update(renderable)
                live.refresh()
                if done:
                    break
                time.sleep(1)
    except KeyboardInterrupt:
        pass


def status(
    pond: Optional[str] = typer.Argument(None, help="Filter to this Pond and its upstream sources."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to query (uses default if omitted)."),
    all: bool = typer.Option(False, "--all", "-a", help="Include all Ponds, not just active ones."),
    major: Optional[int] = typer.Option(
        None, "--major", "-m", help="Major version of the selected Pond (requires pond argument)."
    ),
    version: Optional[str] = typer.Option(
        None, "--version", "-v", help="Specific semver of the selected Pond, e.g. 1.2.3 (requires pond argument)."
    ),
    once: bool = typer.Option(False, "--once", help="Print a single snapshot and exit without live updates."),
    watch: bool = typer.Option(False, "--watch", help="Live updates; never auto-exit even when all Ponds are stopped."),
) -> None:
    """Show Pond activity in the Catchment. Live by default; exits when all Ponds are stopped."""
    from rich.console import Console

    from .config import resolve_catchment

    _, cfg = resolve_catchment(catchment)
    url = cfg["url"]

    if not once:
        _run_live(url, all, pond, major, version, watch=watch)
        return

    ponds, edges = _fetch_status(url, all)

    console = Console()

    if pond is not None:
        pond_names = {p["name"] for p in ponds}
        if pond not in pond_names and not any(True for src, snk in edges if src == pond or snk == pond):
            console.print(f"[red]Pond [bold]{pond}[/bold] not found.[/red]")
            raise typer.Exit(1)
        ponds, edges = _filter_for_pond(ponds, edges, pond, major, version)
        if not ponds:
            console.print(f"[dim]No matching versions for [bold]{pond}[/bold].[/dim]")
            return

    if not ponds:
        console.print(f"[dim]No active Ponds in [bold]{catchment}[/bold].[/dim]")
        return

    console.print(_build_renderable(ponds, edges))
