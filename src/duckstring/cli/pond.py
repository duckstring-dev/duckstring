from __future__ import annotations

from enum import Enum
from pathlib import Path

import typer

app = typer.Typer(help="Manage Pond projects.", add_completion=False, no_args_is_help=True)

# ── Templates ─────────────────────────────────────────────────────────────────

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

_DEMO_INLET_TOML = """\
[pond]
name = "inlet"
version = "1.0.0"
type = "inlet"
"""

_DEMO_INLET_PY = """\
from duckstring import ripple


@ripple
def daily(pond):
    # Generate synthetic data — replace with your actual external data source.
    data = pond.con.sql(
        "SELECT range AS id, 'value_' || range::VARCHAR AS label FROM range(10)"
    )
    pond.write_table("daily", data)
"""

_DEMO_INLET_README = """\
# inlet

Demo Duckstring Inlet — generates synthetic data for the demo pipeline.

Deploy to a Catchment:

```bash
duckstring deploy <catchment>
```
"""

_DEMO_POND_TOML = """\
[pond]
name = "pond"
version = "1.0.0"

[sources]
inlet = "1.0.0"
"""

_DEMO_POND_PY = """\
from duckstring import ripple


@ripple
def clean(pond):
    raw = pond.read_table("inlet.daily")
    pond.write_table("clean", raw)
"""

_DEMO_POND_README = """\
# pond

Demo Duckstring Pond — reads from the `inlet` demo and passes data downstream.

Requires the `inlet` demo to be deployed to the same Catchment.

Deploy to a Catchment:

```bash
duckstring deploy <catchment>
```
"""

_DEMO_OUTLET_TOML = """\
[pond]
name = "outlet"
version = "1.0.0"
type = "outlet"

[sources]
pond = "1.0.0"
"""

_DEMO_OUTLET_PY = """\
from duckstring import ripple


@ripple
def daily(pond):
    df = pond.read_table("pond.clean")
    pond.write_table("daily", df)
"""

_DEMO_OUTLET_README = """\
# outlet

Demo Duckstring Outlet — final data product in the demo pipeline.

Requires both `inlet` and `pond` demos to be deployed to the same Catchment.

Deploy to a Catchment:

```bash
duckstring deploy <catchment>
```

Trigger a single run:

```bash
duckstring pulse <catchment> outlet
```
"""

_DEMO_TEMPLATES = {
    "inlet": (_DEMO_INLET_TOML, _DEMO_INLET_PY, _DEMO_INLET_README),
    "pond": (_DEMO_POND_TOML, _DEMO_POND_PY, _DEMO_POND_README),
    "outlet": (_DEMO_OUTLET_TOML, _DEMO_OUTLET_PY, _DEMO_OUTLET_README),
}

# ── Helpers ───────────────────────────────────────────────────────────────────


def _write_pond_files(cwd: Path, toml_content: str, pond_py_content: str, readme_content: str) -> None:
    src = cwd / "src"
    src.mkdir(exist_ok=True)
    (src / "pond.py").write_text(pond_py_content, encoding="utf-8")
    (cwd / "pond.toml").write_text(toml_content, encoding="utf-8")
    (cwd / "__main__.py").write_text(_MAIN, encoding="utf-8")
    (cwd / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
    (cwd / "README.md").write_text(readme_content, encoding="utf-8")


# ── Commands ──────────────────────────────────────────────────────────────────


class DemoType(str, Enum):
    inlet = "inlet"
    pond = "pond"
    outlet = "outlet"


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


@app.command()
def demo(
    type: DemoType = typer.Argument(..., help="Pond type: inlet, pond, or outlet."),
) -> None:
    """Create a demo Pond project in the current directory."""
    from rich.console import Console

    cwd = Path.cwd()

    if (cwd / "pond.toml").exists():
        typer.echo("Error: pond.toml already exists in this directory.", err=True)
        typer.echo("Use an empty directory for a demo Pond project.", err=True)
        raise typer.Exit(1)

    toml_content, pond_py, readme = _DEMO_TEMPLATES[type.value]
    _write_pond_files(cwd, toml_content, pond_py, readme)

    console = Console()
    console.print(f"[green]Created[/green] demo [bold]{type.value}[/bold] Pond in {cwd}")

    if type == DemoType.inlet:
        console.print("  Next: deploy to a Catchment — [dim]duckstring deploy <catchment>[/dim]")
    elif type == DemoType.pond:
        console.print("  Next: deploy to a Catchment — [dim]duckstring deploy <catchment>[/dim]")
    else:
        console.print("  Next: deploy — [dim]duckstring deploy <catchment>[/dim]")
        console.print("  Then run — [dim]duckstring pulse <catchment> outlet[/dim]")
