from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional
from textwrap import dedent
from urllib.parse import urlparse

import typer
from click.shell_completion import CompletionItem

from duckstring import Catchment, Species


app = typer.Typer(help="Work with catchment specs.", add_completion=False, no_args_is_help=True)
species_app = typer.Typer(help="Manage catchment species.", add_completion=False, no_args_is_help=True)
ponds_app = typer.Typer(help="Manage catchment pond catalog.", add_completion=False, no_args_is_help=True)
app.add_typer(species_app, name="species")
app.add_typer(ponds_app, name="ponds")

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _ensure_source_ids(catchment: Catchment) -> bool:
    changed = False
    next_id = 1
    used: set[int] = set()
    for source in catchment.pond_sources:
        if not isinstance(source, dict):
            continue
        raw = source.get("id")
        if isinstance(raw, int) and raw > 0:
            used.add(raw)
            next_id = max(next_id, raw + 1)

    for source in catchment.pond_sources:
        if not isinstance(source, dict):
            continue
        raw = source.get("id")
        if isinstance(raw, int) and raw > 0:
            continue
        while next_id in used:
            next_id += 1
        source["id"] = next_id
        used.add(next_id)
        next_id += 1
        changed = True
    return changed


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
        catchment = Catchment.load(str(path))
        _ensure_source_ids(catchment)
        return catchment
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

    if catchment.ponds:
        errors.append(
            "catchment.ponds is no longer supported; define sources under catchment.pond_sources only."
        )

    for mode_name, mode_spec in sorted(catchment.modes.items()):
        if not isinstance(mode_spec, dict):
            errors.append(f"mode {mode_name!r} must be an object.")
            continue
        if str(mode_spec.get("type", "pulse")) != "pulse":
            errors.append(f"mode {mode_name!r} has unsupported type {mode_spec.get('type')!r}.")

    if not isinstance(catchment.pond_sources, list):
        errors.append("pond_sources must be a list when present.")
    else:
        seen_ids: set[int] = set()
        for idx, source in enumerate(catchment.pond_sources):
            if not isinstance(source, dict):
                errors.append(f"pond_sources[{idx}] must be an object.")
                continue
            raw_id = source.get("id")
            if isinstance(raw_id, int) and raw_id > 0:
                if raw_id in seen_ids:
                    errors.append(f"pond_sources[{idx}] duplicates id={raw_id}.")
                seen_ids.add(raw_id)
            source_type = source.get("type")
            structure = source.get("structure")
            if source_type == "local" and structure == "catalog":
                raw_id = source.get("id")
                if not isinstance(raw_id, int) or raw_id <= 0:
                    errors.append(f"pond_sources[{idx}] requires positive integer id.")
                root = source.get("root")
                if not isinstance(root, str) or not root.strip():
                    errors.append(f"pond_sources[{idx}] local/catalog requires non-empty root.")
                if source.get("entrypoint", "pond.py") != "pond.py":
                    errors.append(f"pond_sources[{idx}] local/catalog entrypoint must be 'pond.py'.")
            elif source_type == "local" and structure == "single":
                raw_id = source.get("id")
                if not isinstance(raw_id, int) or raw_id <= 0:
                    errors.append(f"pond_sources[{idx}] requires positive integer id.")
                for field in ("pond", "version", "path"):
                    if not isinstance(source.get(field), str) or not str(source.get(field)).strip():
                        errors.append(f"pond_sources[{idx}] local/single requires non-empty {field}.")
                if source.get("entrypoint", "pond.py") != "pond.py":
                    errors.append(f"pond_sources[{idx}] local/single entrypoint must be 'pond.py'.")
            elif source_type == "git" and structure == "single":
                raw_id = source.get("id")
                if not isinstance(raw_id, int) or raw_id <= 0:
                    errors.append(f"pond_sources[{idx}] requires positive integer id.")
                for field in ("repo", "ref_type", "ref", "pond", "version"):
                    if not isinstance(source.get(field), str) or not str(source.get(field)).strip():
                        errors.append(f"pond_sources[{idx}] git/single requires non-empty {field}.")
                if source.get("entrypoint", "pond.py") != "pond.py":
                    errors.append(f"pond_sources[{idx}] git/single entrypoint must be 'pond.py'.")
            elif source_type == "git" and structure == "catalog":
                raw_id = source.get("id")
                if not isinstance(raw_id, int) or raw_id <= 0:
                    errors.append(f"pond_sources[{idx}] requires positive integer id.")
                repo = source.get("repo")
                if not isinstance(repo, str) or not repo.strip():
                    errors.append(f"pond_sources[{idx}] git/catalog requires non-empty repo.")
                repo_structure = str(source.get("repo_structure", "versioned"))
                if repo_structure not in ("versioned", "monorepo"):
                    errors.append(
                        f"pond_sources[{idx}] git/catalog repo_structure must be 'versioned' or 'monorepo'."
                    )
                ref_type = str(source.get("ref_type", "branch"))
                if repo_structure == "versioned":
                    if ref_type not in ("branch", "tag"):
                        errors.append(
                            f"pond_sources[{idx}] git/catalog with repo_structure=versioned requires ref_type branch or tag."
                        )
                    if not isinstance(source.get("ref_pattern"), str) or not str(source.get("ref_pattern")).strip():
                        errors.append(f"pond_sources[{idx}] git/catalog requires non-empty ref_pattern for versioned.")
                    if not isinstance(source.get("pond"), str) or not str(source.get("pond")).strip():
                        errors.append(f"pond_sources[{idx}] git/catalog requires non-empty pond for versioned.")
                else:
                    if ref_type not in ("branch", "tag", "commit"):
                        errors.append(
                            f"pond_sources[{idx}] git/catalog with repo_structure=monorepo requires ref_type branch, tag, or commit."
                        )
                    if not isinstance(source.get("ref_pattern"), str) or not str(source.get("ref_pattern")).strip():
                        errors.append(f"pond_sources[{idx}] git/catalog requires non-empty ref_pattern for monorepo.")
                if source.get("entrypoint", "pond.py") != "pond.py":
                    errors.append(f"pond_sources[{idx}] git/catalog entrypoint must be 'pond.py'.")
            else:
                errors.append(
                    f"pond_sources[{idx}] has unsupported type/structure combination "
                    f"{source_type!r}/{structure!r}."
                )

    return errors


