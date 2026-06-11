from __future__ import annotations

import typer

from . import catchment as catchment_cmd
from . import pond as pond_cmd
from . import puddle as puddle_cmd
from .control import clear, failure_budget, force, kill, sleep, wake
from .data import get, query
from .deploy import deploy
from .status import status
from .trigger import pulse, remove, tap, tide, wave
from .window import app as window_app

app = typer.Typer(help="Duckstring CLI", no_args_is_help=True, add_completion=True)

trigger_app = typer.Typer(
    help="Send execution signals to Outlet Ponds.",
    no_args_is_help=True,
)
trigger_app.command("tap")(tap)
trigger_app.command("pulse")(pulse)
trigger_app.command("wave")(wave)
trigger_app.command("tide")(tide)
trigger_app.command("remove")(remove)
trigger_app.add_typer(window_app, name="window")

control_app = typer.Typer(
    help="Manage a Pond's execution & health: wake, sleep, force, kill, clear a failure, set budgets.",
    no_args_is_help=True,
)
control_app.command("force")(force)
control_app.command("wake")(wake)
control_app.command("sleep")(sleep)
control_app.command("kill")(kill)
control_app.command("clear")(clear)
control_app.command("failure-budget")(failure_budget)

app.add_typer(catchment_cmd.app, name="catchment")
app.add_typer(pond_cmd.app, name="pond")
app.add_typer(puddle_cmd.app, name="puddle")
app.add_typer(trigger_app, name="trigger")
app.add_typer(control_app, name="control")

pond_cmd.app.command("deploy")(deploy)
app.command("status")(status)
app.command("get")(get)
app.command("query")(query)


def main() -> None:
    app()


__all__ = ["app", "main"]
