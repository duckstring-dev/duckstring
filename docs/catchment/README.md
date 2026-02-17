# Catchment CLI

This document describes the `duckstring catchment ...` command group.

## Command Group

- `duckstring catchment create [PATH]`
- `duckstring catchment show [PATH]`
- `duckstring catchment validate [PATH]`
- `duckstring catchment fmt [PATH]`
- `duckstring catchment set-root <ROOT_DIR> [-f|--file <PATH>]`
- `duckstring catchment species ...`
- `duckstring catchment ponds ...`

Default catchment file path for commands that use `--file` is `catchment.json`.
`catchment.json` should define `pond_sources`; `ponds` is not part of the catchment spec.

## catchment create

Create a new catchment spec file.

```bash
duckstring catchment create [path] [options]
```

Options:

- `--root-dir <value>` (default: `.duckstring`)
- `--default-species <name>` (default: `local`)
- `--no-default-species`
- `--force`

## catchment show

Print a human-readable summary.

```bash
duckstring catchment show [path]
```

## catchment validate

Validate catchment structure and pond source metadata.

```bash
duckstring catchment validate [path]
```

## catchment fmt

Rewrite catchment JSON with normalized formatting.

```bash
duckstring catchment fmt [path]
```

## catchment set-root

Set `root_dir`.

```bash
duckstring catchment set-root <root_dir> [-f|--file <path>]
```

## catchment species

See `docs/catchment/species/README.md`.

## catchment ponds

See detailed pond source docs in `docs/catchment/ponds/README.md`.