def _load_for_completion(ctx: typer.Context) -> Optional[Catchment]:
    path_value = ctx.params.get("file")
    if path_value is None and ctx.parent is not None:
        path_value = ctx.parent.params.get("file")
    path = _resolve_path(path_value or "catchment.json")
    if not path.exists():
        return None
    try:
        catchment = Catchment.load(str(path))
        _ensure_source_ids(catchment)
        return catchment
    except Exception:
        return None


def _complete_source_ids(
    ctx: typer.Context, param: typer.CallbackParam, incomplete: str
) -> list[CompletionItem]:
    catchment = _load_for_completion(ctx)
    if catchment is None:
        return []
    items: list[CompletionItem] = []
    for source in catchment.pond_sources:
        if not isinstance(source, dict):
            continue
        source_id = source.get("id")
        if isinstance(source_id, int) and source_id > 0:
            sid = str(source_id)
            if sid.startswith(incomplete):
                items.append(CompletionItem(value=sid, help=_source_list_description(source)))
    return sorted(items, key=lambda item: int(item.value))


def _complete_species_names(ctx: typer.Context, param: typer.CallbackParam, incomplete: str) -> list[str]:
    catchment = _load_for_completion(ctx)
    if catchment is None:
        return []
    return sorted([name for name in catchment.species if name.startswith(incomplete)])


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

    if catchment.pond_sources:
        for source in catchment.pond_sources:
            source_id = source.get("id", "?")
            typer.echo(f"{source_id}  -- {_source_list_description(source)}")
    else:
        typer.echo("Pond sources: <none>")


