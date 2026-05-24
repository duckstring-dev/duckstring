from __future__ import annotations

from pathlib import Path

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
"""

_MAIN = """\
\"\"\"Local runner for smoke-testing this Pond without a Catchment.\"\"\"

if __name__ == "__main__":
    print("Local runner not yet implemented. Deploy to a Catchment to run this Pond.")
    print("  duckstring deploy <catchment>")
"""


def _write_pond_files(cwd: Path, toml_content: str, pond_py_content: str, readme_content: str) -> None:
    src = cwd / "src"
    src.mkdir(exist_ok=True)
    (src / "pond.py").write_text(pond_py_content, encoding="utf-8")
    (cwd / "pond.toml").write_text(toml_content, encoding="utf-8")
    (cwd / "__main__.py").write_text(_MAIN, encoding="utf-8")
    (cwd / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
    (cwd / "README.md").write_text(readme_content, encoding="utf-8")


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
        "```bash\nduckstring deploy <catchment>\n```\n"
    )

    _write_pond_files(cwd, toml_content, pond_py, readme)

    console = Console()
    console.print(f"[green]Created[/green] Pond [bold]{name}[/bold] in {cwd}")
    console.print("  [dim]src/pond.py[/dim]   — define your Ripples here")
    console.print("  [dim]pond.toml[/dim]     — Pond name, version, and Sources")


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
    console.print("  [bold]sales/[/bold]         — deploy third  (3 Ripples: stage → stage → join)")
    console.print("  [bold]reports/[/bold]       — deploy fourth, then: [dim]duckstring pulse <catchment> reports[/dim]")
