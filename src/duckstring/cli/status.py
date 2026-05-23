from __future__ import annotations

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
            if pond.get("last_run_status") == "success":
                last_run_str = f"[green]{rel} ✓[/green]"
            else:
                last_run_str = f"[red]{rel} ✗[/red]"
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


def status(
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to query (uses default if omitted)."),
    all: bool = typer.Option(False, "--all", "-a", help="Include all Ponds, not just active ones."),
) -> None:
    """Print a summary of Pond activity in the Catchment."""
    from rich.console import Console
    from rich.panel import Panel

    from . import _http
    from .config import resolve_catchment

    _, cfg = resolve_catchment(catchment)
    url = cfg["url"]

    params = {"all": "true"} if all else {}
    resp = _http.get(f"{url}/api/status", params=params)

    data = resp.json()
    ponds = data.get("ponds", [])

    console = Console()
    if not ponds:
        console.print(f"[dim]No active Ponds in [bold]{catchment}[/bold].[/dim]")
        return

    edges = [tuple(e) for e in data.get("edges", [])]

    # Topo sort on unique pond names — multiple versions sit at the same DAG position.
    unique_names = list(dict.fromkeys(p["name"] for p in ponds))
    sorted_names = _topo_sort(unique_names, edges)
    components = _connected_components(sorted_names, edges)

    # Expand each component's name list to all matching pond dicts (sorted by version).
    def _expand(names: list[str]) -> list[dict]:
        by_name: dict[str, list[dict]] = {}
        for p in ponds:
            by_name.setdefault(p["name"], []).append(p)
        result = []
        for name in names:
            result.extend(sorted(by_name.get(name, []), key=lambda p: p["version"]))
        return result

    if len(components) == 1:
        console.print(_make_table(_expand(components[0])))
    else:
        for component in components:
            comp_set = set(component)
            sinks_in_comp = {b for a, b in edges if a in comp_set and b in comp_set}
            roots = [n for n in component if n not in sinks_in_comp]
            title = "  →  ".join(roots) if roots else component[0]
            console.print(Panel(_make_table(_expand(component)), title=title, border_style="dim"))
