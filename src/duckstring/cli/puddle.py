from __future__ import annotations

from pathlib import Path

import typer

from ..trickle_io import first_row

app = typer.Typer(
    help="Inspect this Pond's local Puddles and run output.",
    add_completion=False,
    no_args_is_help=True,
)


def _load_project():
    from .pond import _load_project

    return _load_project()


def _inventory(project) -> list[dict]:
    """Every local table: hydrated snapshots (puddles/ponds/*/data/) then run output (puddles/out/), so an output
    view overrides a same-named self-puddle snapshot when both are registered. Each is listed and read through
    the data plane, as a Sink reads it, so append and merge Trickles appear beside plain tables (the viewer
    once globbed single files and plain-table versions only)."""
    from ..trickle_io import CHANGELOG_SUFFIX, DROPLOG_SUFFIX, WARM_SUFFIX

    dirs: list[tuple[str, Path, str]] = []
    ponds_dir = project.puddles_dir / "ponds"
    if ponds_dir.exists():
        for d in sorted(ponds_dir.iterdir()):
            if not (d / "data").is_dir():
                continue
            work = project.puddles_dir / "work" / d.name / "data"  # a step's or live definition's working copy
            dirs.append((d.name, work if (work / "_trickle.json").exists() else d / "data", "puddle"))
    if project.out_dir.is_dir():
        dirs.append((project.name, project.out_dir, "out"))
    from ..dataplane import ParquetDataPlane

    dp = ParquetDataPlane()
    items: list[dict] = []
    for pond, data_dir, kind in dirs:
        for table in dp.list_tables(data_dir):
            if table.endswith((CHANGELOG_SUFFIX, WARM_SUFFIX, DROPLOG_SUFFIX)):
                continue  # a Trickle's companions: read through the table they belong to
            try:
                select = dp.read_select(data_dir, table)
            except FileNotFoundError:
                continue
            items.append({"pond": pond, "table": table, "kind": kind, "select": select,
                          "files": _files(data_dir, table)})
    return items


def _files(data_dir: Path, table: str) -> list[Path]:
    """The Parquet files holding ``table`` in ``data_dir``, in any of its published forms (for size and age)."""
    out = [data_dir / f"{table}.parquet"] if (data_dir / f"{table}.parquet").is_file() else []
    for d in data_dir.iterdir():
        if d.is_dir() and (d.name == table or (d.name.startswith(f"{table}__") and "__" not in d.name[len(table) + 2:])):
            out += sorted(d.glob("*.parquet"))
    return out


def _open(project, items: list[dict]):
    """An in-memory DuckDB with every item registered as "{pond}"."{table}" (and bare) views —
    the same pattern the Catchment's data route uses, with no server and no live registry."""
    import duckdb

    con = duckdb.connect()
    for item in items:
        select = _user_columns(con, item["select"])
        con.execute(f'CREATE SCHEMA IF NOT EXISTS "{item["pond"]}"')
        con.execute(f'CREATE OR REPLACE VIEW "{item["pond"]}"."{item["table"]}" AS {select}')
        con.execute(f'CREATE OR REPLACE VIEW "{item["table"]}" AS {select}')
    return con


def _user_columns(con, select: str) -> str:
    """``select`` without the ``_duckstring_*`` system columns a Trickle's files carry."""
    from ..trickle_io import _q

    cols = [c for c in con.sql(select).columns if not c.startswith("_duckstring_")]
    return f"SELECT {', '.join(_q(c) for c in cols)} FROM ({select})"


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


def _print_sources(console, project) -> None:
    """Each hydrated Source: how it's read (live, copied, copied with history), its set, and the freshness the last
    run read it at."""
    from rich.table import Table

    from ..local.state import load_hydrated, load_position

    hydrated = load_hydrated(project)
    if hydrated is None or not hydrated.sources:
        return
    position = load_position(project)
    table = Table(show_header=True, header_style="bold dim", title=f"Sources (hydrated as of {hydrated.at})",
                  title_justify="left", title_style="bold")
    for col in ("source", "mode", "set", "from", "last run read"):
        table.add_column(col)
    for name, st in sorted(hydrated.sources.items()):
        mode = {"live": "live", "derived": "live, through its definition"}.get(st.mode, "copied")
        if st.mode == "copied" and st.history:
            mode = "copied, with history"
        if st.steps:
            mode += " + steps"
        read = position.sources.get(name, "") if position is not None else ""
        table.add_row(name, mode, st.set, st.f or "", read)
    console.print(table)
    if position is not None:
        console.print(f"[dim]Last run: {position.f}" + (f", step {position.step}" if position.step else "") + "[/dim]")


@app.command("ls")
def ls() -> None:
    """List hydrated Puddles and run output tables, with row counts, sizes and ages."""
    import duckdb
    from rich.console import Console
    from rich.table import Table

    console = Console()
    project = _load_project()
    items = _inventory(project)
    _print_sources(console, project)
    if not items:
        console.print("[dim]No puddles — run 'duckstring pond hydrate' first.[/dim]")
        return

    con = duckdb.connect()
    table = Table(show_header=True, header_style="bold dim")
    for col in ("table", "kind", "rows", "size", "age"):
        table.add_column(col)
    for item in items:
        (rows,) = first_row(con.execute(f"SELECT count(*) FROM ({item['select']})"))
        size_kb = sum(p.stat().st_size for p in item["files"]) / 1024
        size = f"{size_kb / 1024:.1f} MB" if size_kb >= 1024 else f"{size_kb:.1f} KB"
        newest = max(item["files"], key=lambda p: p.stat().st_mtime) if item["files"] else None
        age = _age(newest) if newest is not None else ""
        table.add_row(f"{item['pond']}.{item['table']}", item["kind"], str(rows), size, age)
    console.print(table)


@app.command("show")
def show(
    ref: str = typer.Argument(..., help="Table to preview: {pond}.{table} or a bare table name."),
    limit: int = typer.Option(10, "--limit", "-n", help="Rows to show."),
) -> None:
    """Print the first rows of a Puddle or output table. Output takes precedence over a Puddle of the same name."""
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
    """Run SQL over every Puddle and output table, as "{pond}"."{table}" or bare names."""
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


@app.command("push")
def push(
    catchment: str = typer.Option(None, "--catchment", "-c", help="Catchment to use (default if omitted)."),
    major: int = typer.Option(None, "--major", "-m", help="Major version to target (default: latest)."),
) -> None:
    """Send puddles.toml's section for a Catchment without redeploying: the Sources this Pond reads as Puddles
    there. No file, or no section for it, clears them."""
    from rich.console import Console

    from ..local.state import read_puddles_toml
    from . import _http
    from .config import resolve_catchment
    from .deploy import print_puddle_sources

    project = _load_project()
    try:
        puddles = read_puddles_toml(project.dir)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    name, cfg = resolve_catchment(catchment)
    resp = _http.post(f"{cfg['url']}/api/ponds/{project.name}/puddles", auth=cfg,
                      params=_http.pond_params(major, None), json={"puddles": puddles})
    body = resp.json()
    console = Console()
    if body.get("puddle_sources"):
        print_puddle_sources(console, body)
    else:
        console.print(f"'{project.name}' reads no Source as a Puddle on {name}.")
