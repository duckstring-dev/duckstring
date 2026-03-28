# Basin CLI

This document describes the `duckstring basin ...` command group.

## Command Group

- `duckstring basin`
- `duckstring basin list`
- `duckstring basin show <BASIN_NAME>`
- `duckstring basin create [BASIN_NAME]`
- `duckstring basin hydrate <BASIN_NAME>`
- `duckstring basin pulse <BASIN_NAME>`

`duckstring basin` with no subcommand lists basin directories in `./basins`.

## Command Order

Basin commands use command-first ordering:

- `duckstring basin hydrate <BASIN_NAME>`
- `duckstring basin pulse <BASIN_NAME>`

This replaces the old basin-first form (`duckstring basin <BASIN_NAME> hydrate`).

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

### Interactive mode

```bash
duckstring basin create -i
```

Interactive flow:

1. Basin name
2. Catchment path
3. Basin mode
4. Optional outlet pond targets (`pond` + semver `x.y.z`)
5. Confirmation

## basin show

Print a summary of basin name, mode, catchment path, hydration state, and outlets.

```bash
duckstring basin show <BASIN_NAME> [-s|--spec <path>]
```

`--spec` defaults to `basins/<name>/basin.json` and is resolved relative to the basin directory when not absolute.

## basin hydrate

Hydrate a basin spec and write hydrated metadata back to the spec file.

```bash
duckstring basin hydrate <BASIN_NAME> [-s|--spec <path>]
```

Options:

- `--no-pull` to skip pulling catchment pond sources before hydration.

## basin pulse

Run a pulse for a hydrated basin.

```bash
duckstring basin pulse <BASIN_NAME> [-s|--spec <path>]
```