def _build_source_from_direct_args(
    *,
    source_type: Optional[str],
    scope: Optional[str],
    pond: Optional[str],
    version: Optional[str],
    pond_path: Optional[str],
    root: Optional[str],
    repo: Optional[str],
    ref_type: Optional[str],
    ref: Optional[str],
    ref_pattern: Optional[str],
    repo_structure: str,
) -> dict[str, Any]:
    selected_source = (source_type or "").strip().lower() or None
    selected_scope = (scope or "").strip().lower() or None
    pond_name = (pond or "").strip() or None
    version_value = (version or "").strip() or None
    path_value = (pond_path or "").strip() or None
    root_value = (root or "").strip() or None
    repo_value = (repo or "").strip() or None
    ref_type_value = (ref_type or "").strip().lower() or None
    ref_value = (ref or "").strip() or None
    ref_pattern_value = (ref_pattern or "").strip() or None
    repo_structure_value = (repo_structure or "versioned").strip().lower()

    if selected_source not in ("local", "git"):
        raise ValueError("Provide --source-type as 'local' or 'git'.")
    if selected_scope not in ("single", "catalog"):
        raise ValueError("Provide --scope as 'single' or 'catalog'.")

    if selected_source == "local" and selected_scope == "single":
        if root_value:
            raise ValueError("local/single does not accept --root.")
        if repo_value:
            raise ValueError("local/single does not accept --repo.")
        if ref_type_value:
            raise ValueError("local/single does not accept --ref-type.")
        if ref_value:
            raise ValueError("local/single does not accept --ref.")
        if ref_pattern_value:
            raise ValueError("local/single does not accept --ref-pattern.")
        if repo_structure_value != "versioned":
            raise ValueError("local/single does not accept --repo-structure.")
        if not pond_name:
            raise ValueError("local/single requires --pond.")
        if not version_value:
            raise ValueError("local/single requires --version/-v.")
        if not _SEMVER_RE.match(version_value):
            raise ValueError("version must use x.y.z semver format.")
        if not path_value:
            raise ValueError("local/single requires --path/-p.")
        return {
            "type": "local",
            "structure": "single",
            "pond": pond_name,
            "version": version_value,
            "path": path_value,
            "entrypoint": "pond.py",
        }

    if selected_source == "local" and selected_scope == "catalog":
        if pond_name:
            raise ValueError("local/catalog does not accept --pond.")
        if version_value:
            raise ValueError("local/catalog does not accept --version.")
        if path_value:
            raise ValueError("local/catalog does not accept --path.")
        if repo_value:
            raise ValueError("local/catalog does not accept --repo.")
        if ref_type_value:
            raise ValueError("local/catalog does not accept --ref-type.")
        if ref_value:
            raise ValueError("local/catalog does not accept --ref.")
        if ref_pattern_value:
            raise ValueError("local/catalog does not accept --ref-pattern.")
        if repo_structure_value != "versioned":
            raise ValueError("local/catalog does not accept --repo-structure.")
        if root_value is None:
            root_value = "./ponds" if (Path.cwd() / "ponds").exists() else None
        if not root_value:
            raise ValueError("local/catalog requires --root (or a ./ponds directory).")
        return {
            "type": "local",
            "structure": "catalog",
            "root": root_value,
            "entrypoint": "pond.py",
        }

    if selected_source == "git" and selected_scope == "single":
        if root_value:
            raise ValueError("git/single does not accept --root.")
        if path_value:
            raise ValueError("git/single does not accept --path.")
        if ref_pattern_value:
            raise ValueError("git/single does not accept --ref-pattern.")
        if repo_structure_value != "versioned":
            raise ValueError("git/single does not accept --repo-structure.")
        if not pond_name:
            raise ValueError("git/single requires --pond.")
        if not version_value:
            raise ValueError("git/single requires --version/-v.")
        if not _SEMVER_RE.match(version_value):
            raise ValueError("version must use x.y.z semver format.")
        if not repo_value:
            raise ValueError("git/single requires --repo.")
        if not _is_valid_url(repo_value):
            raise ValueError("git/single requires a valid --repo URL.")
        if ref_type_value is None:
            ref_type_value = "branch"
        if ref_type_value not in ("branch", "tag", "commit"):
            raise ValueError("git/single --ref-type must be branch, tag, or commit.")
        if not ref_value:
            if ref_type_value == "branch":
                ref_value = f"release/{version_value}"
            elif ref_type_value == "tag":
                ref_value = version_value
        if not ref_value:
            raise ValueError("git/single requires --ref for ref_type=commit.")
        return {
            "type": "git",
            "structure": "single",
            "repo": repo_value,
            "ref_type": ref_type_value,
            "ref": ref_value,
            "pond": pond_name,
            "version": version_value,
            "entrypoint": "pond.py",
        }

    # git/catalog
    if version_value:
        raise ValueError("git/catalog does not accept --version.")
    if path_value:
        raise ValueError("git/catalog does not accept --path.")
    if root_value:
        raise ValueError("git/catalog does not accept --root.")
    if ref_value:
        raise ValueError("git/catalog does not accept --ref; use --ref-pattern.")
    if not repo_value:
        raise ValueError("git/catalog requires --repo.")
    if not _is_valid_url(repo_value):
        raise ValueError("git/catalog requires a valid --repo URL.")
    if repo_structure_value not in ("versioned", "monorepo"):
        raise ValueError("--repo-structure must be versioned or monorepo.")

    if repo_structure_value == "versioned":
        if not pond_name:
            raise ValueError("git/catalog versioned requires --pond.")
        if ref_type_value is None:
            ref_type_value = "branch"
        if ref_type_value not in ("branch", "tag"):
            raise ValueError("git/catalog versioned --ref-type must be branch or tag.")
        if not ref_pattern_value:
            if ref_value:
                ref_pattern_value = ref_value
            else:
                ref_pattern_value = "release/{version}" if ref_type_value == "branch" else "{version}"
    else:
        if pond_name:
            raise ValueError("git/catalog monorepo does not accept --pond.")
        if ref_type_value is None:
            ref_type_value = "branch"
        if ref_type_value not in ("branch", "tag", "commit"):
            raise ValueError("git/catalog monorepo --ref-type must be branch, tag, or commit.")
        if not ref_pattern_value:
            ref_pattern_value = ref_value or "main"
        pond_name = None

    return {
        "type": "git",
        "structure": "catalog",
        "repo_structure": repo_structure_value,
        "repo": repo_value,
        "ref_type": ref_type_value,
        "pond": pond_name,
        "ref_pattern": ref_pattern_value,
        "entrypoint": "pond.py",
    }


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


