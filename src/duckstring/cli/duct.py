"""Cross-Catchment commands under ``duckstring catchment``:

* ``open`` / ``close`` — producer side: expose a Pond to demand from any source (+ tap-on-get).
* ``duct create/destroy/sync/ls/add/remove`` — consumer side: open a conduit into an upstream
  Catchment and choose which of its Ponds to draw.

A duct lives on the *consuming* Catchment (``-c`` / default). ``duct create {upstream}`` forwards the
upstream's registration (URL + auth headers) so the consumer can dial it. See
plans/cross-catchment-ducts.md.
"""

from __future__ import annotations

from typing import Optional

import typer

_CATCHMENT = typer.Option(None, "--catchment", "-c", help="Consuming Catchment (uses default if omitted).")
_MAJOR = typer.Option(None, "--major", "-m", help="Major version to target (default: latest).")
_VERSION = typer.Option(None, "--version", "-v", help="Specific semver to target, e.g. 1.2.3.")


# ─── Producer side: open / close ──────────────────────────────────────────────


def open_pond(
    pond: str = typer.Argument(..., help="Pond to open to demand from other Catchments."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
    tap_on_get: bool = typer.Option(False, "--tap-on-get", help="Also send a Tap on every query of the Pond, after "
                                                                "serving it."),
) -> None:
    """Open a Pond to demand from other Catchments."""
    from . import _http
    from .config import resolve_catchment

    _, cfg = resolve_catchment(catchment)
    _http.post(
        f"{cfg['url']}/api/ponds/{pond}/open", auth=cfg,
        params=_http.pond_params(major, version), json={"tap_on_get": tap_on_get},
    )
    typer.echo(f"Opened '{pond}'{' (tap-on-get)' if tap_on_get else ''}.")


def close_pond(
    pond: str = typer.Argument(..., help="Pond to close."),
    catchment: Optional[str] = _CATCHMENT,
    major: Optional[int] = _MAJOR,
    version: Optional[str] = _VERSION,
) -> None:
    """Close a Pond to demand from other Catchments, and stop tapping it on reads."""
    from . import _http
    from .config import resolve_catchment

    _, cfg = resolve_catchment(catchment)
    _http.post(f"{cfg['url']}/api/ponds/{pond}/close", auth=cfg, params=_http.pond_params(major, version))
    typer.echo(f"Closed '{pond}'.")


# ─── Consumer side: ducts ─────────────────────────────────────────────────────

app = typer.Typer(help="Consume Ponds from other Catchments.", no_args_is_help=True)


def _resolve_pair(upstream: str, consumer: Optional[str]):
    """The consuming Catchment's name+config (where the duct lives) and the upstream's registration
    (URL + forwarded creds)."""
    from .config import auth_headers, resolve_catchment

    consumer_name, consumer_cfg = resolve_catchment(consumer)
    _, up_cfg = resolve_catchment(upstream)
    return consumer_name, consumer_cfg, up_cfg["url"], auth_headers(up_cfg)


@app.command("create")
def create(
    upstream: str = typer.Argument(..., help="Registered upstream Catchment to draw from."),
    catchment: Optional[str] = _CATCHMENT,
    sync: bool = typer.Option(False, "--sync", help="Also draw every Pond the upstream currently exposes."),
) -> None:
    """Create a duct from an upstream Catchment into this one."""
    from . import _http

    consumer_name, consumer_cfg, up_url, up_headers = _resolve_pair(upstream, catchment)
    # Record the upstream's stable identity (resolves cross-mesh edges + cuts cycles in the lineage
    # view). Reachability at create time is reasonable to require for a duct.
    upstream_id = _http.get(f"{up_url}/api/catchment/identity", auth={"headers": up_headers}).json().get("id")
    _http.post(
        f"{consumer_cfg['url']}/api/duct", auth=consumer_cfg,
        json={"origin": upstream, "remote_url": up_url, "auth_headers": up_headers or None,
              "upstream_id": upstream_id},
    )
    typer.echo(f"Duct created: '{upstream}' → '{consumer_name}'.")
    if sync:
        _sync(consumer_cfg, upstream, consumer_name)


@app.command("destroy")
def destroy(
    upstream: str = typer.Argument(..., help="Upstream the duct draws from."),
    catchment: Optional[str] = _CATCHMENT,
) -> None:
    """Remove a duct and every Pond Draw it created."""
    from . import _http
    from .config import resolve_catchment

    _, cfg = resolve_catchment(catchment)
    _http.delete(f"{cfg['url']}/api/duct/{upstream}", auth=cfg)
    typer.echo(f"Duct from '{upstream}' destroyed.")


@app.command("sync")
def sync(
    upstream: str = typer.Argument(..., help="Upstream the duct draws from."),
    catchment: Optional[str] = _CATCHMENT,
) -> None:
    """Draw every Pond the upstream Catchment exposes."""
    from .config import resolve_catchment

    consumer_name, cfg = resolve_catchment(catchment)
    _sync(cfg, upstream, consumer_name)


def _sync(consumer_cfg: dict, upstream: str, consumer_name: str) -> None:
    from . import _http

    resp = _http.post(f"{consumer_cfg['url']}/api/duct/{upstream}/sync", auth=consumer_cfg)
    added = resp.json().get("added", [])
    typer.echo(f"Synced '{upstream}' → '{consumer_name}' — drawing {len(added)} pond(s): "
               f"{', '.join(added) or '(none)'}.")


@app.command("ls")
def ls(catchment: Optional[str] = _CATCHMENT) -> None:
    """List ducts and the Ponds each one draws."""
    from rich.console import Console
    from rich.table import Table

    from . import _http
    from .config import resolve_catchment

    _, cfg = resolve_catchment(catchment)
    ducts = _http.get(f"{cfg['url']}/api/duct", auth=cfg).json().get("ducts", [])
    if not ducts:
        typer.echo("No ducts.")
        return
    table = Table(show_header=True, header_style="bold dim")
    table.add_column("Upstream", style="bold")
    table.add_column("URL", style="dim")
    table.add_column("Draws")
    for d in ducts:
        draws = ", ".join(f"{p['pond']}@{p['major']}" for p in d["ponds"]) or "(none)"
        table.add_row(d["origin"], d["remote_url"], draws)
    Console().print(table)


@app.command("add")
def add(
    upstream: str = typer.Argument(..., help="Upstream the duct draws from."),
    pond: str = typer.Argument(..., help="Upstream Pond to draw."),
    catchment: Optional[str] = _CATCHMENT,
    major: int = typer.Option(1, "--major", "-m", help="Major line of the upstream Pond."),
    incremental: bool = typer.Option(False, "--incremental", help="Reserved; not implemented yet."),
) -> None:
    """Draw one upstream Pond over the duct."""
    from . import _http
    from .config import resolve_catchment

    _, cfg = resolve_catchment(catchment)
    _http.post(
        f"{cfg['url']}/api/duct/{upstream}/ponds", auth=cfg,
        json={"pond": pond, "major": major, "incremental": incremental},
    )
    typer.echo(f"Drawing '{pond}@{major}' from '{upstream}'.")


@app.command("remove")
def remove(
    upstream: str = typer.Argument(..., help="Upstream the duct draws from."),
    pond: str = typer.Argument(..., help="Drawn Pond to stop drawing."),
    catchment: Optional[str] = _CATCHMENT,
    major: int = typer.Option(1, "--major", "-m", help="Major line of the drawn Pond."),
) -> None:
    """Stop drawing a Pond and remove its Pond Draw."""
    from . import _http
    from .config import resolve_catchment

    _, cfg = resolve_catchment(catchment)
    _http.delete(f"{cfg['url']}/api/duct/{upstream}/ponds/{pond}", auth=cfg, params={"major": major})
    typer.echo(f"Stopped drawing '{pond}@{major}' from '{upstream}'.")
