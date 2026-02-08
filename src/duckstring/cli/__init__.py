from __future__ import annotations

import sys
import typer

from . import basin as basin_cmd
from . import catchment as catchment_cmd
from . import periscope as periscope_cmd

app = typer.Typer(help="Duckstring CLI", no_args_is_help=True, add_completion=True)

app.add_typer(basin_cmd.app, name="basin", invoke_without_command=True)
app.add_typer(catchment_cmd.app, name="catchment")
app.add_typer(periscope_cmd.app, name="periscope", invoke_without_command=True)


def _rewrite_argv_for_periscope(argv: list[str]) -> list[str]:
    try:
        idx = argv.index("periscope")
    except ValueError:
        return argv

    out = list(argv)
    i = idx + 1
    while i < len(out):
        if out[i] == "-v":
            if i + 1 >= len(out) or out[i + 1].startswith("-"):
                out[i] = "--list-versions"
        i += 1
    return out


def main() -> None:
    sys.argv = _rewrite_argv_for_periscope(sys.argv)
    app()


__all__ = ["app", "main"]
