# Basin CLI

This document describes the `duckstring basin ...` command group.

## V1 Constraints

- Pulse mode only
- Local execution only
- DuckDB engine only

## Command Group

- `duckstring basin`
- `duckstring basin list`
- `duckstring basin show <BASIN_NAME>`
- `duckstring basin create [BASIN_NAME]`
- `duckstring basin hydrate <BASIN_NAME>`
- `duckstring basin pulse <BASIN_NAME>`
- `duckstring basin run <BASIN_NAME>`

`duckstring basin` with no subcommand lists basin directories in `./basins`.

## Command Order

Basin commands use command-first ordering:

- `duckstring basin hydrate <BASIN_NAME>`
- `duckstring basin pulse <BASIN_NAME>`
- `duckstring basin run <BASIN_NAME>`

## basin create

Create `basins/<name>/basin.json`.

```bash
duckstring basin create [BASIN_NAME] [options]
```

Options:

- `--catchment-path <path>` (default: `catchment.json`)
- `--mode <mode>` (default: `pulse`)
- `--outlet <pond=version>` (repeatable)
- `--interactive|-i`
- `--force`

## basin show

Print a summary of basin name, mode, catchment path, hydration state, and outlets.

```bash
duckstring basin show <BASIN_NAME> [-s|--spec <path>]
```

## basin hydrate

Hydrate a basin spec and write hydrated metadata back to the spec file.

```bash
duckstring basin hydrate <BASIN_NAME> [-s|--spec <path>]
```

Options:

- `--no-pull` to skip pulling catchment pond sources before hydration.

## basin pulse

Run a pulse for an already hydrated basin.

```bash
duckstring basin pulse <BASIN_NAME> [-s|--spec <path>]
```

## basin run

Convenience command to run a basin end-to-end.
By default, it hydrates first (including source pull), saves hydration metadata, and then pulses.

```bash
duckstring basin run <BASIN_NAME> [-s|--spec <path>] [options]
```

Options:

- `--no-hydrate` to skip auto-hydration
- `--no-pull` to skip pulling sources during hydration
