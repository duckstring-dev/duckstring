from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import typer

from duckstring import Catchment, Species


app = typer.Typer(help="Work with catchment specs.", add_completion=False, no_args_is_help=True)
species_app = typer.Typer(help="Manage catchment species.", add_completion=False, no_args_is_help=True)
ponds_app = typer.Typer(help="Manage catchment pond catalog.", add_completion=False, no_args_is_help=True)
app.add_typer(species_app, name="species")
app.add_typer(ponds_app, name="ponds")


def _resolve_path(path: Path | str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    return p.resolve()


def _load_catchment(path: Path) -> Catchment:
    if not path.exists():
        typer.echo(f"Catchment not found: {path}", err=True)
        raise typer.Exit(code=2)
    try:
        return Catchment.load(str(path))
    except Exception as exc:
        typer.echo(f"Failed to load catchment {path}: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def _save_catchment(catchment: Catchment, path: Path) -> None:
    try:
        catchment.save(str(path))
    except Exception as exc:
        typer.echo(f"Failed to save catchment {path}: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def _complete_json_paths(ctx: typer.Context, param: typer.CallbackParam, incomplete: str) -> list[str]:
    base = Path.cwd()
    token = incomplete or ""
    if "/" in token:
        parent = Path(token).parent
        stem = Path(token).name
        base = base / parent
        prefix = "" if str(parent) == "." else f"{parent}/"
    else:
        stem = token
        prefix = ""
    if not base.exists():
        return []
    out: list[str] = []
    for entry in base.iterdir():
        if entry.is_file() and entry.suffix == ".json" and entry.name.startswith(stem):
            out.append(f"{prefix}{entry.name}")
    return sorted(out)


def _parse_options(raw_options: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in raw_options:
        if "=" not in item:
            raise ValueError(f"Invalid --option value {item!r}; expected key=value.")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Invalid --option value {item!r}; key must be non-empty.")
        out[key] = value
    return out


def _validate_catchment(catchment: Catchment) -> list[str]:
    errors: list[str] = []
    if not catchment.root_dir or not str(catchment.root_dir).strip():
        errors.append("root_dir must be non-empty.")

    for name, species in sorted(catchment.species.items()):
        try:
            species.validate()
        except Exception as exc:
            errors.append(f"species.{name}: {exc}")

    if catchment.default_species is not None and catchment.default_species not in catchment.species:
        errors.append(
            f"default_species {catchment.default_species!r} is not present in species catalog."
        )

    for pond_name, species_name in sorted(catchment.pond_species.items()):
        if pond_name not in catchment.ponds:
            errors.append(f"pond_species entry {pond_name!r} references unknown pond.")
        if species_name not in catchment.species:
            errors.append(f"pond_species entry {pond_name!r} references unknown species {species_name!r}.")

    for pond_name, entry in sorted(catchment.ponds.items()):
        if isinstance(entry, str):
            errors.append(
                f"pond {pond_name!r} is unversioned; catchment pond entries must be versioned."
            )
            continue
        if isinstance(entry, dict):
            if not entry:
                errors.append(f"pond {pond_name!r} has no versions.")
            for version, path in entry.items():
                if not isinstance(version, str) or not version.strip():
                    errors.append(f"pond {pond_name!r} has an invalid version key.")
                if not isinstance(path, str) or not path.strip():
                    errors.append(f"pond {pond_name!r}@{version!r} has an invalid path.")
            continue
        errors.append(f"pond {pond_name!r} has unsupported catalog entry type {type(entry).__name__}.")

    for mode_name, mode_spec in sorted(catchment.modes.items()):
        if not isinstance(mode_spec, dict):
            errors.append(f"mode {mode_name!r} must be an object.")
            continue
        if str(mode_spec.get("type", "pulse")) != "pulse":
            errors.append(f"mode {mode_name!r} has unsupported type {mode_spec.get('type')!r}.")

    return errors


def _count_pond_versions(ponds: dict[str, Any]) -> int:
    total = 0
    for entry in ponds.values():
        if isinstance(entry, dict):
            total += len(entry)
        else:
            total += 1
    return total


def _load_for_completion(ctx: typer.Context) -> Optional[Catchment]:
    path_value = ctx.params.get("file")
    if path_value is None and ctx.parent is not None:
        path_value = ctx.parent.params.get("file")
    path = _resolve_path(path_value or "catchment.json")
    if not path.exists():
        return None
    try:
        return Catchment.load(str(path))
    except Exception:
        return None


def _complete_species_names(ctx: typer.Context, param: typer.CallbackParam, incomplete: str) -> list[str]:
    catchment = _load_for_completion(ctx)
    if catchment is None:
        return []
    return sorted([name for name in catchment.species if name.startswith(incomplete)])


def _complete_pond_names(ctx: typer.Context, param: typer.CallbackParam, incomplete: str) -> list[str]:
    catchment = _load_for_completion(ctx)
    if catchment is None:
        return []
    return sorted([name for name in catchment.ponds if name.startswith(incomplete)])


@app.command()
def create(
    path: Path = typer.Argument(
        Path("catchment.json"),
        help="Path to write catchment spec.",
        shell_complete=_complete_json_paths,
    ),
    root_dir: str = typer.Option(".duckstring", "--root-dir", help="Catchment root_dir value."),
    default_species: str = typer.Option("local", "--default-species", help="Default species name to create."),
    no_default_species: bool = typer.Option(
        False,
        "--no-default-species",
        help="Skip creating a default local species entry.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing file if it already exists."),
) -> None:
    out_path = _resolve_path(path)
    if out_path.exists() and not force:
        typer.echo(f"File already exists: {out_path} (use --force to overwrite)", err=True)
        raise typer.Exit(code=2)

    catchment = Catchment(root_dir=root_dir)
    if not no_default_species:
        name = default_species.strip()
        if not name:
            typer.echo("default species name must be non-empty.", err=True)
            raise typer.Exit(code=2)
        catchment.set_species({name: Species(kind="local", engine="duckdb")})
        catchment.set_default_species(name)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _save_catchment(catchment, out_path)
    typer.echo(f"Wrote {out_path}")


@app.command("show")
def show_cmd(
    path: Path = typer.Argument(
        Path("catchment.json"),
        help="Path to catchment spec.",
        shell_complete=_complete_json_paths,
    ),
) -> None:
    resolved = _resolve_path(path)
    catchment = _load_catchment(resolved)

    typer.echo(f"Catchment: {resolved}")
    typer.echo(f"Root dir: {catchment.root_dir}")
    typer.echo(f"Species: {len(catchment.species)}")
    typer.echo(f"Default species: {catchment.default_species or '<none>'}")
    typer.echo(f"Ponds: {len(catchment.ponds)}")
    typer.echo(f"Pond versions: {_count_pond_versions(catchment.ponds)}")
    typer.echo(f"Pond species mappings: {len(catchment.pond_species)}")
    typer.echo(f"Modes: {', '.join(sorted(catchment.modes.keys())) if catchment.modes else '<none>'}")


@app.command("validate")
def validate_cmd(
    path: Path = typer.Argument(
        Path("catchment.json"),
        help="Path to catchment spec.",
        shell_complete=_complete_json_paths,
    ),
) -> None:
    resolved = _resolve_path(path)
    catchment = _load_catchment(resolved)
    errors = _validate_catchment(catchment)
    if errors:
        typer.echo(f"Invalid catchment: {resolved}", err=True)
        for err in errors:
            typer.echo(f"  - {err}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Catchment is valid: {resolved}")


@app.command("fmt")
def fmt_cmd(
    path: Path = typer.Argument(
        Path("catchment.json"),
        help="Path to catchment spec.",
        shell_complete=_complete_json_paths,
    ),
) -> None:
    resolved = _resolve_path(path)
    catchment = _load_catchment(resolved)
    _save_catchment(catchment, resolved)
    typer.echo(f"Formatted {resolved}")


@app.command("set-root")
def set_root_cmd(
    root_dir: str = typer.Argument(..., help="New root_dir value."),
    path: Path = typer.Option(
        Path("catchment.json"),
        "--file",
        "-f",
        help="Path to catchment spec.",
        shell_complete=_complete_json_paths,
    ),
) -> None:
    resolved = _resolve_path(path)
    catchment = _load_catchment(resolved)
    catchment.set_root_dir(root_dir)
    _save_catchment(catchment, resolved)
    typer.echo(f"Updated root_dir in {resolved}")


@species_app.command("list")
def species_list_cmd(
    path: Path = typer.Option(
        Path("catchment.json"),
        "--file",
        "-f",
        help="Path to catchment spec.",
        shell_complete=_complete_json_paths,
    ),
) -> None:
    resolved = _resolve_path(path)
    catchment = _load_catchment(resolved)
    if not catchment.species:
        typer.echo("No species configured.")
        return

    default_name = catchment.default_species
    typer.echo("Species:")
    for name, species in sorted(catchment.species.items()):
        marker = " (default)" if name == default_name else ""
        typer.echo(f"  - {name}{marker}: kind={species.kind} engine={species.engine}")


@species_app.command("add")
def species_add_cmd(
    name: str = typer.Argument(..., help="Species name."),
    kind: str = typer.Option("local", "--kind", help="Species kind."),
    engine: str = typer.Option("duckdb", "--engine", help="Species engine."),
    option: list[str] = typer.Option(
        [],
        "--option",
        help="Species option as key=value (repeatable).",
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite existing species config if present."),
    path: Path = typer.Option(
        Path("catchment.json"),
        "--file",
        "-f",
        help="Path to catchment spec.",
        shell_complete=_complete_json_paths,
    ),
) -> None:
    resolved = _resolve_path(path)
    catchment = _load_catchment(resolved)

    try:
        options = _parse_options(option)
        species = Species(kind=kind, engine=engine, options=options)
        catchment.set_species({name: species}, overwrite=overwrite)
    except Exception as exc:
        typer.echo(f"Failed to add species {name!r}: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    _save_catchment(catchment, resolved)
    typer.echo(f"Added species {name!r} to {resolved}")


@species_app.command("remove")
def species_remove_cmd(
    name: str = typer.Argument(..., help="Species name.", shell_complete=_complete_species_names),
    force: bool = typer.Option(
        False,
        "--force",
        help="Also clear default_species and pond_species mappings that reference this species.",
    ),
    path: Path = typer.Option(
        Path("catchment.json"),
        "--file",
        "-f",
        help="Path to catchment spec.",
        shell_complete=_complete_json_paths,
    ),
) -> None:
    resolved = _resolve_path(path)
    catchment = _load_catchment(resolved)
    if name not in catchment.species:
        typer.echo(f"Unknown species {name!r}.", err=True)
        raise typer.Exit(code=2)

    if catchment.default_species == name:
        if not force:
            typer.echo(
                f"Species {name!r} is currently default. Use catchment species set-default first, or pass --force.",
                err=True,
            )
            raise typer.Exit(code=2)
        catchment.default_species = None

    mapped_ponds = sorted([pond for pond, species_name in catchment.pond_species.items() if species_name == name])
    if mapped_ponds:
        if not force:
            typer.echo(
                f"Species {name!r} is assigned to ponds: {', '.join(mapped_ponds)}. Use --force to remove mappings.",
                err=True,
            )
            raise typer.Exit(code=2)
        for pond_name in mapped_ponds:
            catchment.pond_species.pop(pond_name, None)

    catchment.species.pop(name, None)
    _save_catchment(catchment, resolved)
    typer.echo(f"Removed species {name!r} from {resolved}")


@species_app.command("set-default")
def species_set_default_cmd(
    name: str = typer.Argument(..., help="Species name.", shell_complete=_complete_species_names),
    path: Path = typer.Option(
        Path("catchment.json"),
        "--file",
        "-f",
        help="Path to catchment spec.",
        shell_complete=_complete_json_paths,
    ),
) -> None:
    resolved = _resolve_path(path)
    catchment = _load_catchment(resolved)
    try:
        catchment.set_default_species(name)
    except Exception as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    _save_catchment(catchment, resolved)
    typer.echo(f"Set default species to {name!r} in {resolved}")


@ponds_app.command("list")
def ponds_list_cmd(
    path: Path = typer.Option(
        Path("catchment.json"),
        "--file",
        "-f",
        help="Path to catchment spec.",
        shell_complete=_complete_json_paths,
    ),
) -> None:
    resolved = _resolve_path(path)
    catchment = _load_catchment(resolved)
    if not catchment.ponds:
        typer.echo("No ponds configured.")
        return

    typer.echo("Ponds:")
    for pond_name, entry in sorted(catchment.ponds.items()):
        if isinstance(entry, str):
            typer.echo(f"  - {pond_name}: {entry}")
        elif isinstance(entry, dict):
            typer.echo(f"  - {pond_name}:")
            for version, pond_path in sorted(entry.items()):
                typer.echo(f"      {version}: {pond_path}")
        else:
            typer.echo(f"  - {pond_name}: <unsupported entry type {type(entry).__name__}>")


def _set_pond_entry(
    catchment: Catchment,
    *,
    name: str,
    pond_path: str,
    version: str,
    overwrite: bool,
) -> None:
    existing = catchment.ponds.get(name)
    if not version.strip():
        raise ValueError("version must be non-empty.")
    if existing is None:
        catchment.ponds[name] = {version: pond_path}
        return
    if isinstance(existing, str):
        if not overwrite:
            raise ValueError(f"Pond {name!r} already exists as an unversioned entry. Use --overwrite to replace it.")
        catchment.ponds[name] = {version: pond_path}
        return
    if isinstance(existing, dict):
        if version in existing and not overwrite and existing[version] != pond_path:
            raise ValueError(f"Pond {name!r}@{version} already exists with a different path.")
        existing[version] = pond_path
        return
    raise ValueError(f"Unsupported existing pond entry type for {name!r}: {type(existing).__name__}")


@ponds_app.command("add")
def ponds_add_cmd(
    name: str = typer.Argument(..., help="Pond name."),
    pond_path: str = typer.Option(..., "--path", "-p", help="Local pond path."),
    version: str = typer.Option(..., "--version", "-v", help="Required pond version."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite existing entry if present."),
    path: Path = typer.Option(
        Path("catchment.json"),
        "--file",
        "-f",
        help="Path to catchment spec.",
        shell_complete=_complete_json_paths,
    ),
) -> None:
    resolved = _resolve_path(path)
    catchment = _load_catchment(resolved)
    try:
        _set_pond_entry(
            catchment,
            name=name.strip(),
            pond_path=pond_path.strip(),
            version=version.strip(),
            overwrite=overwrite,
        )
    except Exception as exc:
        typer.echo(f"Failed to add pond entry: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    _save_catchment(catchment, resolved)
    label = f"{name}@{version}"
    typer.echo(f"Added pond {label!r} to {resolved}")


@ponds_app.command("remove")
def ponds_remove_cmd(
    name: str = typer.Argument(..., help="Pond name.", shell_complete=_complete_pond_names),
    version: str = typer.Option(..., "--version", "-v", help="Required version to remove."),
    path: Path = typer.Option(
        Path("catchment.json"),
        "--file",
        "-f",
        help="Path to catchment spec.",
        shell_complete=_complete_json_paths,
    ),
) -> None:
    resolved = _resolve_path(path)
    catchment = _load_catchment(resolved)
    if name not in catchment.ponds:
        typer.echo(f"Unknown pond {name!r}.", err=True)
        raise typer.Exit(code=2)

    entry = catchment.ponds.get(name)
    if not isinstance(entry, dict):
        typer.echo(f"Pond {name!r} is not stored as versioned entry.", err=True)
        raise typer.Exit(code=2)
    if version not in entry:
        typer.echo(f"Pond {name!r} has no version {version!r}.", err=True)
        raise typer.Exit(code=2)
    entry.pop(version, None)
    if not entry:
        catchment.ponds.pop(name, None)
        catchment.pond_species.pop(name, None)

    _save_catchment(catchment, resolved)
    label = f"{name}@{version}"
    typer.echo(f"Removed pond {label!r} from {resolved}")
