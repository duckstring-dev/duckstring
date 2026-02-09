from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional
from textwrap import dedent

import typer

from duckstring import Catchment, Species


app = typer.Typer(help="Work with catchment specs.", add_completion=False, no_args_is_help=True)
species_app = typer.Typer(help="Manage catchment species.", add_completion=False, no_args_is_help=True)
ponds_app = typer.Typer(help="Manage catchment pond catalog.", add_completion=False, no_args_is_help=True)
app.add_typer(species_app, name="species")
app.add_typer(ponds_app, name="ponds")

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


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

    if not isinstance(catchment.pond_sources, list):
        errors.append("pond_sources must be a list when present.")
    else:
        for idx, source in enumerate(catchment.pond_sources):
            if not isinstance(source, dict):
                errors.append(f"pond_sources[{idx}] must be an object.")
                continue
            source_type = source.get("type")
            structure = source.get("structure")
            if source_type == "local" and structure == "catalog":
                root = source.get("root")
                if not isinstance(root, str) or not root.strip():
                    errors.append(f"pond_sources[{idx}] local/catalog requires non-empty root.")
                if source.get("entrypoint", "pond.py") != "pond.py":
                    errors.append(f"pond_sources[{idx}] local/catalog entrypoint must be 'pond.py'.")
            elif source_type == "git" and structure == "single":
                for field in ("repo", "ref_type", "ref", "pond", "version"):
                    if not isinstance(source.get(field), str) or not str(source.get(field)).strip():
                        errors.append(f"pond_sources[{idx}] git/single requires non-empty {field}.")
                if source.get("entrypoint", "pond.py") != "pond.py":
                    errors.append(f"pond_sources[{idx}] git/single entrypoint must be 'pond.py'.")
            elif source_type == "git" and structure == "catalog":
                for field in ("repo", "catalog_path", "ref_pattern"):
                    if not isinstance(source.get(field), str) or not str(source.get(field)).strip():
                        errors.append(f"pond_sources[{idx}] git/catalog requires non-empty {field}.")
                if source.get("entrypoint", "pond.py") != "pond.py":
                    errors.append(f"pond_sources[{idx}] git/catalog entrypoint must be 'pond.py'.")
            else:
                errors.append(
                    f"pond_sources[{idx}] has unsupported type/structure combination "
                    f"{source_type!r}/{structure!r}."
                )

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
    typer.echo(f"Pond sources: {len(catchment.pond_sources)}")
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
    if catchment.ponds:
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
    else:
        typer.echo("Ponds: <none>")

    if catchment.pond_sources:
        typer.echo("Pond sources:")
        for idx, source in enumerate(catchment.pond_sources):
            source_type = source.get("type", "<unknown>")
            structure = source.get("structure", "<unknown>")
            if source_type == "local" and structure == "catalog":
                typer.echo(
                    f"  - [{idx}] local_catalog root={source.get('root')} strict={bool(source.get('strict', False))}"
                )
            elif source_type == "git" and structure == "single":
                typer.echo(
                    f"  - [{idx}] git_single pond={source.get('pond')} version={source.get('version')} repo={source.get('repo')}"
                )
            elif source_type == "git" and structure == "catalog":
                typer.echo(
                    f"  - [{idx}] git_catalog repo={source.get('repo')} pattern={source.get('ref_pattern')}"
                )
            else:
                typer.echo(f"  - [{idx}] {source_type}/{structure}")
    else:
        typer.echo("Pond sources: <none>")


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
    if not _SEMVER_RE.match(version):
        raise ValueError("version must use x.y.z semver format.")
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


def _prompt_choice(
    message: str,
    choices: tuple[str, ...],
    default: str,
    aliases: Optional[dict[str, str]] = None,
    multiline: bool = False,
) -> str:
    alias_map = aliases or {}
    choices_with_aliases = [f"{a} ({c})" for c, a in alias_map.items()]
    choice_text = " | ".join(choices_with_aliases if choices_with_aliases else choices)
    if multiline:
        message_split = dedent(message).strip().splitlines()
        indentation = ""
        message_indented = "\n".join(f"{indentation}{line}" for line in message_split)
        typer.echo("\n"+message_indented)
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


def _resolve_for_preview(catchment: Catchment, path_value: str) -> Path:
    p = Path(path_value).expanduser()
    if p.is_absolute():
        return p
    if catchment._loaded_from:
        return (Path(catchment._loaded_from).resolve().parent / p).resolve()
    return (Path.cwd() / p).resolve()


def _preview_local_catalog(catchment: Catchment, root_value: str) -> list[str]:
    root = _resolve_for_preview(catchment, root_value)
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Catalog root is not a directory: {root}")

    discovered: list[str] = []
    for pond_dir in sorted(root.iterdir()):
        if not pond_dir.is_dir():
            continue
        pond_name = pond_dir.name
        for version_dir in sorted(pond_dir.iterdir()):
            if not version_dir.is_dir():
                continue
            if not _SEMVER_RE.match(version_dir.name):
                continue
            if (version_dir / "pond.py").exists():
                discovered.append(f"{pond_name}@{version_dir.name}")
    return discovered


