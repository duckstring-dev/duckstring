from __future__ import annotations

import json
import re
from pathlib import Path
from textwrap import dedent
from typing import Any, Optional

import typer

from duckstring import Basin, Catchment

app = typer.Typer(
    help="Work with basins.",
    add_completion=False,
    invoke_without_command=True,
)

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_BASIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_OUTLET_OPTION = typer.Option(
    [],
    "--outlet",
    help="Outlet mapping pond=version. Repeat for multiple values.",
)


def _repo_root() -> Path:
    return Path.cwd().resolve()


def _basins_dir(root: Path) -> Path:
    return root / "basins"


def _validate_basin_name(name: str) -> None:
    if not _BASIN_NAME_RE.match(name):
        raise ValueError(
            "Invalid basin name. Use letters, numbers, '.', '_', or '-', and start with an alphanumeric character."
        )


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


def _complete_basin_names(ctx: typer.Context, param: typer.CallbackParam, incomplete: str) -> list[str]:
    _ = ctx
    _ = param
    root = _repo_root()
    candidates = _list_basin_names(root)
    return [c for c in candidates if c.startswith(incomplete)]


def _basin_name_from_ctx(ctx: typer.Context) -> Optional[str]:
    candidate = ctx.params.get("basin_name")
    if isinstance(candidate, str) and candidate:
        return candidate
    if ctx.parent:
        candidate = ctx.parent.params.get("basin_name")
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _complete_spec_path(ctx: typer.Context, param: typer.CallbackParam, incomplete: str) -> list[str]:
    _ = param
    basin_name = _basin_name_from_ctx(ctx)
    if not basin_name:
        return []
    try:
        basin_dir = _resolve_basin_dir(_repo_root(), basin_name)
    except FileNotFoundError:
        return []
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


def _print_basin_list(root: Path) -> None:
    basins = _list_basin_names(root)
    if basins:
        typer.echo("Basins:")
        for name in basins:
            typer.echo(f"  - {name}")
    else:
        typer.echo(f"No basins found in {_basins_dir(root)}")


