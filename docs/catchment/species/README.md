# Catchment Species CLI

This document describes `duckstring catchment species ...`.

## Command Group

- `duckstring catchment species list`
- `duckstring catchment species add`
- `duckstring catchment species remove`
- `duckstring catchment species set-default`

All commands accept `-f|--file <path>` (default: `catchment.json`) where applicable.

## species list

List configured species and mark the default species.

```bash
duckstring catchment species list [-f|--file <path>]
```

## species add

Add a species entry.

```bash
duckstring catchment species add <name> [options]
```

Options:

- `--kind <kind>` (default: `local`)
- `--engine <engine>` (default: `duckdb`)
- `--option <key=value>` (repeatable)
- `--overwrite`
- `-f|--file <path>`

## species remove

Remove a species entry.

```bash
duckstring catchment species remove <name> [--force] [-f|--file <path>]
```

`--force` also clears:

- `default_species` (if this species is currently default)
- `pond_species` mappings pointing to this species

## species set-default

Set default species.

```bash
duckstring catchment species set-default <name> [-f|--file <path>]
```
