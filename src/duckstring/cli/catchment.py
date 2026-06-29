from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(help="Work with Catchments.", add_completion=False, no_args_is_help=True)


def _offer_default(name: str, yes: bool) -> None:
    """Prompt to set name as the default catchment, unless it already is."""
    from .config import get_default_catchment, set_default_catchment

    if get_default_catchment() == name:
        return
    if yes or typer.confirm(f"Set '{name}' as default catchment?", default=True):
        set_default_catchment(name)
        typer.echo(f"Default catchment set to '{name}'.")


def _has_key_ladder(root: Path) -> bool:
    """Whether tiered read/demand/full keys are stored in this Catchment's database."""
    from duckstring.catchment.db import connect

    db = root / "duck.db"
    if not db.exists():
        return False
    con = connect(db)
    try:
        row = con.execute(
            "SELECT 1 FROM catchment_key LIMIT 1"
        ).fetchone()
    except Exception:  # table not migrated yet
        row = None
    finally:
        con.close()
    return row is not None


def _generate_ladder(root: Path) -> dict[str, str]:
    """Mint the three-tier key ladder into a Catchment's database, returning the plaintext once."""
    from duckstring.catchment import auth
    from duckstring.catchment.db import connect, migrate

    root.mkdir(parents=True, exist_ok=True)
    con = connect(root / "duck.db")
    try:
        migrate(con)
        return auth.generate(con)
    finally:
        con.close()


_LEVEL_BLURB = {
    "full": "deploy, control, ducts, rotate",
    "demand": "read + create demand + downstream duct",
    "read": "read & query only",
}


def _print_keys_panel(name: str, keys: dict[str, str], url: str | None = None) -> None:
    from rich.console import Console
    from rich.panel import Panel

    rows = "\n".join(
        f"  [bold]{lvl:<6}[/bold] [cyan]{keys[lvl]}[/cyan]   [dim]{_LEVEL_BLURB[lvl]}[/dim]"
        for lvl in ("full", "demand", "read")
        if lvl in keys
    )
    connect_hint = (
        f"\n\n  Give the [bold]demand[/bold] key to a downstream operator:\n"
        f"  [dim]duckstring catchment connect --name {name} --path {url} --key {keys['demand']}[/dim]"
        if url and "demand" in keys else ""
    )
    stored = (
        f"\n\n  The [bold]full[/bold] key is stored against '{name}' in [dim]~/.duckstring/config.toml[/dim]; "
        "the others are not — copy them now."
        if "full" in keys else ""
    )
    Console().print(
        Panel(
            "[bold white]Access keys[/bold white]  [dim](shown once — store them now)[/dim]\n\n"
            f"{rows}{connect_hint}{stored}",
            border_style="cyan",
            padding=(1, 2),
        )
    )


def _launch(name: str, url: str, root: Path, key: str | None = None) -> None:
    from urllib.parse import urlparse

    import uvicorn
    from rich.console import Console
    from rich.panel import Panel

    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 7474

    auth_line = "API key required" if (key or _has_key_ladder(root)) else "open (no API key)"
    console = Console()
    console.print(
        Panel(
            f"[bold white]duckstring catchment[/bold white] [bold cyan]{name}[/bold cyan]\n\n"
            f"  [dim]url:  {url}[/dim]\n"
            f"  [dim]root: {root}[/dim]\n"
            f"  [dim]auth: {auth_line}[/dim]\n\n"
            f"  Press [bold]Ctrl-C[/bold] to stop.",
            border_style="bright_black",
            padding=(1, 2),
        )
    )
    from duckstring.catchment.app import create_app

    # Ducks dial back to the actual bind address (a wildcard bind is dialled via loopback).
    dial_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    uvicorn.run(
        create_app(root, api_key=key, base_url=f"http://{dial_host}:{port}", name=name),
        host=host, port=port, reload=False, log_level="warning",
    )


