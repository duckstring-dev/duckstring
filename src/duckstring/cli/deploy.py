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
    catchment: str = typer.Argument(..., help="Name of the registered Catchment to deploy to."),
    git: Optional[str] = typer.Option(None, "--git", help="Deploy from a git ref (branch, commit, or tag)."),
) -> None:
    """Deploy the current Pond project to a Catchment."""
    from rich.console import Console

    from . import _http
    from .config import resolve_catchment

    console = Console()
    cwd = Path.cwd()
    cfg = resolve_catchment(catchment)
    url = cfg["url"]

    info = _read_pond_toml(cwd)
    pond_section = info.get("pond", {})
    name = pond_section.get("name", "unknown")
    version = pond_section.get("version", "0.0.0")
    pond_type = pond_section.get("type", "pond")

    if git:
        import subprocess

        try:
            repo_url = subprocess.check_output(
                ["git", "remote", "get-url", "origin"], cwd=cwd, text=True
            ).strip()
        except subprocess.CalledProcessError:
            typer.echo("Error: could not read git remote 'origin'. Is this a git repo with a remote?", err=True)
            raise typer.Exit(1) from None

        console.print(f"Deploying [bold]{name}@{version}[/bold] ([dim]git:{git}[/dim]) → [bold]{catchment}[/bold]...")
        _http.post(
            f"{url}/api/deploy",
            json={"name": name, "version": version, "type": pond_type, "git_ref": git, "repo_url": repo_url},
        )
    else:
        console.print(f"Deploying [bold]{name}@{version}[/bold] ([dim]local[/dim]) → [bold]{catchment}[/bold]...")
        archive = _zip_pond(cwd)
        _http.post(
            f"{url}/api/deploy",
            files={"pond": ("pond.zip", archive, "application/zip")},
            data={"name": name, "version": version, "type": pond_type},
            timeout=120,
        )

    console.print(f"[green]Deployed[/green] [bold]{name}@{version}[/bold] to [bold]{catchment}[/bold].")
