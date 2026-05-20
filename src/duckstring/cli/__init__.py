from __future__ import annotations

import typer

from . import catchment as catchment_cmd
from . import pond as pond_cmd
from .data import get, query
from .deploy import deploy
from .execute import pulse, tide, wave
from .status import status

app = typer.Typer(help="Duckstring CLI", no_args_is_help=True, add_completion=True)

app.add_typer(catchment_cmd.app, name="catchment")
app.add_typer(pond_cmd.app, name="pond")

app.command("deploy")(deploy)
app.command("pulse")(pulse)
app.command("wave")(wave)
app.command("tide")(tide)
app.command("status")(status)
app.command("get")(get)
app.command("query")(query)


def main() -> None:
    app()


__all__ = ["app", "main"]
