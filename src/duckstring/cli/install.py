"""`duckstring install [COMPONENT...]`: machine setup a pip install can't do (plans/records-and-media.md).

    duckstring install                       # every component's state
    duckstring install postgres              # PostgreSQL for Records, from the official packages
    duckstring install extensions            # DuckDB's extensions, for a machine without internet access
    duckstring install extensions httpfs postgres
    duckstring install aws dbt               # Duckstring's extras, pinned to this Duckstring
"""

from __future__ import annotations

import shlex
from typing import List, Optional

import typer

COMPONENTS = ("postgres", "extensions")


def _plans(components: list[str], postgres_version: int) -> list:
    from duckstring import install as inst

    plans: list = []
    extras: list[str] = []
    i = 0
    while i < len(components):
        name = components[i]
        if name == "postgres":
            plans.append(inst.postgres_plan(postgres_version))
        elif name == "extensions":
            names = []
            while i + 1 < len(components) and components[i + 1] not in COMPONENTS + inst.EXTRAS:
                i += 1
                names.append(components[i])
            plans += inst.extension_plans(names or None)
        else:
            extras.append(name)
        i += 1
    if extras:
        plans.append(inst.extra_plan(extras))
    return plans


def _status(postgres_version: int) -> None:
    from rich.console import Console
    from rich.table import Table

    from duckstring import install as inst

    table = Table(box=None, pad_edge=False)
    table.add_column("Component")
    table.add_column("State")
    table.add_column("Detail", overflow="fold")
    rows = [inst.postgres_plan(postgres_version), *inst.extension_plans()]
    rows += [inst.extra_plan([e]) for e in inst.EXTRAS]
    colour = {"installed": "green", "missing": "yellow", "unsupported": "red"}
    for plan in rows:
        label = plan.component.removeprefix("extras ")
        detail = plan.detail
        table.add_row(label, f"[{colour[plan.state]}]{plan.state}[/]", detail)
    Console().print(table)
    typer.echo("\nInstall with: duckstring install COMPONENT...")


def install(
    components: Optional[List[str]] = typer.Argument(
        None, help="postgres, extensions [NAME...], or an extra (aws, azure, gcp, databricks, dbt, lineage, "
                   "starrocks). None: show every component's state."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Run the steps without asking."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the steps and run nothing."),
    postgres_version: int = typer.Option(18, "--postgres-version", help="The PostgreSQL major to install."),
) -> None:
    """Install what Duckstring needs beyond pip: PostgreSQL for Records, DuckDB's extensions, and
    Duckstring's extras. Only official sources are used, and the steps are shown before anything runs."""
    from duckstring import install as inst

    if not components:
        _status(postgres_version)
        return
    try:
        plans = _plans(list(components), postgres_version)
    except inst.InstallError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None
    for plan in plans:
        if plan.state == "installed":
            typer.echo(f"{plan.component}: installed ({plan.detail})")
        elif plan.state == "unsupported":
            typer.echo(f"{plan.component}: {plan.detail}", err=True)
    unsupported = [p for p in plans if p.state == "unsupported"]
    todo = [p for p in plans if p.state == "missing"]
    if todo:
        typer.echo("\nSteps:")
        for plan in todo:
            for step in plan.steps:
                typer.echo(f"  {step.describe}" + ("  (as root)" if step.root else ""))
                if step.argv is not None:
                    typer.echo(f"      $ {shlex.join(step.argv)}")
        if dry_run:
            raise typer.Exit(1 if unsupported else 0)
        if inst.needs_root(todo):
            typer.echo('\nSome steps need root: run this as root, e.g. sudo "$(which duckstring)" install '
                       + " ".join(components), err=True)
            raise typer.Exit(1)
        if not yes and not typer.confirm("\nRun these steps?", default=True):
            raise typer.Exit(1)
        try:
            inst.run(todo, typer.echo)
        except inst.InstallError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from None
    elif not dry_run:
        # Record an already-present server, so a Catchment started later finds the same one.
        for plan in plans:
            if plan.after is not None and plan.state == "installed":
                plan.after()
    if unsupported:
        raise typer.Exit(1)