def _is_valid_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    return bool(parsed.scheme and parsed.netloc)


def _prompt_url(message: str, *, default: Optional[str] = None) -> str:
    while True:
        value = _prompt_non_empty(message, default=default)
        if _is_valid_url(value):
            return value
        typer.echo("Please enter a valid URL (e.g. https://example.com/repo.git).", err=True)


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


def _source_signature(source: dict[str, Any]) -> str:
    return json.dumps(source, sort_keys=True, separators=(",", ":"))


def _source_label(source: dict[str, Any]) -> str:
    source_type = source.get("type")
    structure = source.get("structure")
    if source_type == "local" and structure == "single":
        return f"local/single {source.get('pond')}@{source.get('version')}"
    if source_type == "local" and structure == "catalog":
        return f"local/catalog root={source.get('root')}"
    if source_type == "git" and structure == "single":
        return f"git/single {source.get('pond')}@{source.get('version')}"
    if source_type == "git" and structure == "catalog":
        repo_structure = source.get("repo_structure", "versioned")
        if repo_structure == "versioned":
            return f"git/catalog versioned pond={source.get('pond')}"
        return f"git/catalog monorepo repo={source.get('repo')}"
    return f"{source_type}/{structure}"


def _source_list_description(source: dict[str, Any]) -> str:
    source_type = source.get("type", "<unknown>")
    structure = source.get("structure", "<unknown>")
    if source_type == "local" and structure == "catalog":
        return f"local_catalog root={source.get('root')}"
    if source_type == "local" and structure == "single":
        return f"local_single pond={source.get('pond')} version={source.get('version')} path={source.get('path')}"
    if source_type == "git" and structure == "single":
        return f"git_single pond={source.get('pond')} version={source.get('version')} repo={source.get('repo')}"
    if source_type == "git" and structure == "catalog":
        ref_type = source.get("ref_type", "branch")
        repo_structure = source.get("repo_structure", "versioned")
        if repo_structure == "versioned":
            return (
                f"git_catalog/versioned pond={source.get('pond')} repo={source.get('repo')} "
                f"ref_type={ref_type} pattern={source.get('ref_pattern')}"
            )
        return (
            f"git_catalog/monorepo repo={source.get('repo')} "
            f"ref_type={ref_type} ref={source.get('ref_pattern')}"
        )
    return f"{source_type}/{structure}"


