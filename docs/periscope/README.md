# Periscope CLI

This document describes `duckstring periscope ...`.

Periscope inspects parquet tables materialized in a catchment data directory.
It resolves versions by prefix and defaults to the latest available version.

## Command

```bash
duckstring periscope <POND> [TABLE] [options]
```

Options:

- `--version|-v <prefix>` where prefix can be `MAJOR`, `MAJOR.MINOR`, or `MAJOR.MINOR.PATCH`
- `--list-versions` (or `-v` with no value)
- `--limit|-l <N>` row preview limit when table is provided
- `--no-head` disable row preview output

## Examples

List latest tables for pond:

```bash
duckstring periscope marts_orders
```

List versions for pond:

```bash
duckstring periscope marts_orders --list-versions
```

Inspect a specific table from the latest `1.2.x` version:

```bash
duckstring periscope marts_orders --version 1.2 out
```

Inspect an exact version with limit:

```bash
duckstring periscope marts_orders --version 1.2.3 out --limit 50
```
