from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Optional

import typer


def get(
    outlet: str = typer.Argument(..., help="Pond name."),
    ripple: str = typer.Argument(..., help="Ripple name within the Pond."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to read from (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver whose major line to read."),
    path: Optional[Path] = typer.Option(None, "--path", help="Output directory (default: ./ponds/{outlet}/{ripple})."),
) -> None:
    """Download a Ripple's output directory from a Catchment."""
    from rich.console import Console

    from . import _http
    from .config import resolve_catchment

    _, cfg = resolve_catchment(catchment)
    url = cfg["url"]

    out_dir = path or (Path("ponds") / outlet / ripple)
    out_dir = Path(out_dir)

    console = Console()
    console.print(f"Fetching [bold]{outlet}.{ripple}[/bold]...")
    resp = _http.get(
        f"{url}/api/ponds/{outlet}/ripples/{ripple}", key=cfg.get("key"),
        params=_http.pond_params(major, version),
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            zf.extractall(out_dir)
    except zipfile.BadZipFile:
        (out_dir / "output.bin").write_bytes(resp.content)

    console.print(f"[green]Written to[/green] {out_dir}")


def query(
    outlet: str = typer.Argument(..., help="Pond name."),
    ripple: Optional[str] = typer.Argument(None, help="Ripple name (default query: SELECT * LIMIT 10)."),
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to use (uses default if omitted)."),
    major: Optional[int] = typer.Option(None, "--major", "-m", help="Major version to query (default: latest)."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Specific semver whose major line to query."),
    sql: Optional[str] = typer.Option(None, "--sql", help="SQL statement, or @path/to/file.sql to read from a file."),
    csv_out: Optional[str] = typer.Option(None, "--csv", metavar="FILENAME", help="Write result as CSV."),
    json_out: Optional[str] = typer.Option(None, "--json", metavar="FILENAME", help="Write result as JSON records."),
    parquet_out: Optional[str] = typer.Option(None, "--parquet", metavar="FILENAME", help="Write result as Parquet."),
    path: Optional[Path] = typer.Option(None, "--path", help="Output directory for file output (overrides default location)."),
) -> None:
    """Run a SQL query against a Pond's tables and print or save the result."""
    from rich.console import Console
    from rich.table import Table

    from . import _http
    from .config import resolve_catchment

    _, cfg = resolve_catchment(catchment)
    url = cfg["url"]
    console = Console()

    sql_stmt = sql
    if sql_stmt and sql_stmt.startswith("@"):
        sql_file = Path(sql_stmt[1:])
        if not sql_file.exists():
            typer.echo(f"Error: SQL file not found: {sql_file}", err=True)
            raise typer.Exit(1)
        sql_stmt = sql_file.read_text(encoding="utf-8")
    elif not sql_stmt and ripple:
        sql_stmt = f"SELECT * FROM {outlet}.{ripple} LIMIT 10"

    payload: dict = {"pond": outlet}
    if major is not None:
        payload["major"] = major
    if version is not None:
        payload["version"] = version
    if ripple:
        payload["ripple"] = ripple
    if sql_stmt:
        payload["sql"] = sql_stmt

    output_filename = csv_out or json_out or parquet_out
    if output_filename:
        if csv_out:
            payload["format"] = "csv"
        elif json_out:
            payload["format"] = "json"
        else:
            payload["format"] = "parquet"

    resp = _http.post(f"{url}/api/query", key=cfg.get("key"), json=payload)

    if not output_filename:
        try:
            rows = resp.json()
        except Exception:
            typer.echo(resp.text)
            return

        if not rows:
            console.print("[dim]No results.[/dim]")
            return

        if isinstance(rows, list) and rows:
            table = Table(show_header=True, header_style="bold dim")
            for col in rows[0].keys():
                table.add_column(str(col))
            for row in rows:
                table.add_row(*[str(v) if v is not None else "" for v in row.values()])
            console.print(table)
        else:
            import json
            console.print(json.dumps(rows, indent=2))
    else:
        if path:
            out_path = Path(path) / output_filename
        elif ripple:
            out_path = Path("ponds") / outlet / ripple / output_filename
        else:
            out_path = Path("ponds") / outlet / output_filename

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(resp.content)
        console.print(f"[green]Written to[/green] {out_path}")