def _interactive_add(catchment: Catchment) -> str:
    typer.echo("-- Ponds: Interactive Add --")

    source_type = _prompt_choice(
        """
        - Source Type -
        May be 'local' using a file system path, or 'git' using a git repository.
        """,
        ("local", "git"),
        "local",
        aliases={"l": "local", "g": "git"},
        multiline=True,
    )
    scope = _prompt_choice(
        """
        - Scope -
        May be 'single' for an explicit pond + version, or 'catalog' for multiple ponds under a specified structure.
        Local catalogs follow a strict layout of {root}/{pond}/{version}/pond.py.
        Git catalogs accept a ref selector pattern (e.g. release/{version}).
        """,
        ("single", "catalog"),
        "single",
        aliases={"s": "single", "c": "catalog"},
        multiline=True,
    )

    if source_type == "local" and scope == "single":
        pond_dir = _prompt_non_empty("\nPond + Version directory path")
        resolved_dir = _resolve_for_preview(catchment, pond_dir)
        name_default = resolved_dir.parent.name if resolved_dir.parent.name else None
        version_default = resolved_dir.name if resolved_dir.name else None
        pond_name = _prompt_non_empty("\nPond Name", default=name_default)
        version = _prompt_non_empty("\nPond Version (x.y.z)", default=version_default)
        if not _SEMVER_RE.match(version):
            raise ValueError("Version must use x.y.z semver format.")

        if not typer.confirm(
            f"Write explicit pond entry {pond_name}@{version} -> {pond_dir}?",
            default=True,
            show_default=True,
        ):
            raise ValueError("Cancelled by user.")

        _set_pond_entry(
            catchment,
            name=pond_name.strip(),
            pond_path=pond_dir.strip(),
            version=version.strip(),
            overwrite=True,
        )
        return f"Added local pond {pond_name}@{version}"

    if source_type == "local" and scope == "catalog":
        ponds_default = "./ponds" if (Path.cwd() / "ponds").exists() else None
        if ponds_default is None:
            root_value = _prompt_non_empty("\nCatalog Root")
        else:
            root_value = _prompt_non_empty("\nCatalog Root", default=ponds_default)
        strict = typer.confirm(
            f"\nRun in strict mode, failing if expected layout not met?",
            default=True,
            show_default=True,
        )
        discovered = _preview_local_catalog(catchment, root_value)
        typer.echo("\nCatalog Preview:")
        if discovered:
            for item in discovered:
                typer.echo(f"  - {item}")
        else:
            typer.echo("  <none>")
        if not typer.confirm(
            f"\nWrite local catalog source root={root_value!r} strict={strict}?",
            default=True,
            show_default=True,
        ):
            raise ValueError("Cancelled by user.")
        catchment.pond_sources.append(
            {
                "type": "local",
                "structure": "catalog",
                "root": root_value,
                "layout": "{pond}/{version}",
                "entrypoint": "pond.py",
                "strict": strict,
            }
        )
        return f"Added local catalog source root={root_value!r}"

    if source_type == "git" and scope == "single":
        repo = _prompt_non_empty("\nGit Repo URL")
        ref_type = _prompt_choice(
            "\nGit Ref Type",
            ("branch", "tag", "commit"),
            "branch",
            aliases={"b": "branch", "t": "tag", "c": "commit"},
        )
        ref = _prompt_non_empty("\nGit Ref Value")
        pond_name = _prompt_non_empty("\nPond Name")
        version = _prompt_non_empty("\nPond Version (x.y.z)")
        if not _SEMVER_RE.match(version):
            raise ValueError("Version must use x.y.z semver format.")
        if not typer.confirm(
            f"\nWrite git single source for {pond_name}@{version} ({ref_type}:{ref})?",
            default=True,
            show_default=True,
        ):
            raise ValueError("Cancelled by user.")
        catchment.pond_sources.append(
            {
                "type": "git",
                "structure": "single",
                "repo": repo,
                "ref_type": ref_type,
                "ref": ref,
                "pond": pond_name,
                "version": version,
                "entrypoint": "pond.py",
            }
        )
        return f"Added git single source for {pond_name}@{version}"

    typer.echo("Git catalog: stores metadata only; no clone/pull or preview discovery is performed.")
    repo = _prompt_non_empty("Git repo URL")
    catalog_path = _prompt_non_empty("Catalog path in repo", default=".")
    ref_pattern = _prompt_non_empty("Ref selector pattern", default="release/{version}")
    strict = _prompt_choice(
        "Strict layout validation",
        ("yes", "no"),
        "no",
        aliases={"y": "yes", "n": "no"},
    ) == "yes"
    if not typer.confirm(
        f"Write git catalog source repo={repo!r} path={catalog_path!r} pattern={ref_pattern!r} strict={strict}?",
        default=True,
        show_default=True,
    ):
        raise ValueError("Cancelled by user.")
    catchment.pond_sources.append(
        {
            "type": "git",
            "structure": "catalog",
            "repo": repo,
            "catalog_path": catalog_path,
            "ref_pattern": ref_pattern,
            "layout": "{pond}/{version}",
            "entrypoint": "pond.py",
            "strict": strict,
        }
    )
    return f"Added git catalog source repo={repo!r}"


@ponds_app.command("add")
def ponds_add_cmd(
    name: Optional[str] = typer.Argument(None, help="Pond name (required for non-interactive mode)."),
    pond_path: Optional[str] = typer.Option(None, "--path", "-p", help="Local pond path."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Required pond version."),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        "-i",
        help="Guided prompts for local/git and single/catalog pond source setup.",
    ),
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

    if interactive:
        try:
            summary = _interactive_add(catchment)
        except Exception as exc:
            typer.echo(f"Interactive add failed: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        _save_catchment(catchment, resolved)
        typer.echo(f"{summary} in {resolved}")
        return

    if not name or pond_path is None or version is None:
        typer.echo("Non-interactive add requires <name>, --path/-p, and --version/-v.", err=True)
        raise typer.Exit(code=2)

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
