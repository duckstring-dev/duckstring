from __future__ import annotations

import io
import sys
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
    toml_path = cwd / "pond.toml"
    if not toml_path.exists():
        typer.echo("Error: no pond.toml found in the current directory.", err=True)
        typer.echo("Are you in a Pond project root? Run 'duckstring pond init <name>' to create one.", err=True)
        raise typer.Exit(1)
    text = toml_path.read_text(encoding="utf-8")
    if sys.version_info >= (3, 11):
        import tomllib
        return tomllib.loads(text)
    import tomli
    return tomli.loads(text)


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


def deploy(
    catchment: Optional[str] = typer.Option(
        None, "--catchment", "-c", help="Catchment to deploy to (uses default if omitted)."
    ),
    git: Optional[str] = typer.Option(None, "--git", help="Deploy from a git ref (branch, commit, or tag)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Deploy the current Pond project to a Catchment."""
    from rich.console import Console

    from . import _http
    from .config import resolve_catchment

    console = Console()
    cwd = Path.cwd()
    catchment_name, cfg = resolve_catchment(catchment)
    url = cfg["url"]

    info = _read_pond_toml(cwd)
    pond_section = info.get("pond", {})
    name = pond_section.get("name", "unknown")
    version = pond_section.get("version", "0.0.0")
    pond_type = pond_section.get("type", "pond")

    # Pre-deploy: check whether this version already exists on the catchment.
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
        typer.confirm("Do you wish to proceed?", default=True, abort=True)

    if git:
        import subprocess

        try:
            repo_url = subprocess.check_output(
                ["git", "remote", "get-url", "origin"], cwd=cwd, text=True
            ).strip()
        except subprocess.CalledProcessError:
            typer.echo("Error: could not read git remote 'origin'. Is this a git repo with a remote?", err=True)
            raise typer.Exit(1) from None

        _http.post(
            f"{url}/api/deploy",
            json={"name": name, "version": version, "type": pond_type, "git_ref": git, "repo_url": repo_url},
        )
    else:
        archive = _zip_pond(cwd)
        _http.post(
            f"{url}/api/deploy",
            files={"pond": ("pond.zip", archive, "application/zip")},
            data={"name": name, "version": version, "type": pond_type},
            timeout=120,
        )

    console.print(f"[green]Deployed[/green] [bold]{name}@{version}[/bold] to [bold]{catchment_name}[/bold].")
