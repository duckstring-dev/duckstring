from __future__ import annotations

from collections import defaultdict, deque


def topo_sort(nodes: list[str], edges: list[tuple[str, str]]) -> list[str]:
    """Kahn's algorithm. Raises ValueError naming cycle members if a cycle exists.

    edges: (upstream, downstream) pairs — upstream appears earlier in the result.
    """
    node_set = set(nodes)
    in_degree: dict[str, int] = {n: 0 for n in nodes}
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

    if len(result) != len(nodes):
        cycle_members = sorted(n for n in nodes if n not in set(result))
        raise ValueError(f"Cycle detected among: {', '.join(cycle_members)}")
    return result


def connected_components(sorted_nodes: list[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    """Partition sorted_nodes into connected components (undirected).

    Each component preserves the topo order from sorted_nodes.
    """
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


def assert_no_cycles(db) -> None:
    """Query the selected inter-pond graph and raise ValueError if a cycle exists."""
    nodes = [
        r[0] for r in db.execute(
            "SELECT pn.name FROM pond p JOIN pond_name pn ON pn.id = p.pond_name_id"
        ).fetchall()
    ]
    edges = [
        (r[0], r[1]) for r in db.execute("""
            SELECT src.name, snk.name
            FROM pond_to_pond e
            JOIN pond p ON p.id = e.pond_id
            JOIN pond_name snk ON snk.id = p.pond_name_id
            JOIN pond_name src ON src.id = e.source_pond_name_id
        """).fetchall()
    ]
    topo_sort(nodes, edges)