def _single_key(source: dict[str, Any]) -> Optional[tuple[str, str]]:
    source_type = source.get("type")
    structure = source.get("structure")
    if structure != "single":
        return None
    pond = source.get("pond")
    version = source.get("version")
    if isinstance(pond, str) and isinstance(version, str) and pond.strip() and version.strip():
        return pond.strip(), version.strip()
    return None


def _catalog_pond_names(catchment: Catchment, source: dict[str, Any]) -> set[str]:
    source_type = source.get("type")
    structure = source.get("structure")
    if structure != "catalog":
        return set()
    if source_type == "git":
        if source.get("repo_structure", "versioned") == "versioned":
            pond = source.get("pond")
            if isinstance(pond, str) and pond.strip():
                return {pond.strip()}
        return set()
    if source_type == "local":
        root = source.get("root")
        if isinstance(root, str) and root.strip():
            discovered = _preview_local_catalog(catchment, root)
            return {item.split("@", 1)[0] for item in discovered if "@" in item}
    return set()


def _has_monorepo_source(sources: list[dict[str, Any]]) -> bool:
    for source in sources:
        if source.get("type") == "git" and source.get("structure") == "catalog":
            if source.get("repo_structure", "versioned") == "monorepo":
                return True
    return False


def _catalog_versions(catchment: Catchment, source: dict[str, Any]) -> set[str]:
    if source.get("type") == "local" and source.get("structure") == "catalog":
        root = source.get("root")
        if isinstance(root, str) and root.strip():
            return set(_preview_local_catalog(catchment, root))
    return set()


