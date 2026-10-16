"""The `duckstring trigger` group from before Refresh, kept hidden for one release. Each command prints its replacement and does
the same thing through it: `tap`/`pulse` are `refresh POND`, `wave` is `refresh add POND --continuous`, `tide` is
`refresh add POND --every BOUND`, `remove` removes the floating Refresh, and `window` is `duckstring window`.
"""

from __future__ import annotations

from typing import Optional

import typer

from .refresh import _CATCHMENT, _MAJOR, _SILENT_HELP, _VERSION, _WATCH_HELP, send_and_watch
from .window import legacy_app as window_legacy_app

app = typer.Typer(help="Deprecated: use `duckstring refresh` and `duckstring window`.", no_args_is_help=True,
                  hidden=True)
app.add_typer(window_legacy_app, name="window")


def _deprecated(old: str, new: str) -> None:
    typer.echo(f"`duckstring trigger {old}` is deprecated: use `duckstring {new}`.", err=True)


def _cfg(catchment: Optional[str]) -> dict:
    from .config import resolve_catchment
    _, cfg = resolve_catchment(catchment)
    return cfg


def _one_shot(name: str, outlet: str, catchment, major, version, silent: bool, watch: bool) -> None:
    _deprecated(f"{name} {outlet}", f"refresh {outlet}")
    send_and_watch(_cfg(catchment), outlet, major, version, silent, watch, "refresh", {}, "Refresh sent.",
                   one_shot=True)


@app.command("tap")
def tap(outlet: str = typer.Argument(...), catchment: Optional[str] = _CATCHMENT, major: Optional[int] = _MAJOR,
        version: Optional[str] = _VERSION, silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
        watch: bool = typer.Option(False, "--watch", help=_WATCH_HELP)) -> None:
    """Deprecated: `duckstring refresh POND`."""
    _one_shot("tap", outlet, catchment, major, version, silent, watch)


@app.command("pulse")
def pulse(outlet: str = typer.Argument(...), catchment: Optional[str] = _CATCHMENT, major: Optional[int] = _MAJOR,
          version: Optional[str] = _VERSION, silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP),
          watch: bool = typer.Option(False, "--watch", help=_WATCH_HELP)) -> None:
    """Deprecated: `duckstring refresh POND`."""
    _one_shot("pulse", outlet, catchment, major, version, silent, watch)


@app.command("wave")
def wave(outlet: str = typer.Argument(...), catchment: Optional[str] = _CATCHMENT, major: Optional[int] = _MAJOR,
         version: Optional[str] = _VERSION, silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP)) -> None:
    """Deprecated: `duckstring refresh add POND --continuous`."""
    _deprecated(f"wave {outlet}", f"refresh add {outlet} --continuous")
    send_and_watch(_cfg(catchment), outlet, major, version, silent, False, "refreshes", {"continuous": True},
                   "Continuous Refresh added.", one_shot=False)


@app.command("tide")
def tide(outlet: str = typer.Argument(...), bound: str = typer.Argument(...), catchment: Optional[str] = _CATCHMENT,
         major: Optional[int] = _MAJOR, version: Optional[str] = _VERSION,
         silent: bool = typer.Option(False, "--silent", help=_SILENT_HELP)) -> None:
    """Deprecated: `duckstring refresh add POND --every BOUND`."""
    from .window import _parse_duration

    seconds = _parse_duration(bound)
    _deprecated(f"tide {outlet} {bound}", f"refresh add {outlet} --every {bound}")
    send_and_watch(_cfg(catchment), outlet, major, version, silent, False, "refreshes", {"every_seconds": seconds},
                   "Refresh added.", one_shot=False)


@app.command("remove")
def remove(outlet: str = typer.Argument(...), catchment: Optional[str] = _CATCHMENT, major: Optional[int] = _MAJOR,
           version: Optional[str] = _VERSION) -> None:
    """Deprecated: `duckstring refresh rm POND NAME`."""
    from . import _http

    _deprecated(f"remove {outlet}", f"refresh rm {outlet} NAME")
    cfg = _cfg(catchment)
    _http.post(f"{cfg['url']}/api/ponds/{outlet}/untrigger", auth=cfg, params=_http.pond_params(major, version),
               json={})
    typer.echo("Floating Refresh removed.")
