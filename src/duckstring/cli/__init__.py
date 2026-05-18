from __future__ import annotations

import typer

from . import catchment as catchment_cmd

app = typer.Typer(help="Duckstring CLI", no_args_is_help=True, add_completion=True)

app.add_typer(catchment_cmd.app, name="catchment")


def main() -> None:
    app()


__all__ = ["app", "main"]
