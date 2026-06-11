from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Optional

import typer

_SKIP_DIRS = {
    ".git", ".venv", "__pycache__", ".ruff_cache", ".pytest_cache",
    "dist", ".next", "node_modules", ".mypy_cache",
}
_SKIP_EXTS = {".pyc", ".pyo"}


def _read_pond_toml(cwd: Path) -> dict:
    from ..core import read_pond_toml

    if not (cwd / "pond.toml").exists():
        typer.echo("Error: no pond.toml found in the current directory.", err=True)
        typer.echo("Are you in a Pond project root? Run 'duckstring pond init <name>' to create one.", err=True)
        raise typer.Exit(1)
    return read_pond_toml(cwd)


def _zip_pond(cwd: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fpath in sorted(cwd.rglob("*")):
            if fpath.is_dir():
                continue
            rel = fpath.relative_to(cwd)
            parts = rel.parts
            # Skip hidden dirs, known noise dirs, and bad extensions
            if any(p in _SKIP_DIRS or (p.startswith(".") and p not in {".gitignore"}) for p in parts[:-1]):
                continue
            if fpath.suffix in _SKIP_EXTS:
                continue
            zf.write(fpath, rel)
    return buf.getvalue()


def _deploy_one(
    console,
    pond_dir: Path,
    url: str,
    catchment_name: str,
    git: Optional[str],
    yes: bool,
) -> bool:
    """Deploy a single pond directory. Returns True on success, False if skipped."""
    from . import _http

    info = _read_pond_toml(pond_dir)
    pond_section = info.get("pond", {})
    name = pond_section.get("name", "unknown")
    version = pond_section.get("version", "0.0.0")
    pond_type = pond_section.get("type", "pond")

    try:
        import httpx as _httpx
        _r = _httpx.get(f"{url}/api/ponds/{name}/versions/{version}", timeout=5.0)
        if _r.status_code == 200:
            version_exists: bool | None = True
        elif _r.status_code == 404:
            version_exists = False
        else:
            version_exists = None
    except Exception:
        version_exists = None

    mode = f"git:{git}" if git else "local"
    console.print(f"Deploying [bold]{name}[/bold] v[bold]{version}[/bold] ([dim]{mode}[/dim]) → [bold]{catchment_name}[/bold]")
    if version_exists is True:
        console.print("[yellow]A Pond with the same name and version currently exists and will be overwritten.[/yellow]")
    elif version_exists is False:
        console.print("[dim]New version — no conflicts.[/dim]")
    else:
        console.print("[dim]Could not check for conflicts — proceed with care.[/dim]")

    if not yes:
        confirmed = typer.confirm("Do you wish to proceed?", default=True)
        if not confirmed:
            console.print("[dim]Skipped.[/dim]")
            return False

    if git:
        import subprocess

        try:
            repo_url = subprocess.check_output(
                ["git", "remote", "get-url", "origin"], cwd=pond_dir, text=True
            ).strip()
        except subprocess.CalledProcessError:
            typer.echo("Error: could not read git remote 'origin'. Is this a git repo with a remote?", err=True)
            raise typer.Exit(1) from None

        _http.post(
            f"{url}/api/deploy",
            json={"name": name, "version": version, "type": pond_type, "git_ref": git, "repo_url": repo_url},
        )
    else:
        archive = _zip_pond(pond_dir)
        _http.post(
            f"{url}/api/deploy",
            files={"pond": ("pond.zip", archive, "application/zip")},
            data={"name": name, "version": version, "type": pond_type},
            timeout=120,
        )

    console.print(f"[green]Deployed[/green] [bold]{name}@{version}[/bold] to [bold]{catchment_name}[/bold].")
    return True


def deploy(
    catchment: Optional[str] = typer.Option(
        None, "--catchment", "-c", help="Catchment to deploy to (uses default if omitted)."
    ),
    git: Optional[str] = typer.Option(None, "--git", help="Deploy from a git ref (branch, commit, or tag)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompts."),
    all_ponds: bool = typer.Option(False, "--all", help="Deploy all Ponds found in subdirectories of the current directory."),
) -> None:
    """Deploy the current Pond project to a Catchment."""
    from rich.console import Console

    from .config import resolve_catchment

    console = Console()
    catchment_name, cfg = resolve_catchment(catchment)
    url = cfg["url"]

    if all_ponds:
        cwd = Path.cwd()
        pond_dirs = sorted(
            d for d in cwd.iterdir()
            if d.is_dir() and (d / "pond.toml").exists()
        )
        if not pond_dirs:
            typer.echo("No pond.toml files found in any subdirectory.", err=True)
            raise typer.Exit(1)
        console.print(f"Found [bold]{len(pond_dirs)}[/bold] pond(s): {', '.join(d.name for d in pond_dirs)}")
        for pond_dir in pond_dirs:
            console.rule(pond_dir.name)
            _deploy_one(console, pond_dir, url, catchment_name, git, yes)
    else:
        _deploy_one(console, Path.cwd(), url, catchment_name, git, yes)
