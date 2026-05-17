# Duckstring

Duckstring is a local-first data pipeline framework built around modular, versioned nodes called **Ponds**.
A **Basin** is a dependency-resolved DAG of ponds with one or more target outputs (outlets), and a **Catchment** is the runtime boundary where ponds, state, and data are stored.

## V1 Scope

Duckstring v1 is intentionally narrow:

- Local execution only
- DuckDB engine only
- Pulse execution mode only
- Local parquet inlet locations only
- Pond code can be sourced from local catalogs or git repositories

The codebase keeps structure for future expansion, but v1 should be treated as a strict, stable baseline.

## Core Concepts

- **Catchment**: runtime root plus configuration (species, modes, pond sources, inlet locations)
- **Pond**: versioned transformation unit that declares upstream dependencies and output tables
- **Basin**: dependency-resolved plan built from outlet targets
- **Hydration**: materialize pond code + manifests into the catchment runtime
- **Pulse**: one full execution pass over a hydrated basin in topological order
- **Inlet Location**: named landing location (typically parquet files) that inlet ponds can read from

## Installation

```bash
pip install duckstring
```

## CLI Quickstart

### 1) Create a catchment

```bash
duckstring catchment create catchment.json
```

### 2) Register pond code sources

Local catalog example:

```bash
duckstring catchment ponds add \
  --source-type local \
  --scope catalog \
  --root ./ponds \
  --force \
  -f catchment.json
```

Git monorepo catalog example:

```bash
duckstring catchment ponds add \
  --source-type git \
  --scope catalog \
  --repo-structure monorepo \
  --repo git@github.com:your-org/ponds.git \
  --ref-type branch \
  --ref-pattern main \
  --root catalog \
  --force \
  -f catchment.json
```

### 3) Register inlet landing locations (optional)

```bash
duckstring catchment inlets add landing_orders \
  --path ./landing/orders \
  --glob "*.parquet" \
  -f catchment.json
```

### 4) Pull pond sources into the runtime

```bash
duckstring catchment ponds pull -f catchment.json
```

### 5) Create a basin

```bash
duckstring basin create analytics \
  --catchment-path catchment.json \
  --outlet marts_orders=1.0.0
```

### 6) Run the basin

`run` auto-hydrates by default, then pulses:

```bash
duckstring basin run analytics
```

### 7) Inspect materialized outputs

```bash
duckstring periscope marts_orders --version 1.0 --list-versions
duckstring periscope marts_orders --version 1.0.0 out
```

## Pond Authoring Example

```python
import ibis
from duckstring import Pond


def pond():
    p = Pond(name="marts_orders", description="Orders mart", version="1.0.0")

    # Read landed parquet files from a named inlet location in catchment.json
    landed = p.inlet("landing_orders")

    # Optionally read from upstream pond contracts
    # p.source({"stg_orders": "1.0.0"})
    # stg = p.upstream["stg_orders"].get("orders", {"order_id": "order_id"})

    out = landed.select("order_id", "customer_id", "order_total")

    p.sink({"out": out})
    p.flow([None])
    return p
```

## Documentation

- Basin CLI: `docs/basin/README.md`
- Catchment CLI: `docs/catchment/README.md`
- Catchment ponds: `docs/catchment/ponds/README.md`
- Catchment species: `docs/catchment/species/README.md`
- Catchment inlets: `docs/catchment/inlets/README.md`
- Periscope CLI: `docs/periscope/README.md`
- End-to-end demo: `docs/demo/README.md`

## Not In V1

- Non-local compute
- Non-DuckDB engines
- Non-pulse scheduling modes
- Managed ingestion/orchestration for landing locations (landing is expected to be populated by an external process)