def _register_or_abort(
    name: str, url: str, kind: str, root: str | None = None, key: str | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    """Call register_catchment, printing a friendly error on conflict."""
    from .config import CatchmentConflict, register_catchment

    try:
        register_catchment(name, url=url, kind=kind, root=root, key=key, headers=headers)
    except CatchmentConflict as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


def _parse_headers(values: Optional[list[str]]) -> Optional[dict[str, str]]:
    """``["Name: value", ...]`` → a headers dict; friendly error on a malformed entry."""
    if not values:
        return None
    headers: dict[str, str] = {}
    for raw in values:
        hname, sep, hval = raw.partition(":")
        if not sep or not hname.strip() or not hval.strip():
            typer.echo(f"Error: invalid --header {raw!r} — use 'Name: value'.", err=True)
            raise typer.Exit(1)
        headers[hname.strip()] = hval.strip()
    return headers


_HEADER_HELP = (
    "Extra header attached to every request to this Catchment, as 'Name: value' (repeatable). "
    "For auth handled by the platform in front of the Catchment, e.g. 'Authorization: Key …' "
    "for Posit Connect."
)


@app.command()
def init(
    name: str = typer.Option(..., "--name", "-n", prompt="Catchment name", help="Name to register this Catchment under."),
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind to."),
    port: int = typer.Option(7474, "--port", "-p", help="Port to listen on."),
    root: Optional[Path] = typer.Option(None, "--root", help="Root directory for Catchment data."),
    key: Optional[str] = typer.Option(
        None, "--key", help="A single full-access API key the server requires (and the CLI then sends)."
    ),
    generate_key: bool = typer.Option(
        False, "--generate-key",
        help="Generate the three-tier key ladder (read/demand/full), print them once, and start the "
             "server. The full key is stored in the registration so `catchment start` reuses it. "
             "Mutually exclusive with --key.",
    ),
    header: Optional[list[str]] = typer.Option(None, "--header", help=_HEADER_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="Automatically set as default catchment."),
) -> None:
    """Create and register a new local Catchment, then start the server."""
    from .config import CONFIG_DIR, list_catchments

    if generate_key and key:
        typer.echo("Error: --generate-key and --key are mutually exclusive — the generated full key IS the key.",
                   err=True)
        raise typer.Exit(1)

    headers = _parse_headers(header)
    root_dir = Path(root) if root else CONFIG_DIR / name
    url = f"http://{host}:{port}"

    if generate_key:
        keys = _generate_ladder(root_dir)
        key = keys["full"]  # the operator's own CLI gets full access; the others are handed out
        _print_keys_panel(name, keys, url)

    existing = dict(list_catchments()).get(name)
    if existing:
        existing_root = existing.get("root", str(CONFIG_DIR / name))
        key = key or existing.get("key")
        headers = headers or existing.get("headers")
        if existing_root == str(root_dir):
            typer.echo(f"Catchment '{name}' already registered at {root_dir}.")
            if key != existing.get("key") or headers != existing.get("headers"):
                _register_or_abort(name, url=url, kind="local", root=str(root_dir), key=key, headers=headers)
        else:
            typer.echo(f"Catchment '{name}' is already registered with data at: {existing_root}")
            if not typer.confirm(f"Update root to {root_dir}?", default=False):
                raise typer.Exit(0)
            root_dir.mkdir(parents=True, exist_ok=True)
            _register_or_abort(name, url=url, kind="local", root=str(root_dir), key=key, headers=headers)
    else:
        root_dir.mkdir(parents=True, exist_ok=True)
        _register_or_abort(name, url=url, kind="local", root=str(root_dir), key=key, headers=headers)
        _offer_default(name, yes)

    _launch(name, url, root_dir, key)


@app.command()
def start(
    name: str = typer.Argument(..., help="Name of the registered local Catchment to start."),
) -> None:
    """Start the server for a registered local Catchment."""
    from .config import list_catchments

    registered = dict(list_catchments())
    if name not in registered:
        typer.echo(f"Error: no catchment '{name}' registered.", err=True)
        typer.echo(f"  duckstring catchment init --name {name}", err=True)
        raise typer.Exit(1)

    cfg = registered[name]
    if cfg.get("type") != "local":
        typer.echo(f"Error: '{name}' is a remote catchment and cannot be started locally.", err=True)
        raise typer.Exit(1)

    url = cfg["url"]
    root_dir = Path(cfg["root"]) if cfg.get("root") else Path.home() / ".duckstring" / name
    _launch(name, url, root_dir, cfg.get("key"))


@app.command(name="rotate-keys")
def rotate_keys(
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to rotate (uses default if omitted)."),
    level: Optional[list[str]] = typer.Option(
        None, "--level", help="Level(s) to reroll: read/demand/full (repeatable). Default: all three."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
) -> None:
    """Reroll a Catchment's access keys, printing the new ones once. Rerolling invalidates the old key
    for each rotated level; the internal Duck token is untouched (running Ducks keep working). Requires
    a full-access key on the registration."""
    from . import _http
    from .config import resolve_catchment, update_catchment_key

    cname, cfg = resolve_catchment(catchment)
    levels = list(level) if level else None
    target = ", ".join(levels) if levels else "all (read, demand, full)"
    if not yes:
        typer.confirm(f"Reroll the {target} key(s) for '{cname}'? Old keys stop working.", default=False, abort=True)

    resp = _http.post(f"{cfg['url']}/api/catchment/keys/rotate", auth=cfg, json={"levels": levels}).json()
    keys = resp["keys"]
    # Keep the operator's own CLI working: if the full key was rerolled, update the stored registration.
    if "full" in keys:
        update_catchment_key(cname, keys["full"])
    _print_keys_panel(cname, keys)


@app.command()
def connect(
    name: str = typer.Option(..., "--name", "-n", help="Name to register this Catchment under."),
    path: str = typer.Option(..., "--path", help="URL of the remote Catchment server."),
    key: Optional[str] = typer.Option(
        None, "--key", help="API key the server requires; sent with every request to this Catchment."
    ),
    header: Optional[list[str]] = typer.Option(None, "--header", help=_HEADER_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="Automatically set as default catchment."),
) -> None:
    """Register a remote Catchment server by name."""
    from rich.console import Console

    _register_or_abort(name, url=path, kind="remote", key=key, headers=_parse_headers(header))
    console = Console()
    console.print(f"[green]Registered[/green] catchment [bold]{name}[/bold] → {path}")
    _offer_default(name, yes)


@app.command(name="list")
def list_cmd() -> None:
    """List all registered Catchments."""
    from rich.console import Console
    from rich.table import Table

    from .config import get_default_catchment, list_catchments

    items = list_catchments()
    if not items:
        typer.echo("No catchments registered.")
        typer.echo("  duckstring catchment init")
        typer.echo("  duckstring catchment connect --name <name> --path <url>")
        return

    default = get_default_catchment()
    # If no explicit default but only one catchment, that one is implicitly default.
    if default is None and len(items) == 1:
        default = items[0][0]

    console = Console()
    table = Table(show_header=True, header_style="bold dim")
    table.add_column("", width=1)
    table.add_column("Name", style="bold")
    table.add_column("Type")
    table.add_column("URL")
    table.add_column("Root", style="dim")

    for n, cfg in items:
        marker = "[green]●[/green]" if n == default else ""
        table.add_row(marker, n, cfg.get("type", "?"), cfg.get("url", "?"), cfg.get("root", ""))

    console.print(table)


@app.command()
def disconnect(
    name: str = typer.Argument(..., help="Name of the registered Catchment to remove."),
    purge: bool = typer.Option(False, "--purge", help="Delete the local data directory without prompting."),
) -> None:
    """Remove a registered Catchment."""
    from .config import list_catchments, unregister_catchment

    registered = dict(list_catchments())
    if name not in registered:
        typer.echo(f"Error: no catchment '{name}' registered.", err=True)
        raise typer.Exit(1)

    cfg = registered[name]
    root = cfg.get("root")

    if root:
        import shutil
        from pathlib import Path as _Path

        root_path = _Path(root)
        delete = purge or typer.confirm(f"Delete data directory at {root}?", default=False)
        if delete:
            if root_path.exists():
                shutil.rmtree(root_path)
                typer.echo(f"Deleted data directory: {root_path}")
            else:
                typer.echo(f"Data directory not found (skipping): {root_path}")
        else:
            typer.echo(f"Data directory retained: {root_path}")

    unregister_catchment(name)
    typer.echo(f"Disconnected catchment '{name}'.")


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


@app.command()
def download(
    catchment: Optional[str] = typer.Option(None, "--catchment", "-c", help="Catchment to download (uses default if omitted)."),
    path: Path = typer.Option(
        Path(".duckstring"), "--path",
        help="Destination directory for the Catchment root (default ./.duckstring — drops straight "
             "into a platform deploy bundle).",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the size confirmation."),
) -> None:
    """Download a Catchment's entire state (its root: database, artifacts, data, ledgers) into a
    local directory — e.g. to carry state across a platform redeploy, or as a backup."""
    import tarfile
    import tempfile

    import httpx
    from rich.console import Console
    from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TransferSpeedColumn

    from . import _http
    from .config import auth_headers, resolve_catchment

    cname, cfg = resolve_catchment(catchment)
    url = cfg["url"]

    use = _http.get(f"{url}/api/catchment/usage", auth=cfg).json()
    size_str = _fmt_bytes(use["total_bytes"])
    console = Console()
    console.print(
        f"Catchment [bold]{cname}[/bold] holds [bold]{size_str}[/bold] "
        f"({use['file_count']} files) → [bold]{path}[/bold]"
        + (" [yellow](exists — contents will be overwritten where names collide)[/yellow]"
           if path.exists() and any(path.iterdir()) else "")
    )
    if not yes:
        typer.confirm("Download?", default=True, abort=True)

    # Stream to a temp tar with a progress bar, then extract — so a broken transfer never leaves a
    # half-written root.
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        try:
            with httpx.stream(
                "GET", f"{url}/api/catchment/archive", headers=auth_headers(cfg),
                timeout=httpx.Timeout(None, connect=5.0),
            ) as resp:
                resp.raise_for_status()
                with Progress(
                    TextColumn("[progress.description]{task.description}"), BarColumn(),
                    DownloadColumn(), TransferSpeedColumn(), console=console,
                ) as progress:
                    task = progress.add_task("Downloading", total=use["archive_bytes"])
                    for chunk in resp.iter_raw():
                        tmp.write(chunk)
                        progress.update(task, advance=len(chunk))
            tmp.close()
            path.mkdir(parents=True, exist_ok=True)
            with tarfile.open(tmp_path, mode="r") as tar:
                try:
                    tar.extractall(path, filter="data")
                except TypeError:  # Python without the extraction-filter backport
                    tar.extractall(path)
        except httpx.HTTPStatusError as exc:
            typer.echo(f"Error: {exc.response.status_code} from Catchment", err=True)
            raise typer.Exit(1) from None
        except httpx.HTTPError as exc:
            typer.echo(f"Error: download failed — {exc}", err=True)
            raise typer.Exit(1) from None
        finally:
            tmp_path.unlink(missing_ok=True)

    console.print(f"[green]Downloaded[/green] catchment [bold]{cname}[/bold] → {path}")


@app.command(name="set-default")
def set_default(
    name: str = typer.Argument(..., help="Name of the registered Catchment to use as default."),
) -> None:
    """Set the default Catchment used when none is specified on a command."""
    from .config import list_catchments, set_default_catchment

    registered = {n for n, _ in list_catchments()}
    if name not in registered:
        typer.echo(f"Error: no catchment '{name}' registered.", err=True)
        raise typer.Exit(1)
    set_default_catchment(name)
    typer.echo(f"Default catchment set to '{name}'.")