def _require_basin_dir(root: Path, basin_name: str) -> Path:
    try:
        return _resolve_basin_dir(root, basin_name)
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        basins = _list_basin_names(root)
        if basins:
            typer.echo("Available basins:", err=True)
            for name in basins:
                typer.echo(f"  - {name}", err=True)
        raise typer.Exit(code=2) from exc


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
def basin(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _print_basin_list(_repo_root())


def _prompt_choice(
    message: str,
    choices: tuple[str, ...],
    default: str,
    aliases: Optional[dict[str, str]] = None,
    multiline: bool = False,
) -> str:
    alias_map = aliases or {}
    choices_with_aliases = [f"{choice} ({alias})" for alias, choice in alias_map.items()]
    choice_text = " | ".join(choices_with_aliases if choices_with_aliases else choices)
    if multiline:
        message_split = dedent(message).strip().splitlines()
        message_indented = "\n".join(line for line in message_split)
        typer.echo(f"\n{message_indented}")
        prompt_message = f"({choice_text})"
    else:
        prompt_message = f"{message} ({choice_text})"
    while True:
        value = typer.prompt(prompt_message, default=default, show_default=True).strip().lower()
        value = alias_map.get(value, value)
        if value in choices:
            return value
        valid = list(choices) + sorted(alias_map.keys())
        typer.echo(f"Please choose one of: {', '.join(valid)}", err=True)


def _prompt_non_empty(message: str, *, default: Optional[str] = None) -> str:
    while True:
        if default is None:
            value = typer.prompt(message).strip()
        else:
            value = typer.prompt(message, default=default, show_default=True).strip()
        if value:
            return value
        typer.echo("Value must be non-empty.", err=True)


def _prompt_bool(message: str, *, default: bool) -> bool:
    default_choice = "yes" if default else "no"
    return (
        _prompt_choice(
            message,
            ("yes", "no"),
            default_choice,
            aliases={"y": "yes", "n": "no"},
        )
        == "yes"
    )


def _prompt_semver(message: str, *, default: Optional[str] = None) -> str:
    while True:
        value = _prompt_non_empty(message, default=default)
        if _SEMVER_RE.match(value):
            return value
        typer.echo("Version must use x.y.z semver format.", err=True)


def _resolve_workspace_path(root: Path, path_value: str) -> Path:
    p = Path(path_value).expanduser()
    if not p.is_absolute():
        p = root / p
    return p.resolve()


def _load_catchment_mode_names(root: Path, catchment_path: str) -> list[str]:
    resolved = _resolve_workspace_path(root, catchment_path)
    if not resolved.exists():
        return []
    try:
        catchment = Catchment.load(str(resolved))
    except Exception:
        return []
    mode_names = sorted(catchment.modes.keys())
    return mode_names


def _parse_outlet_args(raw_outlets: list[str]) -> dict[str, str]:
    outlets: dict[str, str] = {}
    for item in raw_outlets:
        if "=" not in item:
            raise ValueError(f"Invalid --outlet value {item!r}; expected pond=version.")
        pond_name, version = item.split("=", 1)
        pond_name = pond_name.strip()
        version = version.strip()
        if not pond_name:
            raise ValueError(f"Invalid --outlet value {item!r}; pond name must be non-empty.")
        if not _SEMVER_RE.match(version):
            raise ValueError(f"Invalid --outlet value {item!r}; version must use x.y.z format.")
        outlets[pond_name] = version
    return outlets


def _interactive_create(
    root: Path,
    *,
    default_basin_name: Optional[str],
    default_catchment_path: str,
    default_mode: str,
) -> tuple[str, str, str, dict[str, str], str]:
    typer.echo("-- Basin: Interactive Create --")
    typer.echo("This flow writes basins/{name}/basin.json with optional outlet targets.")

    if default_basin_name is None:
        basin_name = _prompt_non_empty("Basin name")
    else:
        basin_name = _prompt_non_empty("Basin name", default=default_basin_name)

    catchment_path = _prompt_non_empty("Catchment path", default=default_catchment_path)
    mode_names = _load_catchment_mode_names(root, catchment_path)
    if mode_names:
        mode = _prompt_choice(
            """
            - Basin Mode -
            Select a mode from the catchment spec.
            """,
            tuple(mode_names),
            default_mode if default_mode in mode_names else mode_names[0],
            multiline=True,
        )
    else:
        typer.echo("Note: Could not load catchment modes; using free-form mode input.")
        mode = _prompt_non_empty("Basin mode", default=default_mode)

    outlets: dict[str, str] = {}
    if _prompt_bool("Add outlet pond targets now", default=True):
        typer.echo("")
        while True:
            pond_name = _prompt_non_empty("Outlet pond name")
            version = _prompt_semver("Outlet version (x.y.z)")
            if pond_name in outlets and outlets[pond_name] != version:
                typer.echo(f"Replacing outlet {pond_name!r}: {outlets[pond_name]!r} -> {version!r}.")
            outlets[pond_name] = version
            if not _prompt_bool("Add another outlet", default=False):
                break

    summary = f"Create basin {basin_name!r} with mode {mode!r} and {len(outlets)} outlet(s)"
    return basin_name, catchment_path, mode, outlets, summary


def _build_basin_spec(
    *,
    basin_name: str,
    catchment_path: str,
    mode: str,
    outlets: dict[str, str],
) -> dict[str, Any]:
    basin = Basin(catchment=None, outlets=outlets, mode=mode, name=basin_name)
    spec = basin.to_dict()
    spec["catchment"] = {"path": catchment_path}
    return spec


@app.command("list")
def list_cmd() -> None:
    _print_basin_list(_repo_root())


@app.command("show")
def show_cmd(
    basin_name: str = typer.Argument(
        ...,
        help="Basin name",
        shell_complete=_complete_basin_names,
    ),
    spec: Optional[str] = typer.Option(
        None,
        "--spec",
        "-s",
        help="Path to basin spec (default: basin.json; relative to basin dir if not absolute).",
        shell_complete=_complete_spec_path,
    ),
) -> None:
    root = _repo_root()
    basin_dir = _require_basin_dir(root, basin_name)
    spec_path = _resolve_spec_path(basin_dir, spec)
    _print_summary(basin_name, basin_dir, spec_path)


@app.command("create")
def create_cmd(
    basin_name: Optional[str] = typer.Argument(
        None,
        help="Basin name (required unless --interactive).",
    ),
    catchment_path: str = typer.Option(
        "catchment.json",
        "--catchment-path",
        help="Path to catchment spec stored in basin.catchment.path.",
    ),
    mode: str = typer.Option("pulse", "--mode", help="Basin mode name."),
    outlet: list[str] = _OUTLET_OPTION,
    interactive: bool = typer.Option(
        False,
        "--interactive",
        "-i",
        help="Guided prompts to create a basin spec.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite basins/{name}/basin.json if it already exists.",
    ),
) -> None:
    root = _repo_root()
    raw_outlets = outlet

    if interactive:
        try:
            basin_name_value, catchment_path_value, mode_value, outlets, summary = _interactive_create(
                root,
                default_basin_name=basin_name,
                default_catchment_path=catchment_path,
                default_mode=mode,
            )
            if raw_outlets:
                typer.echo("Warning: --outlet values are ignored in interactive mode.", err=True)
            if not _prompt_bool(f"Confirm: {summary}", default=True):
                raise ValueError("Cancelled by user.")
        except Exception as exc:
            typer.echo(f"Interactive create failed: {exc}", err=True)
            raise typer.Exit(code=2) from exc
    else:
        if basin_name is None or not basin_name.strip():
            typer.echo("Error: Missing basin name.", err=True)
            raise typer.Exit(code=2)
        basin_name_value = basin_name.strip()
        catchment_path_value = catchment_path.strip()
        mode_value = mode.strip()
        try:
            outlets = _parse_outlet_args(raw_outlets)
        except ValueError as exc:
            typer.echo(f"Failed to create basin: {exc}", err=True)
            raise typer.Exit(code=2) from exc

    basin_name_value = basin_name_value.strip()
    catchment_path_value = catchment_path_value.strip()
    mode_value = mode_value.strip()
    try:
        _validate_basin_name(basin_name_value)
    except ValueError as exc:
        typer.echo(f"Failed to create basin: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if not catchment_path_value:
        typer.echo("Failed to create basin: catchment path must be non-empty.", err=True)
        raise typer.Exit(code=2)
    if not mode_value:
        typer.echo("Failed to create basin: mode must be non-empty.", err=True)
        raise typer.Exit(code=2)

    basin_dir = _basins_dir(root) / basin_name_value
    spec_path = basin_dir / "basin.json"
    if spec_path.exists() and not force:
        typer.echo(f"File already exists: {spec_path} (use --force to overwrite)", err=True)
        raise typer.Exit(code=2)

    basin_dir.mkdir(parents=True, exist_ok=True)
    spec = _build_basin_spec(
        basin_name=basin_name_value,
        catchment_path=catchment_path_value,
        mode=mode_value,
        outlets=outlets,
    )
    spec_path.write_text(json.dumps(spec, indent=2, sort_keys=True), encoding="utf-8")
    typer.echo(f"Wrote {spec_path}")


@app.command()
def hydrate(
    basin_name: str = typer.Argument(
        ...,
        help="Basin name",
        shell_complete=_complete_basin_names,
    ),
    spec: Optional[str] = typer.Option(
        None,
        "--spec",
        "-s",
        help="Path to basin spec (default: basin.json; relative to basin dir if not absolute).",
        shell_complete=_complete_spec_path,
    ),
    no_pull: bool = typer.Option(
        False,
        "--no-pull",
        help="Skip pulling catchment pond sources before hydration.",
    ),
) -> None:
    root = _repo_root()
    basin_dir = _require_basin_dir(root, basin_name)
    spec_path = _resolve_spec_path(basin_dir, spec)
    if not spec_path.exists():
        typer.echo(f"Spec not found: {spec_path}", err=True)
        raise typer.Exit(code=2)

    basin = Basin.load(str(spec_path))
    basin.hydrate(pull_sources=not no_pull)
    basin.save(str(spec_path))
    typer.echo(f"Wrote hydrated {spec_path}")


@app.command()
def pulse(
    basin_name: str = typer.Argument(
        ...,
        help="Basin name",
        shell_complete=_complete_basin_names,
    ),
    spec: Optional[str] = typer.Option(
        None,
        "--spec",
        "-s",
        help="Path to basin spec (default: basin.json; relative to basin dir if not absolute).",
        shell_complete=_complete_spec_path,
    ),
) -> None:
    root = _repo_root()
    basin_dir = _require_basin_dir(root, basin_name)
    spec_path = _resolve_spec_path(basin_dir, spec)
    if not spec_path.exists():
        typer.echo(f"Spec not found: {spec_path}", err=True)
        raise typer.Exit(code=2)

    basin = Basin.load(str(spec_path))
    pulse_result = basin.pulse(verbose=True)
    typer.echo(f"Completed pulse {pulse_result} in {pulse_result.duration} seconds")