def _analyze_new_source(catchment: Catchment, new_source: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    conflicts: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    sources = [entry for entry in catchment.pond_sources if isinstance(entry, dict)]

    new_sig = _source_signature(new_source)
    duplicate_idxs = [idx for idx, existing in enumerate(sources) if _source_signature(existing) == new_sig]
    if duplicate_idxs:
        conflicts.append(
            {
                "kind": "duplicate",
                "message": "Source already exists.",
                "indexes": duplicate_idxs,
            }
        )

    existing_single_map: dict[tuple[str, str], list[int]] = {}
    for idx, source in enumerate(sources):
        key = _single_key(source)
        if key is not None:
            existing_single_map.setdefault(key, []).append(idx)

    new_single = _single_key(new_source)
    if new_single is not None and new_single in existing_single_map:
        conflicts.append(
            {
                "kind": "single_key",
                "message": f"Single source for {new_single[0]}@{new_single[1]} already exists.",
                "indexes": existing_single_map[new_single],
            }
        )

    existing_catalog_names: dict[str, list[str]] = {}
    existing_catalog_indexes: dict[str, list[int]] = {}
    for idx, source in enumerate(sources):
        try:
            names = _catalog_pond_names(catchment, source)
        except Exception as exc:
            warnings.append({"title": "Catalog inspection warning", "message": f"Could not inspect pond_sources[{idx}]: {exc}"})
            continue
        for pond in sorted(names):
            existing_catalog_names.setdefault(pond, []).append(f"pond_sources[{idx}]")
            existing_catalog_indexes.setdefault(pond, []).append(idx)

    try:
        new_catalog_names = _catalog_pond_names(catchment, new_source)
    except Exception as exc:
        conflicts.append({"kind": "invalid_catalog", "message": f"Could not inspect new catalog source: {exc}", "indexes": []})
        return conflicts, warnings

    if new_single is not None:
        pond_name = new_single[0]
        if pond_name in existing_catalog_names:
            idxs = sorted(set(existing_catalog_indexes.get(pond_name, [])))
            warnings.append(
                {
                    "title": "Single source may override catalog entries",
                    "sources": [sources[i] for i in idxs],
                    "ponds": [pond_name],
                    "versions": [f"{pond_name}@{new_single[1]}"],
                }
            )
    elif new_source.get("structure") == "catalog":
        overlapping_ponds = sorted([pond for pond in new_catalog_names if pond in existing_catalog_names])
        if overlapping_ponds:
            idxs: set[int] = set()
            for pond in overlapping_ponds:
                for idx in existing_catalog_indexes.get(pond, []):
                    idxs.add(idx)
            new_versions = _catalog_versions(catchment, new_source)
            warnings.append(
                {
                    "title": "Catalog contains potentially conflicting pond sources",
                    "sources": [sources[i] for i in sorted(idxs)],
                    "ponds": overlapping_ponds,
                    "versions": sorted([v for v in new_versions if v.split("@", 1)[0] in overlapping_ponds]),
                }
            )
        for pond in sorted(new_catalog_names):
            matching_singles = sorted(
                [f"{p}@{v}" for (p, v) in existing_single_map.keys() if p == pond]
            )
            if matching_singles:
                warnings.append(
                    {
                        "title": "Catalog includes pond with single-source overrides",
                        "ponds": [pond],
                        "versions": matching_singles,
                    }
                )

    combined_sources = sources + [new_source]
    if _has_monorepo_source(combined_sources):
        warnings.append(
            {
                "title": "Monorepo catalog warning",
                "message": "A git monorepo source exists; overlaps can only be detected during basin hydration.",
            }
        )

    return conflicts, warnings


def _print_warning(warning: dict[str, Any]) -> None:
    title = warning.get("title", "Warning")
    typer.echo(f"Warning: {title}", err=True)
    message = warning.get("message")
    if isinstance(message, str) and message.strip():
        typer.echo(f"  {message}", err=True)
    sources = warning.get("sources")
    if isinstance(sources, list) and sources:
        typer.echo("  Sources:", err=True)
        for source in sources:
            typer.echo("    ---", err=True)
            typer.echo(json.dumps(source, indent=6, sort_keys=True), err=True)
            typer.echo("    ---", err=True)
    ponds = warning.get("ponds")
    if isinstance(ponds, list) and ponds:
        typer.echo("  Ponds:", err=True)
        for pond in ponds:
            typer.echo(f"    {pond}", err=True)
    versions = warning.get("versions")
    if isinstance(versions, list) and versions:
        typer.echo("  Versions:", err=True)
        for version in versions:
            typer.echo(f"    {version}", err=True)


def _resolve_conflicts(catchment: Catchment, conflicts: list[dict[str, Any]]) -> None:
    if not conflicts:
        return
    typer.echo("Conflicts detected with existing pond sources:", err=True)
    remove_indexes: set[int] = set()
    for conflict in conflicts:
        typer.echo(f"  - {conflict.get('message', 'Conflict')}", err=True)
        for idx in conflict.get("indexes", []):
            remove_indexes.add(int(idx))
    if not _prompt_bool("Overwrite conflicting source(s)", default=False):
        raise ValueError("Cancelled due to conflicts.")
    if remove_indexes:
        catchment.pond_sources = [
            src for idx, src in enumerate(catchment.pond_sources) if idx not in remove_indexes
        ]


def _interactive_add(catchment: Catchment) -> tuple[str, dict[str, Any]]:
    typer.echo("-- Ponds: Interactive Add --")
    typer.echo("This flow writes either explicit pond entries or pond source definitions to catchment.json.")

    source_type = _prompt_choice(
        """
        - Source Type -
        local:     Pond code is sourced from a local filesystem path.
        git:       Pond code is sourced from a git repository.
        """,
        ("local", "git"),
        "local",
        aliases={"l": "local", "g": "git"},
        multiline=True,
    )
    if source_type == "local":
        scope_prompt = """
        - Scope (local) -
        single:    Specify a single explicit pond + version by a directory path.
        catalog:   Specify a location containing ponds and versions with structure {root}/{pond}/{version}.
        """
    else:
        scope_prompt = """
        - Scope (git) -
        single:    Specify a single explicit pond + version by a fixed git ref.
        catalog:   Specify a collection of ponds and versions in a git repository, e.g. by a ref pattern.
        """

    scope = _prompt_choice(
        scope_prompt,
        ("single", "catalog"),
        "single",
        aliases={"s": "single", "c": "catalog"},
        multiline=True,
    )

    if source_type == "local" and scope == "single":
        typer.echo("")
        pond_name = _prompt_non_empty("Pond name")
        version = _prompt_non_empty("Pond version (x.y.z)")
        if not _SEMVER_RE.match(version):
            raise ValueError("Version must use x.y.z semver format.")
        pond_dir = _prompt_non_empty("Pond version directory path")
        resolved_dir = _resolve_for_preview(catchment, pond_dir)
        if resolved_dir.parent.name and resolved_dir.parent.name != pond_name:
            typer.echo(
                f"Note: path implies pond {resolved_dir.parent.name!r}; using explicit name {pond_name!r}.",
            )
        if resolved_dir.name and resolved_dir.name != version:
            typer.echo(
                f"Note: path implies version {resolved_dir.name!r}; using explicit version {version!r}.",
            )

        source = {
            "type": "local",
            "structure": "single",
            "pond": pond_name.strip(),
            "version": version.strip(),
            "path": pond_dir.strip(),
            "entrypoint": "pond.py",
        }
        return f"Add local single source for {pond_name}@{version}", source

    if source_type == "local" and scope == "catalog":
        typer.echo("")
        ponds_default = "./ponds" if (Path.cwd() / "ponds").exists() else None
        if ponds_default is None:
            root_value = _prompt_non_empty("Catalog root path")
        else:
            root_value = _prompt_non_empty("Catalog root path", default=ponds_default)
        discovered = _preview_local_catalog(catchment, root_value)
        typer.echo("\nDiscovery preview:")
        if discovered:
            for item in discovered:
                typer.echo(f"  - {item}")
        else:
            typer.echo("  <none>")
        source = {
            "type": "local",
            "structure": "catalog",
            "root": root_value,
            "entrypoint": "pond.py",
        }
        return f"Add local catalog source root={root_value!r}", source

    if source_type == "git" and scope == "single":
        typer.echo("")
        pond_name = _prompt_non_empty("Pond name")
        version = _prompt_non_empty("Pond version (x.y.z)")
        if not _SEMVER_RE.match(version):
            raise ValueError("Version must use x.y.z semver format.")
        ref_type = _prompt_choice(
            "Git ref type",
            ("branch", "tag", "commit"),
            "branch",
            aliases={"b": "branch", "t": "tag", "c": "commit"},
        )
        repo = _prompt_url("Git repo URL")
        ref_default = None
        if ref_type == "branch":
            ref_default = f"release/{version}"
        elif ref_type == "tag":
            ref_default = version
        ref = _prompt_non_empty("Git ref value", default=ref_default)
        source = {
            "type": "git",
            "structure": "single",
            "repo": repo,
            "ref_type": ref_type,
            "ref": ref,
            "pond": pond_name,
            "version": version,
            "entrypoint": "pond.py",
        }
        return f"Add git single source for {pond_name}@{version}", source

    repo_structure = _prompt_choice(
        """
        - Repository Structure -
        versioned: (Recommended) Repo contains one pond with versions implied by a ref pattern (e.g. branch: release/{version})
        monorepo:  Repo at a fixed ref contains a catalog of ponds in the structure {root}/{pond}/{version}
        """,
        ("versioned", "monorepo"),
        "versioned",
        aliases={"v": "versioned", "m": "monorepo"},
        multiline=True,
    )
    if repo_structure == "monorepo":
        typer.echo(
            "Note: Monorepo layout must use {root}/{pond}/{version} even if only one pond is included."
        )
        ref_type = _prompt_choice(
            "Git ref type",
            ("branch", "tag", "commit"),
            "branch",
            aliases={"b": "branch", "t": "tag", "c": "commit"},
        )
        ref_pattern = _prompt_non_empty("Git ref value", default="main")
        repo = _prompt_url("Git repo URL")
        pond_name = None
    else:
        ref_type = _prompt_choice(
            "Git ref type",
            ("branch", "tag"),
            "branch",
            aliases={"b": "branch", "t": "tag"},
        )
        pattern_default = "release/{version}" if ref_type == "branch" else "{version}"
        ref_pattern = _prompt_non_empty("Git ref pattern", default=pattern_default)
        repo = _prompt_url("Git repo URL")
        pond_name = _prompt_non_empty("Pond name")
    source = {
        "type": "git",
        "structure": "catalog",
        "repo_structure": repo_structure,
        "repo": repo,
        "ref_type": ref_type,
        "pond": pond_name,
        "ref_pattern": ref_pattern,
        "entrypoint": "pond.py",
    }
    return f"Add git catalog source repo={repo!r}", source


@ponds_app.command("add")
def ponds_add_cmd(
    source_type: Optional[str] = typer.Option(
        None,
        "--source-type",
        help="Source type for direct mode: local or git.",
    ),
    scope: Optional[str] = typer.Option(
        None,
        "--scope",
        help="Scope for direct mode: single or catalog.",
    ),
    pond: Optional[str] = typer.Option(
        None,
        "--pond",
        help="Pond name (required for single sources; for git/versioned catalog).",
    ),
    pond_path: Optional[str] = typer.Option(None, "--path", "-p", help="Local pond path."),
    root: Optional[str] = typer.Option(None, "--root", help="Local catalog root path."),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Required pond version."),
    repo: Optional[str] = typer.Option(None, "--repo", help="Git repository URL."),
    repo_structure: str = typer.Option(
        "versioned",
        "--repo-structure",
        help="Git catalog repository structure: versioned or monorepo.",
    ),
    ref_type: Optional[str] = typer.Option(
        None,
        "--ref-type",
        help="Git ref type. single: branch/tag/commit; versioned catalog: branch/tag; monorepo catalog: branch/tag/commit.",
    ),
    ref: Optional[str] = typer.Option(None, "--ref", help="Git ref value (single sources)."),
    ref_pattern: Optional[str] = typer.Option(
        None,
        "--ref-pattern",
        help="Git ref pattern or fixed ref value for monorepo catalogs.",
    ),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        "-i",
        help="Guided prompts for local/git and single/catalog pond source setup.",
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

    if interactive:
        try:
            summary, source = _interactive_add(catchment)
        except Exception as exc:
            typer.echo(f"Interactive add failed: {exc}", err=True)
            raise typer.Exit(code=2) from exc
    else:
        try:
            source = _build_source_from_direct_args(
                source_type=source_type,
                scope=scope,
                pond=pond,
                version=version,
                pond_path=pond_path,
                root=root,
                repo=repo,
                ref_type=ref_type,
                ref=ref,
                ref_pattern=ref_pattern,
                repo_structure=repo_structure,
            )
            summary = f"Add pond source {_source_label(source)!r}"
        except Exception as exc:
            typer.echo(f"Failed to add pond source: {exc}", err=True)
            raise typer.Exit(code=2) from exc

    try:
        conflicts, warnings = _analyze_new_source(catchment, source)
        if conflicts:
            _resolve_conflicts(catchment, conflicts)
            conflicts, warnings = _analyze_new_source(catchment, source)
            if conflicts:
                raise ValueError("Conflicts remain after overwrite attempt.")
        for warning in warnings:
            _print_warning(warning)
        if not _prompt_bool(f"Confirm: {summary}", default=True):
            raise ValueError("Cancelled by user.")
    except Exception as exc:
        typer.echo(f"Failed to add pond source: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    catchment.pond_sources.append(source)
    _save_catchment(catchment, resolved)
    typer.echo(f"Added pond source {_source_label(source)!r} to {resolved}")


@ponds_app.command("remove")
def ponds_remove_cmd(
    source_id: int = typer.Argument(..., help="Pond source id to remove.", shell_complete=_complete_source_ids),
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
    before = len(catchment.pond_sources)
    catchment.pond_sources = [
        source
        for source in catchment.pond_sources
        if not (isinstance(source, dict) and source.get("id") == source_id)
    ]
    if len(catchment.pond_sources) == before:
        typer.echo(f"No pond source found with id={source_id}.", err=True)
        raise typer.Exit(code=2)
    _save_catchment(catchment, resolved)
    typer.echo(f"Removed pond source id={source_id} from {resolved}")
