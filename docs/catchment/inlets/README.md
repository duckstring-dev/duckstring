# Catchment Inlets CLI

This document describes `duckstring catchment inlets ...`.

Inlet locations are named landing paths that inlet ponds can reference via `Pond.inlet("name")`.
In v1, inlet locations are local parquet paths only.

## Command Group

- `duckstring catchment inlets list`
- `duckstring catchment inlets show <NAME>`
- `duckstring catchment inlets add <NAME>`
- `duckstring catchment inlets remove <NAME>`

All commands accept `-f|--file <path>` (default: `catchment.json`) where applicable.

## inlets list

List configured inlet locations.

```bash
duckstring catchment inlets list [-f|--file <path>]
```

## inlets show

Print JSON for one inlet location.

```bash
duckstring catchment inlets show <name> [-f|--file <path>]
```

## inlets add

Add or update an inlet location.

```bash
duckstring catchment inlets add <name> [options]
```

Options:

- `--path|-p <path>` (required)
- `--format <format>` (default: `parquet`, v1 only)
- `--glob <pattern>` (optional)
- `--overwrite`
- `-f|--file <path>`

Stored shape:

```json
{
  "kind": "local",
  "path": "./landing/orders",
  "format": "parquet",
  "glob": "*.parquet"
}
```

## inlets remove

Remove an inlet location.

```bash
duckstring catchment inlets remove <name> [-f|--file <path>]
```

## Pond Usage

Example:

```python
from duckstring import Pond


def pond():
    p = Pond(name="inlet_orders", description=None, version="1.0.0")
    landed = p.inlet("landing_orders")
    p.sink({"out": landed})
    p.flow([None])
    return p
```
