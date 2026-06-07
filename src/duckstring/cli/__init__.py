from __future__ import annotations

import typer

from . import catchment as catchment_cmd
from . import pond as pond_cmd
from .data import get, query
from .deploy import deploy
from .status import status
from .trigger import pulse, remove, start, stop, tap, tide, wave

app = typer.Typer(help="Duckstring CLI", no_args_is_help=True, add_completion=True)

trigger_app = typer.Typer(
    help="Send execution signals to Outlet Ponds.",
    no_args_is_help=True,
)
trigger_app.command("start")(start)
trigger_app.command("stop")(stop)
trigger_app.command("tap")(tap)
trigger_app.command("pulse")(pulse)
trigger_app.command("wave")(wave)
trigger_app.command("tide")(tide)
trigger_app.command("remove")(remove)

app.add_typer(catchment_cmd.app, name="catchment")
app.add_typer(pond_cmd.app, name="pond")
app.add_typer(trigger_app, name="trigger")

app.command("deploy")(deploy)
app.command("status")(status)
app.command("get")(get)
app.command("query")(query)


def main() -> None:
    app()


__all__ = ["app", "main"]
