from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(help="Manage Pond projects.", add_completion=False, no_args_is_help=True)

_DEMO_DIR = Path(__file__).parent.parent / "demo"

_GITIGNORE = """\
__pycache__/
*.pyc
.venv/
dist/
*.egg-info/
.ruff_cache/
puddles/
"""

_PUDDLES_PY = """\
\"\"\"Puddles — local snapshots of this Pond's Sources, for testing before deployment.

Define one per Source table this Pond reads, then:
    duckstring pond hydrate
    duckstring pond run
\"\"\"

# from duckstring import puddle
#
# @puddle("some_source.some_table")
# def some_table(p):
#     p.write_table(p.con.sql("SELECT 1 AS id"))          # synthesise it,
#     # p.write_path("~/data/sample.parquet")             # copy it from a file,
#     # p.write_table(p.catchment().get())                # or pull it from a Catchment.
"""


def _write_pond_files(cwd: Path, toml_content: str, pond_py_content: str, readme_content: str) -> None:
    src = cwd / "src"
    src.mkdir(exist_ok=True)
    (src / "pond.py").write_text(pond_py_content, encoding="utf-8")
    (src / "puddles.py").write_text(_PUDDLES_PY, encoding="utf-8")
    (cwd / "pond.toml").write_text(toml_content, encoding="utf-8")
    (cwd / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
    (cwd / "README.md").write_text(readme_content, encoding="utf-8")


def _load_project():
    from ..local import load_project

    try:
        return load_project()
    except FileNotFoundError:
        typer.echo("Error: no pond.toml found in the current directory.", err=True)
        typer.echo("Are you in a Pond project root? Run 'duckstring pond init <name>' to create one.", err=True)
        raise typer.Exit(1) from None


@app.command()
def init(
    name: str = typer.Argument(..., help="Name for the new Pond."),
) -> None:
    """Scaffold a new Pond project in the current directory."""
    from rich.console import Console

    cwd = Path.cwd()

    if (cwd / "pond.toml").exists():
        typer.echo("Error: pond.toml already exists in this directory.", err=True)
        typer.echo("Use an empty directory for a new Pond project.", err=True)
        raise typer.Exit(1)

    toml_content = f'[pond]\nname = "{name}"\nversion = "0.1.0"\n'
    pond_py = (
        "from duckstring import ripple\n\n\n"
        "@ripple\n"
        "def run(pond):\n"
        "    # TODO: implement your transformation.\n"
        "    pass\n"
    )
    readme = (
        f"# {name}\n\nA Duckstring Pond.\n\n"
        "Deploy to a Catchment:\n\n"
        "```bash\nduckstring pond deploy\n```\n"
    )

    _write_pond_files(cwd, toml_content, pond_py, readme)

    console = Console()
    console.print(f"[green]Created[/green] Pond [bold]{name}[/bold] in {cwd}")
    console.print("  [dim]src/pond.py[/dim]      — define your Ripples here")
    console.print("  [dim]src/puddles.py[/dim]   — define Source snapshots for local testing")
    console.print("  [dim]pond.toml[/dim]        — Pond name, version, and Sources")


_DEMO_PONDS = ("transactions", "products", "sales", "reports")


@app.command()
def demo() -> None:
    """Create the transactions, products, sales, and reports demo projects as subdirectories."""
    import shutil

    from rich.console import Console

    console = Console()
    cwd = Path.cwd()

    existing = [name for name in _DEMO_PONDS if (cwd / name).exists()]
    if existing:
        for name in existing:
            typer.echo(f"Error: '{name}' already exists in this directory.", err=True)
        raise typer.Exit(1)

    pond_list = ", ".join(f"[bold]{p}/[/bold]" for p in _DEMO_PONDS)
    console.print(f"Will create {pond_list} in {cwd}")
    typer.confirm("Continue?", default=True, abort=True)

    for name in _DEMO_PONDS:
        shutil.copytree(_DEMO_DIR / name, cwd / name)

    console.print("[green]Created[/green] demo pipeline:")
    console.print("  [bold]transactions/[/bold]  — deploy first  (POS event log, grows each run)")
    console.print("  [bold]products/[/bold]      — deploy second (product catalogue, grows each run)")
    console.print("  [bold]sales/[/bold]         — deploy third  (3 Ripples: daily_sales → price_tiers → join_lines)")
    console.print("  [bold]reports/[/bold]       — deploy fourth, then: [dim]duckstring pulse <catchment> reports[/dim]")


@app.command()
def hydrate(
    source: Optional[list[str]] = typer.Option(
        None, "--source", "-s", help="Hydrate only these Sources (repeatable)."
    ),
    catchment: Optional[str] = typer.Option(
        None, "--catchment", "-c", help="Catchment for puddles that pull (uses default if omitted)."
    ),
    from_catchment: bool = typer.Option(
        False, "--from-catchment", help="Fill Sources that have no puddle definition from the Catchment."
    ),
) -> None:
    """Materialise this Pond's puddles (Source snapshots) into puddles/ for a local run."""
    from rich.console import Console

    from ..local import hydrate as hydrate_project

    console = Console()
    project = _load_project()
    try:
        results, warnings = hydrate_project(
            project, only_sources=source, catchment=catchment, from_catchment=from_catchment
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None

    for w in warnings:
        console.print(f"[yellow]Warning:[/yellow] {w}")
    if not results:
        console.print(f"[dim]No puddles defined ({project.puddles_entry}) — nothing to hydrate.[/dim]")
        return

    failed = False
    for r in results:
        if r.status == "ok":
            console.print(f"  [green]✓[/green] {r.target} [dim]({r.duration_s:.2f}s)[/dim]")
        else:
            failed = True
            console.print(f"  [red]✗[/red] {r.target} — {r.error}")
            if r.traceback:
                console.print(f"[dim]{r.traceback}[/dim]")
    if failed:
        raise typer.Exit(1)
    console.print(f"[green]Hydrated[/green] into {project.puddles_dir / 'ponds'}")


@app.command()
def run(
    ripple: Optional[str] = typer.Option(None, "--ripple", "-r", help="Run a single Ripple against the existing local state."),
    fresh: bool = typer.Option(False, "--fresh", help="Ignore a self-puddle seed (start from nothing)."),
) -> None:
    """Execute this Pond locally against its hydrated puddles (no Catchment, no Duck)."""
    from rich.console import Console

    from ..local import run_pond

    console = Console()
    project = _load_project()
    try:
        result = run_pond(project, ripple=ripple, fresh=fresh)
    except (ValueError, FileNotFoundError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None

    if result.seeded:
        console.print(f"[dim]Seeded prior state from puddles/ponds/{project.name}/[/dim]")
    for r in result.ripples:
        if r.status == "ok":
            console.print(f"  [green]✓[/green] {r.name} [dim]({r.duration_s:.2f}s)[/dim]")
        else:
            console.print(f"  [red]✗[/red] {r.name} — {r.error}")
            if r.traceback:
                console.print(f"[dim]{r.traceback}[/dim]")
            if ripple:
                console.print(
                    "[dim]A single-Ripple run reads its intra-Pond inputs from the last local run — "
                    "if they are missing, run the full pond first: duckstring pond run[/dim]"
                )
    if not result.ok:
        raise typer.Exit(1)
    console.print(f"[green]Output written[/green] to {project.out_dir}")
