from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

import typer

from duckstring import Basin


app = typer.Typer(
    help="Work with basins.",
    add_completion=False,
    invoke_without_command=True,
    context_settings={"allow_interspersed_args": True},
)


def _repo_root() -> Path:
    return Path.cwd().resolve()


def _basins_dir(root: Path) -> Path:
    return root / "basins"


def _list_basin_names(root: Path) -> list[str]:
    basins_dir = _basins_dir(root)
    if not basins_dir.exists():
        return []
    out: list[str] = []
    for entry in basins_dir.iterdir():
        if entry.is_dir() and not entry.name.startswith("."):
            out.append(entry.name)
    return sorted(out)


def _resolve_basin_dir(root: Path, basin_name: str) -> Path:
    basin_dir = _basins_dir(root) / basin_name
    if not basin_dir.exists() or not basin_dir.is_dir():
        raise FileNotFoundError(f"Unknown basin {basin_name!r}.")
    return basin_dir


def _resolve_spec_path(basin_dir: Path, spec: Optional[str]) -> Path:
    if spec is None:
        return basin_dir / "basin.json"
    p = Path(spec).expanduser()
    if not p.is_absolute():
        p = basin_dir / p
    return p.resolve()


def _load_spec(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _basin_ctx(ctx: typer.Context) -> Optional[dict]:
    if ctx.obj:
        return ctx.obj
    if ctx.parent and ctx.parent.obj:
        return ctx.parent.obj
    if ctx.parent:
        basin_name = ctx.parent.params.get("basin_name")
        if isinstance(basin_name, str) and basin_name:
            try:
                basin_dir = _resolve_basin_dir(_repo_root(), basin_name)
            except FileNotFoundError:
                return None
            return {"basin_name": basin_name, "basin_dir": basin_dir, "root": _repo_root()}
    return None


def _complete_basin_names(ctx: typer.Context, param: typer.CallbackParam, incomplete: str) -> list[str]:
    root = _repo_root()
    candidates = _list_basin_names(root)
    return [c for c in candidates if c.startswith(incomplete)]


def _complete_spec_path(ctx: typer.Context, param: typer.CallbackParam, incomplete: str) -> list[str]:
    info = _basin_ctx(ctx)
    if not info:
        return []
    basin_dir = info["basin_dir"]
    base = basin_dir
    prefix = incomplete
    rel_prefix: Optional[str] = None
    if "/" in incomplete:
        prefix_path = Path(incomplete)
        base = basin_dir / prefix_path.parent
        prefix = prefix_path.name
        rel_prefix = str(prefix_path.parent)
    if not base.exists():
        return []
    candidates = [p.name for p in base.glob("*.json")]
    if rel_prefix and rel_prefix != ".":
        return [f"{rel_prefix}/{c}" for c in candidates if c.startswith(prefix)]
    return [c for c in candidates if c.startswith(prefix)]


def _print_summary(basin_name: str, basin_dir: Path, spec_path: Path) -> None:
    if not spec_path.exists():
        typer.echo(f"Spec not found: {spec_path}", err=True)
        raise typer.Exit(code=2)
    data = _load_spec(spec_path)

    name = data.get("name") or basin_name
    mode = data.get("mode", "pulse")
    catchment = data.get("catchment")
    catchment_path = None
    if isinstance(catchment, dict):
        catchment_path = catchment.get("path")
    elif isinstance(catchment, str):
        catchment_path = catchment

    outlets = data.get("outlets") if isinstance(data.get("outlets"), dict) else {}
    hydrated = data.get("hydrated") if isinstance(data.get("hydrated"), dict) else {}
    hydrated_ponds = hydrated.get("ponds") if isinstance(hydrated.get("ponds"), dict) else {}
    stages = hydrated.get("stages") if isinstance(hydrated.get("stages"), list) else []
    hydrated_ok = bool(hydrated_ponds)

    typer.echo(f"Basin: {name}")
    typer.echo(f"Spec: {spec_path}")
    typer.echo(f"Mode: {mode}")
    typer.echo(f"Catchment: {catchment_path or '<none>'}")
    typer.echo(f"Hydrated: {'yes' if hydrated_ok else 'no'}")
    if hydrated_ok:
        typer.echo(f"  Ponds: {len(hydrated_ponds)}")
        typer.echo(f"  Stages: {len(stages)}")

    if outlets:
        typer.echo("Outlets:")
        for pond, version in outlets.items():
            typer.echo(f"  - {pond}: {version}")
    else:
        typer.echo("Outlets: <none>")


@app.callback(invoke_without_command=True)
def basin(
    ctx: typer.Context,
    basin_name: Optional[str] = typer.Argument(
        None,
        help="Basin name",
        shell_complete=_complete_basin_names,
    ),
) -> None:
    root = _repo_root()
    if basin_name is None:
        if ctx.invoked_subcommand:
            typer.echo("Error: Missing basin name.", err=True)
            raise typer.Exit(code=2)
        basins = _list_basin_names(root)
        if basins:
            typer.echo("Basins:")
            for name in basins:
                typer.echo(f"  - {name}")
        else:
            typer.echo(f"No basins found in {(_basins_dir(root))}")
        return

    if basin_name.startswith("-"):
        typer.echo("Error: Basin name must not start with '-'.", err=True)
        raise typer.Exit(code=2)

    try:
        basin_dir = _resolve_basin_dir(root, basin_name)
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        basins = _list_basin_names(root)
        if basins:
            typer.echo("Available basins:", err=True)
            for name in basins:
                typer.echo(f"  - {name}", err=True)
        raise typer.Exit(code=2) from exc

    ctx.obj = {
        "root": root,
        "basin_name": basin_name,
        "basin_dir": basin_dir,
    }

    if ctx.invoked_subcommand is None:
        spec_path = _resolve_spec_path(basin_dir, None)
        _print_summary(basin_name, basin_dir, spec_path)


@app.command()
def hydrate(
    ctx: typer.Context,
    spec: Optional[str] = typer.Option(
        None,
        "--spec",
        "-s",
        help="Path to basin spec (default: basin.json; relative to basin dir if not absolute).",
        shell_complete=_complete_spec_path,
    ),
) -> None:
    info = _basin_ctx(ctx)
    if not info:
        typer.echo("Error: Missing basin name.", err=True)
        raise typer.Exit(code=2)

    basin_dir = info["basin_dir"]
    spec_path = _resolve_spec_path(basin_dir, spec)
    if not spec_path.exists():
        typer.echo(f"Spec not found: {spec_path}", err=True)
        raise typer.Exit(code=2)

    basin = Basin.load(str(spec_path))
    basin.hydrate()
    basin.save(str(spec_path))
    typer.echo(f"Wrote hydrated {spec_path}")


@app.command()
def pulse(
    ctx: typer.Context,
    spec: Optional[str] = typer.Option(
        None,
        "--spec",
        "-s",
        help="Path to basin spec (default: basin.json; relative to basin dir if not absolute).",
        shell_complete=_complete_spec_path,
    ),
) -> None:
    info = _basin_ctx(ctx)
    if not info:
        typer.echo("Error: Missing basin name.", err=True)
        raise typer.Exit(code=2)

    basin_dir = info["basin_dir"]
    spec_path = _resolve_spec_path(basin_dir, spec)
    if not spec_path.exists():
        typer.echo(f"Spec not found: {spec_path}", err=True)
        raise typer.Exit(code=2)

    basin = Basin.load(str(spec_path))
    pulse_result = basin.pulse(verbose=True)
    typer.echo(f"Completed pulse {pulse_result} in {pulse_result.duration} seconds")
