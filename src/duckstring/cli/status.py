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


def _rel_time(dt_str: str) -> str:
    from datetime import datetime, timezone
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    diff = (datetime.now(timezone.utc) - dt).total_seconds()
    if diff < 60:
        return f"{int(diff)}s ago"
    if diff < 3600:
        return f"{int(diff / 60)}m ago"
    if diff < 86400:
        return f"{int(diff / 3600)}h ago"
    return f"{int(diff / 86400)}d ago"


def _make_table(component_ponds: list[dict]) -> object:
    from rich.table import Table

    _status_fmt = {
        "running": "[bold green]running[/bold green]",
        "queued":  "[yellow]queued[/yellow]",
        "failed":  "[bold red]failed[/bold red]",
        "stopped": "[dim]stopped[/dim]",
        "idle":    "[dim]idle[/dim]",
    }

    table = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 1))
    table.add_column("Pond", style="bold")
    table.add_column("Kind", style="dim")
    table.add_column("Version")
    table.add_column("Status")
    table.add_column("Gen", justify="right")
    table.add_column("Last ver", style="dim")
    table.add_column("Last run")

    for pond in component_ponds:
        status_str = _status_fmt.get(pond.get("status", ""), pond.get("status", "?"))

        gen = pond.get("gen", 0)
        gen_str = str(gen) if gen else "[dim]—[/dim]"

        last_run_at = pond.get("last_run_at")
        if last_run_at:
            rel = _rel_time(last_run_at)
            dur = pond.get("last_run_duration")
            dur_str = f" ({dur}s)" if dur is not None else ""
            if pond.get("last_run_status") == "success":
                last_run_str = f"[green]{rel}{dur_str} ✓[/green]"
            else:
                last_run_str = f"[red]{rel}{dur_str} ✗[/red]"
        else:
            last_run_str = "[dim]—[/dim]"

        last_run_version = pond.get("last_run_version") or "—"

        table.add_row(
            pond.get("name", "?"),
            pond.get("kind", "?"),
            pond.get("version", "?"),
            status_str,
            gen_str,
            last_run_version,
            last_run_str,
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


def _run_monitor(
    url: str,
    all_versions: bool,
    pond_name: Optional[str],
    major: Optional[int],
    version_str: Optional[str],
) -> None:
    """Poll status and refresh display in-place until Ctrl+C."""
    from datetime import datetime, timezone

    from rich.console import Group
    from rich.live import Live
    from rich.text import Text

    def _build() -> tuple[object, bool]:
        try:
            ponds, edges = _fetch_status(url, all_versions)
            if pond_name:
                ponds, edges = _filter_for_pond(ponds, edges, pond_name, major, version_str)
            if not ponds:
                body = Text("No active Ponds.", style="dim")
                done = False
            else:
                body = _build_renderable(ponds, edges)
                done = pond_name is not None and all(p.get("status") == "stopped" for p in ponds)
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            footer = " · stopped" if done else " · Ctrl+C to stop"
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
    monitor: bool = typer.Option(False, "--monitor", help="Poll and refresh the display continuously until Ctrl+C."),
) -> None:
    """Print a summary of Pond activity in the Catchment."""
    from rich.console import Console

    from .config import resolve_catchment

    _, cfg = resolve_catchment(catchment)
    url = cfg["url"]

    if monitor:
        _run_monitor(url, all, pond, major, version)
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
