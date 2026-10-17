"""`duckstring data lineage [POND[.TABLE]]` and `duckstring data trace POND.TABLE`: the observed table-level
lineage (plans/lineage.md), and the run that produced some rows.

Prints the tables each Ripple actually read and wrote on its latest recorded run — recorded by the Pond
handle at the moment of the call, so it is exact (never inferred) and per-run rather than static::

    duckstring data lineage                     # every pond with recorded lineage
    duckstring data lineage sales               # one pond's ripples (reads → writes)
    duckstring data lineage sales.sale_line     # only the ripples touching that table
"""

from __future__ import annotations

from typing import Optional

import typer


def show(
    ref: Optional[str] = typer.Argument(None, metavar="[POND[.TABLE]]", help="A Pond, or one of its tables (default: "
                                                                            "every Pond with recorded lineage)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    columns: bool = typer.Option(False, "--columns", help="Also show which source columns each output "
                                                          "column is derived from."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
) -> None:
    """Show the tables each Ripple actually read and wrote. With a table, only the Ripples that read or wrote it."""
    from rich.console import Console
    from rich.tree import Tree

    from duckstring.core import parse_ref

    from . import _http
    from .config import resolve_catchment

    pond = table = None
    if ref:
        try:
            first, rest = parse_ref(ref)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
        pond, table = (rest, None) if first is None else (first, rest)
    _, cfg = resolve_catchment(catchment)
    params = [("pond", pond), ("major", major), ("table", table), ("columns", "true" if columns else None)]
    query = "&".join(f"{k}={v}" for k, v in params if v is not None)
    data = _http.get(f"{cfg['url']}/api/lineage{'?' + query if query else ''}", auth=cfg).json()

    ponds = data.get("ponds", [])
    if not ponds or all(not p["ripples"] and not p.get("columns") for p in ponds):
        typer.echo("No recorded lineage yet — lineage is observed as Ripples run.")
        return
    console = Console()
    for p in ponds:
        if not p["ripples"] and not p.get("columns"):
            continue
        tree = Tree(f"[bold]{p['name']}[/bold]@{p['major']} [dim]{p['version']}[/dim]")
        for r in p["ripples"]:
            node = tree.add(f"[cyan]{r['ripple']}[/cyan] [dim](run {r['f']})[/dim]")
            for rd in r["reads"]:
                label = f"{rd['source']}.{rd['table']}" if rd["source"] else f"[dim]own[/dim] {rd['table']}"
                node.add(f"← {label}")
            for w in r["writes"]:
                node.add(f"[green]→ {w}[/green]")
        for tname, cols in (p.get("columns") or {}).items():
            cnode = tree.add(f"[magenta]{tname}[/magenta] [dim]columns[/dim]")
            if cols == "opaque":
                cnode.add("[dim]opaque (a .sql() output — column derivations unprovable)[/dim]")
                continue
            for col, prov in cols.items():
                if prov == "constant":
                    cnode.add(f"{col} [dim]← (constant)[/dim]")
                elif prov == "opaque":
                    cnode.add(f"{col} [dim]← opaque[/dim]")
                else:
                    srcs = ", ".join(f"{s['ref']}.{s['column']}" for s in prov)
                    cnode.add(f"{col} ← {srcs}")
        console.print(tree)


def trace(
    ref: str = typer.Argument(..., metavar="POND.TABLE", help="The table the rows are in."),
    where: Optional[str] = typer.Option(None, "--where", "-w", help="SQL condition selecting the rows, "
                                                                    "e.g. \"order_id = 42\". Omit for the whole table."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
) -> None:
    """Find the run that produced some published rows.

    Shows the newest run among the matching rows, with its version, timings and status, and the window of
    Source data it read::

        duckstring data trace revenue.revenue_by_product --where "product_id = 7"
    """
    from urllib.parse import quote

    from . import _http
    from .config import resolve_catchment
    from .data import split_ref

    pond, table = split_ref(ref, "a table")
    _, cfg = resolve_catchment(catchment)
    query = f"table={quote(table)}"
    if where:
        query += f"&where={quote(where)}"
    if major is not None:
        query += f"&major={major}"
    data = _http.get(f"{cfg['url']}/api/ponds/{pond}/trace?{query}", auth=cfg).json()

    if not data.get("matched"):
        typer.echo("No rows matched.")
        return
    run = data.get("run")
    typer.echo(f"{data['matched']} row(s) · newest produced at f = {data['row_f']}")
    if run:
        typer.echo(f"  run: {pond} v{run['version']} · {run['status']} · {run['started_at']} → {run['finished_at']}")
    w = data.get("window") or {}
    typer.echo(f"  input window: ({w.get('previous_f') or 'NEVER'}, {w.get('f')}]")
    if data.get("sources"):
        typer.echo(f"  sources read over that window: {', '.join(data['sources'])}")
