from __future__ import annotations

import typer

from . import periscope as periscope_cmd

app = typer.Typer(help="Duckstring CLI", no_args_is_help=True, add_completion=True)

app.add_typer(periscope_cmd.app, name="periscope")

__all__ = ["app"]
