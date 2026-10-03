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
    from ..pondignore import DEFAULT_PATTERNS, IGNORE_FILE

    src = cwd / "src"
    src.mkdir(exist_ok=True)
    (src / "pond.py").write_text(pond_py_content, encoding="utf-8")
    (src / "puddles.py").write_text(_PUDDLES_PY, encoding="utf-8")
    (cwd / "pond.toml").write_text(toml_content, encoding="utf-8")
    (cwd / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
    (cwd / IGNORE_FILE).write_text(DEFAULT_PATTERNS, encoding="utf-8")
    (cwd / "README.md").write_text(readme_content, encoding="utf-8")


def _use_pond_env() -> None:
    """Re-run this command under the Pond's own environment (``.venv``, from ``uv sync``) when it declares
    one, so a local run imports the same packages a deployed one does (plans/pond-environments.md)."""
    import os
    import sys

    from ..environments import REEXEC_ENV, has_duckstring, local_python

    cwd = Path.cwd()
    python = local_python(cwd)
    if python is None:
        if (cwd / "pyproject.toml").is_file() and not (cwd / ".venv").exists() and not os.environ.get(REEXEC_ENV):
            typer.echo("Warning: this Pond has a pyproject.toml but no .venv; running in the current "
                       "environment. Run `uv sync` to create the Pond's environment.", err=True)
        return
    if not has_duckstring(python):
        typer.echo("Error: the Pond's environment (.venv) doesn't have Duckstring installed. "
                   "Add it with `uv add duckstring`.", err=True)
        raise typer.Exit(1)
    os.environ[REEXEC_ENV] = "1"
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(str(python), [str(python), "-m", "duckstring", *sys.argv[1:]])


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
    console.print("  [dim].pondignore[/dim]      — files deploy leaves out (puddles/, .env, caches)")


# The demo pipelines. Each entry is (pond name, one-line role), in pipeline order; the command copies one
# set. Deploy order doesn't matter (a Sink can be deployed before its Sources), so the output only says
# what each Pond is, then how to deploy and run the set (_DEMO_OUTLETS).
_RIPPLE_DEMO = (
    ("transactions", "Inlet: a point-of-sale event log that grows each run"),
    ("products", "Inlet: a product catalogue that grows each run"),
    ("sales", "3 Ripples: daily_sales and price_tiers in parallel, then join_lines"),
    ("reports", "Outlet: a monthly summary"),
)
_TRICKLE_DEMO = (
    ("orders", "append Trickle: insert-only order-line history"),
    ("catalog", "merge Trickle: a product catalogue whose price changes flow downstream"),
    ("priced", "the Trickle builder: order lines joined to prices, incrementally"),
    ("revenue", "Outlet: revenue by product"),
)
# The real-data Trickle demos (plans/real-data-testing.md): each has at least 4 Ponds, a cross-Pond join,
# and two independent Outlets, to show one path running at a different cadence from another.
_TPCDS_DEMO = (
    ("tpcds_sales", "append Trickle: the TPC-DS store_sales fact, generated with dsdgen and streamed"),
    ("tpcds_items", "merge Trickle: the item dimension, with price drift"),
    ("tpcds_stores", "merge Trickle: the store dimension"),
    ("tpcds_priced", "the Trickle builder: a 3-way incremental star join"),
    ("tpcds_category_revenue", "Outlet: revenue per category"),
    ("tpcds_store_revenue", "Outlet: revenue per store"),
)
_GHARCHIVE_DEMO = (
    ("gh_events", "append Trickle: the public GitHub event archive, hour by hour"),
    ("gh_actors", "merge Trickle: the actor dimension, from the event stream"),
    ("gh_pushes", "the Trickle builder: push events joined to gh_actors"),
    ("gh_repo_activity", "Outlet: activity per repository"),
    ("gh_stars", "append Trickle: star and fork events"),
    ("gh_trending", "Outlet: repositories ranked by stars and forks"),
)
# A dbt-mode Pond deployed alongside a plain-Python Source (plans/dbt.md). shop_analytics is a dbt project,
# each model a Ripple, reading shop_orders as a cross-Pond source(). Needs the dbt extra to deploy/run.
_DBT_DEMO = (
    ("shop_orders", "Inlet: plain Python, generating the orders the dbt project reads"),
    ("shop_analytics", "a dbt project deployed as a Pond: 3 models, one Ripple each"),
)
# A third element names the demo directory to copy when it differs from the Pond's name.
_SQL_DEMO = (
    ("transactions", "Inlet: a point-of-sale event log that grows each run"),
    ("products", "Inlet: a product catalogue that grows each run"),
    ("sales", "3 SQL Ripples declared in pond.toml, plus a static table of price bands", "sql_sales"),
    ("reports", "Outlet: a monthly summary, as one SQL Ripple", "sql_reports"),
)
_DEMO_OUTLETS = {
    _RIPPLE_DEMO: ("reports",),
    _TRICKLE_DEMO: ("revenue",),
    _TPCDS_DEMO: ("tpcds_category_revenue", "tpcds_store_revenue"),
    _GHARCHIVE_DEMO: ("gh_repo_activity", "gh_trending"),
    _DBT_DEMO: ("shop_analytics",),
    _SQL_DEMO: ("reports",),
}
_DEMO_PONDS = tuple(name for name, _ in _RIPPLE_DEMO)  # the default set (back-compat)


@app.command()
def demo(
    ripple: bool = typer.Option(False, "--ripple", help="transactions, products, sales, reports: plain Ripples (the default)."),
    trickle: bool = typer.Option(False, "--trickle", help="orders, catalog, priced, revenue: incremental Trickles."),
    tpcds: bool = typer.Option(False, "--tpcds", help="Six Ponds over locally generated TPC-DS data."),
    gharchive: bool = typer.Option(False, "--gharchive", help="Six Ponds over the public GitHub event archive."),
    dbt: bool = typer.Option(False, "--dbt", help="A dbt project deployed as a Pond, and its Source."),
    sql: bool = typer.Option(False, "--sql", help="The --ripple set with sales and reports written as SQL Ripples."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Create a set of demo Pond projects as subdirectories.

    --dbt needs the dbt extra to deploy and run: pip install 'duckstring[dbt]'.
    """
    import shutil

    from rich.console import Console

    console = Console()
    cwd = Path.cwd()

    if sum((ripple, trickle, tpcds, gharchive, dbt, sql)) > 1:
        typer.echo("Error: pass only one of --ripple / --trickle / --tpcds / --gharchive / --dbt / --sql.",
                   err=True)
        raise typer.Exit(1)
    ponds = (
        _SQL_DEMO if sql
        else _TPCDS_DEMO if tpcds
        else _GHARCHIVE_DEMO if gharchive
        else _DBT_DEMO if dbt
        else _TRICKLE_DEMO if trickle
        else _RIPPLE_DEMO  # default (no flag) = the Ripple set
    )

    existing = [name for name, *_ in ponds if (cwd / name).exists()]
    if existing:
        for name in existing:
            typer.echo(f"Error: '{name}' already exists in this directory.", err=True)
        raise typer.Exit(1)

    pond_list = ", ".join(f"[bold]{name}/[/bold]" for name, *_ in ponds)
    console.print(f"Will create {pond_list} in {cwd}")
    if not yes:
        typer.confirm("Continue?", default=True, abort=True)

    for name, _role, *source in ponds:
        shutil.copytree(_DEMO_DIR / (source[0] if source else name), cwd / name,
                        ignore=shutil.ignore_patterns("__pycache__"))

    kind = (
        "SQL Ripple" if sql
        else "TPC-DS (real-data Trickle)" if tpcds
        else "GHArchive (real-data Trickle)" if gharchive
        else "dbt-mode" if dbt
        else "Trickle (incremental)" if trickle
        else "Ripple"
    )
    console.print(f"[green]Created[/green] {kind} demo pipeline:")
    width = max(len(name) for name, *_ in ponds) + 1
    for name, role, *_ in ponds:
        console.print(f"  [bold]{name + '/':<{width}}[/bold]  {role}")
    outlets = _DEMO_OUTLETS[ponds]
    run = " or ".join(f"[bold]duckstring trigger pulse {o}[/bold]" for o in outlets)
    console.print(f"\nNext: [bold]duckstring pond deploy --all --yes[/bold], then {run}.")


@app.command()
def hydrate(
    source: Optional[list[str]] = typer.Option(
        None, "--source", "-s", help="Hydrate only these Sources (repeatable)."
    ),
    catchment: Optional[str] = typer.Option(
        None, "--catchment", "-c", help="Catchment used by Puddles that read from one, and by --from-catchment."
    ),
    from_catchment: bool = typer.Option(
        False, "--from-catchment", help="Fill Sources with no Puddle definition from the Catchment."
    ),
) -> None:
    """Build this Pond's Puddles into puddles/ for a local run."""
    _use_pond_env()
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
    ripple: Optional[str] = typer.Option(None, "--ripple", "-r", help="Run only this Ripple, against the existing local "
                                                                      "output."),
    fresh: bool = typer.Option(False, "--fresh", help="Ignore the Pond's own Puddle and start from nothing."),
) -> None:
    """Run this Pond once on this machine against its Puddles, with no Catchment. Output goes to puddles/out/."""
    _use_pond_env()
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
