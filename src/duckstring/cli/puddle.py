from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(
    help="Inspect this Pond's local puddles (Source snapshots) and run output.",
    add_completion=False,
    no_args_is_help=True,
)


def _load_project():
    from .pond import _load_project

    return _load_project()


def _inventory(project) -> list[dict]:
    """Every local parquet: hydrated snapshots (puddles/ponds/*/data/) then run output (puddles/out/),
    so an output view overrides a same-named self-puddle snapshot when both are registered."""
    items: list[dict] = []
    ponds_dir = project.puddles_dir / "ponds"
    if ponds_dir.exists():
        for pond_dir in sorted(ponds_dir.iterdir()):
            for pq in sorted((pond_dir / "data").glob("*.parquet")):
                items.append({"pond": pond_dir.name, "table": pq.stem, "path": pq, "kind": "puddle"})
    for pq in sorted(project.out_dir.glob("*.parquet")):
        items.append({"pond": project.name, "table": pq.stem, "path": pq, "kind": "out"})
    return items


def _open(project, items: list[dict]):
    """An in-memory DuckDB with every item registered as "{pond}"."{table}" (and bare) views —
    the same pattern the Catchment's data route uses, with no server and no live registry."""
    import duckdb

    con = duckdb.connect()
    for item in items:
        select = f"SELECT * FROM read_parquet('{str(item['path']).replace(chr(39), chr(39) * 2)}')"
        con.execute(f'CREATE SCHEMA IF NOT EXISTS "{item["pond"]}"')
        con.execute(f'CREATE OR REPLACE VIEW "{item["pond"]}"."{item["table"]}" AS {select}')
        con.execute(f'CREATE OR REPLACE VIEW "{item["table"]}" AS {select}')
    return con


def _print_rows(console, cursor) -> None:
    from rich.table import Table

    if cursor.description is None:
        console.print("[dim]No results.[/dim]")
        return
    rows = cursor.fetchall()
    if not rows:
        console.print("[dim]No results.[/dim]")
        return
    table = Table(show_header=True, header_style="bold dim")
    for col in cursor.description:
        table.add_column(str(col[0]))
    for row in rows:
        table.add_row(*[str(v) if v is not None else "" for v in row])
    console.print(table)


def _age(path: Path) -> str:
    import time

    seconds = max(0, time.time() - path.stat().st_mtime)
    for unit, span in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= span:
            return f"{int(seconds // span)}{unit}"
    return f"{int(seconds)}s"


@app.command("ls")
def ls() -> None:
    """List hydrated puddles and run output, with row counts, size, and age."""
    import duckdb
    from rich.console import Console
    from rich.table import Table

    console = Console()
    project = _load_project()
    items = _inventory(project)
    if not items:
        console.print("[dim]No puddles — run 'duckstring pond hydrate' first.[/dim]")
        return

    con = duckdb.connect()
    table = Table(show_header=True, header_style="bold dim")
    for col in ("table", "kind", "rows", "size", "age"):
        table.add_column(col)
    for item in items:
        path_sql = str(item["path"]).replace("'", "''")
        (rows,) = con.execute(f"SELECT count(*) FROM read_parquet('{path_sql}')").fetchone()
        size_kb = item["path"].stat().st_size / 1024
        size = f"{size_kb / 1024:.1f} MB" if size_kb >= 1024 else f"{size_kb:.1f} KB"
        table.add_row(f"{item['pond']}.{item['table']}", item["kind"], str(rows), size, _age(item["path"]))
    console.print(table)


@app.command("show")
def show(
    ref: str = typer.Argument(..., help="Table to preview: {pond}.{table} or a bare table name."),
    limit: int = typer.Option(10, "--limit", "-n", help="Rows to show."),
) -> None:
    """Preview a puddle or output table (output wins when a self-puddle shares the name)."""
    from rich.console import Console

    console = Console()
    project = _load_project()
    items = _inventory(project)
    con = _open(project, items)
    target = f'"{ref.replace(".", chr(34) + "." + chr(34))}"' if "." in ref else f'"{ref}"'
    try:
        cursor = con.execute(f"SELECT * FROM {target} LIMIT {int(limit)}")
    except Exception as exc:
        known = ", ".join(f"{i['pond']}.{i['table']}" for i in items) or "(none)"
        typer.echo(f"Error: {exc}", err=True)
        typer.echo(f"Known tables: {known}", err=True)
        raise typer.Exit(1) from None
    _print_rows(console, cursor)


@app.command("query")
def query(
    sql: str = typer.Argument(..., help='SQL over the local tables, e.g. SELECT * FROM "sales"."sale_line".'),
) -> None:
    """Run SQL across every puddle and output table ("{pond}"."{table}" or bare names)."""
    from rich.console import Console

    console = Console()
    project = _load_project()
    con = _open(project, _inventory(project))
    try:
        cursor = con.execute(sql)
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    _print_rows(console, cursor)
