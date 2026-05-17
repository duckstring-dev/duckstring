# Duckstring V1 End-to-End Demo

This demo walks through a full Duckstring v1 workflow from an empty project to versioned pipeline execution.

You will build:

- A new project
- A catchment with one inlet landing location
- Three ponds (`scope` -> `enrich` -> `aggregate`)
- Snapshot runs for each pond during development
- Basin hydration + pulse runs after each pond stage
- A breaking-change upversion (`enrich@2.0.0`) and downstream adoption (`aggregate@2.0.0`)

The final result is a working project that demonstrates Duckstring v1 constraints:

- local execution only
- DuckDB only
- pulse mode only

## 1) Prerequisites

- Python 3.10+
- `git` installed
- ~1 GB free disk (the January NYC taxi parquet is large)

If Duckstring is on PyPI:

```bash
python -m venv .venv
source .venv/bin/activate
pip install duckstring
```

If you are developing from this repository:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e /path/to/duckstring-repo
```

Optional: enable CLI completion for your shell (recommended):

```bash
duckstring --install-completion
# or, if using module invocation:
python -m duckstring --install-completion
```

## 2) Start a New Project

```bash
mkdir duckstring-demo
cd duckstring-demo
mkdir -p landing/yellow_taxi_2023_01
mkdir -p ponds/scope/1.0.0
mkdir -p ponds/enrich/1.0.0
mkdir -p ponds/aggregate/1.0.0
mkdir -p snapshots
```

## 3) Create the Catchment

```bash
duckstring catchment create catchment.json
duckstring catchment show catchment.json
duckstring catchment validate catchment.json
```

## 4) Add Inlet + Populate Landing Data

### Interactive-first (recommended)

Run:

```bash
duckstring catchment inlets add yellow_taxi_jan2023 \
  --path ./landing/yellow_taxi_2023_01 \
  --glob "*.parquet" \
  -f catchment.json
```

(There is currently no `-i` mode for `inlets add`; this command is direct.)

Download example parquet:

```bash
curl -L \
  "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2023-01.parquet" \
  -o landing/yellow_taxi_2023_01/yellow_tripdata_2023-01.parquet
```

Verify:

```bash
duckstring catchment inlets list -f catchment.json
duckstring catchment inlets show yellow_taxi_jan2023 -f catchment.json
```

## 5) Create the Three Ponds

### 5.1 `scope@1.0.0`

Create `ponds/scope/1.0.0/pond.py`:

```python
import ibis
from duckstring import Pond


def pond():
    p = Pond(name="scope", description="Select scoped trip columns", version="1.0.0")

    raw = p.inlet("yellow_taxi_jan2023")

    scoped = raw.select(
        raw.tpep_pickup_datetime.name("pickup_ts"),
        raw.tpep_dropoff_datetime.name("dropoff_ts"),
        raw.PULocationID.cast("int64").name("pickup_location_id"),
        raw.DOLocationID.cast("int64").name("dropoff_location_id"),
        raw.passenger_count.cast("int64").name("passenger_count"),
        raw.trip_distance.cast("float64").name("trip_distance"),
        raw.total_amount.cast("float64").name("total_amount"),
    )

    p.sink({"trips_scope": scoped})
    p.flow([None], notes="Scope raw taxi parquet to required columns")
    return p
```

### 5.2 `enrich@1.0.0`

Create `ponds/enrich/1.0.0/pond.py`:

```python
import ibis
from duckstring import Pond


def pond():
    p = Pond(name="enrich", description="Add derived features", version="1.0.0")
    p.source({"scope": "1.0.0"})

    scoped = p.upstream["scope"].get(
        "trips_scope",
        {
            "pickup_ts": "pickup_ts",
            "dropoff_ts": "dropoff_ts",
            "pickup_location_id": "pickup_location_id",
            "dropoff_location_id": "dropoff_location_id",
            "passenger_count": "passenger_count",
            "trip_distance": "trip_distance",
            "total_amount": "total_amount",
        },
    )

    distance_bucket = (
        ibis.case()
        .when(scoped.trip_distance < 1, "short")
        .when(scoped.trip_distance < 3, "medium")
        .else_("long")
        .end()
    )

    enriched = scoped.mutate(
        pickup_date=scoped.pickup_ts.date(),
        distance_bucket=distance_bucket,
        high_value_trip_int=(scoped.total_amount >= 40).cast("int64"),
    )

    p.sink({"trips_enriched": enriched})
    p.flow([None], notes="Add date and trip segmentation features")
    return p
```

### 5.3 `aggregate@1.0.0`

Create `ponds/aggregate/1.0.0/pond.py`:

```python
from duckstring import Pond


def pond():
    p = Pond(name="aggregate", description="Aggregate enriched trips", version="1.0.0")
    p.source({"enrich": "1.0.0"})

    enriched = p.upstream["enrich"].get(
        "trips_enriched",
        {
            "pickup_date": "pickup_date",
            "distance_bucket": "distance_bucket",
            "trip_distance": "trip_distance",
            "total_amount": "total_amount",
            "high_value_trip_int": "high_value_trip_int",
        },
    )

    grouped = (
        enriched.group_by(["pickup_date", "distance_bucket"])
        .aggregate(
            trip_count=enriched.count(),
            total_revenue=enriched.total_amount.sum(),
            avg_trip_distance=enriched.trip_distance.mean(),
            high_value_trip_count=enriched.high_value_trip_int.sum(),
        )
    )

    p.sink({"trips_by_day_distance": grouped})
    p.flow([None], notes="Daily revenue and counts by distance bucket")
    return p
```

## 6) Register Ponds in Catchment (Interactive)

### Interactive-first (recommended)

Run:

```bash
duckstring catchment ponds add -i -f catchment.json
```

Respond:

1. Source Type: `local`
2. Scope: `catalog`
3. Catalog root path: `./ponds`
4. Confirm add: `yes`

Then pull and verify:

```bash
duckstring catchment ponds pull -f catchment.json
duckstring catchment ponds list-pulled -f catchment.json
```

### Shortcut (non-interactive)

```bash
duckstring catchment ponds add \
  --source-type local \
  --scope catalog \
  --root ./ponds \
  --force \
  -f catchment.json
```

## 7) Development Loop Per Pond

This is the workflow you can repeat while developing each stage:

1. Define snapshot file for the pond
2. Run snapshot and inspect output
3. Run that pond via basin (`hydrate` + `pulse`) and inspect with `periscope`

### 7A) `scope` development

Create `snapshots/scope_snapshot.py`:

```python
from pathlib import Path
import importlib.util

from duckstring import Catchment, Snapshot


ROOT = Path(__file__).resolve().parents[1]


def load_pond_factory(path: Path):
    spec = importlib.util.spec_from_file_location("scope_pond", str(path))
    mod = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)
    return mod.pond


def main():
    source = Catchment.load(ROOT / "catchment.json")
    sink = Catchment(root_dir=str(ROOT / ".snapshot_runs" / "scope"))

    snap = Snapshot(
        name="scope_dev",
        description="scope dev snapshot",
        pond=load_pond_factory(ROOT / "ponds" / "scope" / "1.0.0" / "pond.py"),
        source_catchment=source,
        sink_catchment=sink,
    )
    snap.flow(verbose=True)


if __name__ == "__main__":
    main()
```

Run and inspect snapshot output:

```bash
python snapshots/scope_snapshot.py
python - <<'PY'
import duckdb
con = duckdb.connect()
path = '.snapshot_runs/scope/snapshots/scope/1.0.0/output/trips_scope.parquet'
print(con.execute(f"select count(*) as n from read_parquet('{path}')").fetchdf())
print(con.execute(f"select * from read_parquet('{path}') limit 5").fetchdf())
con.close()
PY
```

Now run pond through basin.

#### Create basin interactively (recommended)

```bash
duckstring basin create taxi_dev -i --force
```

Respond:

1. Basin name: `taxi_dev`
2. Catchment path: `catchment.json`
3. Basin mode: `pulse`
4. Add outlets now: `yes`
5. Outlet pond: `scope`
6. Outlet version: `1.0.0`
7. Add another outlet: `no`
8. Confirm: `yes`

Hydrate + pulse + inspect:

```bash
duckstring basin hydrate taxi_dev
duckstring basin pulse taxi_dev
duckstring periscope scope --version 1.0.0 trips_scope --limit 5
```

#### Shortcut

```bash
duckstring basin create taxi_dev --catchment-path catchment.json --outlet scope=1.0.0 --force
```

### 7B) `enrich` development (source is previous pond `scope`)

Create `snapshots/enrich_snapshot.py`:

```python
from pathlib import Path
import importlib.util

from duckstring import Catchment, Snapshot


ROOT = Path(__file__).resolve().parents[1]


def load_pond_factory(path: Path):
    spec = importlib.util.spec_from_file_location("enrich_pond", str(path))
    mod = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)
    return mod.pond


def main():
    # Use basin-produced data from .duckstring/data as source for upstream contracts.
    source = Catchment(root_dir=str(ROOT / ".duckstring"))
    sink = Catchment(root_dir=str(ROOT / ".snapshot_runs" / "enrich"))

    snap = Snapshot(
        name="enrich_dev",
        description="enrich dev snapshot",
        pond=load_pond_factory(ROOT / "ponds" / "enrich" / "1.0.0" / "pond.py"),
        source_catchment=source,
        sink_catchment=sink,
    )
    snap.flow(verbose=True)


if __name__ == "__main__":
    main()
```

Run and inspect snapshot output:

```bash
python snapshots/enrich_snapshot.py
python - <<'PY'
import duckdb
con = duckdb.connect()
path = '.snapshot_runs/enrich/snapshots/enrich/1.0.0/output/trips_enriched.parquet'
print(con.execute(f"select count(*) as n from read_parquet('{path}')").fetchdf())
print(con.execute(f"select * from read_parquet('{path}') limit 5").fetchdf())
con.close()
PY
```

Update basin outlet interactively:

```bash
duckstring basin create taxi_dev -i --force
```

Respond with same first values, but outlet as:

- Outlet pond: `enrich`
- Outlet version: `1.0.0`

Hydrate + pulse + inspect:

```bash
duckstring basin hydrate taxi_dev
duckstring basin pulse taxi_dev
duckstring periscope enrich --version 1.0.0 trips_enriched --limit 5
```

Shortcut:

```bash
duckstring basin create taxi_dev --catchment-path catchment.json --outlet enrich=1.0.0 --force
```

### 7C) `aggregate` development (source is previous pond `enrich`)

Create `snapshots/aggregate_snapshot.py`:

```python
from pathlib import Path
import importlib.util

from duckstring import Catchment, Snapshot


ROOT = Path(__file__).resolve().parents[1]


def load_pond_factory(path: Path):
    spec = importlib.util.spec_from_file_location("aggregate_pond", str(path))
    mod = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)
    return mod.pond


def main():
    source = Catchment(root_dir=str(ROOT / ".duckstring"))
    sink = Catchment(root_dir=str(ROOT / ".snapshot_runs" / "aggregate"))

    snap = Snapshot(
        name="aggregate_dev",
        description="aggregate dev snapshot",
        pond=load_pond_factory(ROOT / "ponds" / "aggregate" / "1.0.0" / "pond.py"),
        source_catchment=source,
        sink_catchment=sink,
    )
    snap.flow(verbose=True)


if __name__ == "__main__":
    main()
```

Run and inspect snapshot output:

```bash
python snapshots/aggregate_snapshot.py
python - <<'PY'
import duckdb
con = duckdb.connect()
path = '.snapshot_runs/aggregate/snapshots/aggregate/1.0.0/output/trips_by_day_distance.parquet'
print(con.execute(f"select count(*) as n from read_parquet('{path}')").fetchdf())
print(con.execute(f"select * from read_parquet('{path}') limit 5").fetchdf())
con.close()
PY
```

Update basin outlet interactively:

```bash
duckstring basin create taxi_dev -i --force
```

Outlet values:

- Outlet pond: `aggregate`
- Outlet version: `1.0.0`

Hydrate + pulse + inspect:

```bash
duckstring basin hydrate taxi_dev
duckstring basin pulse taxi_dev
duckstring periscope aggregate --version 1.0.0 trips_by_day_distance --limit 10
```

Shortcut:

```bash
duckstring basin create taxi_dev --catchment-path catchment.json --outlet aggregate=1.0.0 --force
```

## 8) Breaking Change Demo (`enrich` upversion + downstream adoption)

Create new version directories:

```bash
mkdir -p ponds/enrich/2.0.0
mkdir -p ponds/aggregate/2.0.0
```

### 8.1 Breaking `enrich@2.0.0`

Create `ponds/enrich/2.0.0/pond.py` (replace `distance_bucket` with `distance_band`):

```python
import ibis
from duckstring import Pond


def pond():
    p = Pond(name="enrich", description="Add derived features (v2)", version="2.0.0")
    p.source({"scope": "1.0.0"})

    scoped = p.upstream["scope"].get(
        "trips_scope",
        {
            "pickup_ts": "pickup_ts",
            "trip_distance": "trip_distance",
            "total_amount": "total_amount",
        },
    )

    distance_band = (
        ibis.case()
        .when(scoped.trip_distance < 2, "near")
        .when(scoped.trip_distance < 6, "mid")
        .else_("far")
        .end()
    )

    spend_segment = (
        ibis.case()
        .when(scoped.total_amount < 20, "low")
        .when(scoped.total_amount < 50, "medium")
        .else_("high")
        .end()
    )

    enriched = scoped.mutate(
        pickup_date=scoped.pickup_ts.date(),
        distance_band=distance_band,
        spend_segment=spend_segment,
        high_value_trip_int=(scoped.total_amount >= 40).cast("int64"),
    )

    p.sink({"trips_enriched": enriched})
    p.flow([None], notes="v2 breaking change")
    return p
```

### 8.2 Update `aggregate@2.0.0` to consume `enrich@2.0.0`

Create `ponds/aggregate/2.0.0/pond.py`:

```python
from duckstring import Pond


def pond():
    p = Pond(name="aggregate", description="Aggregate enriched trips (v2)", version="2.0.0")
    p.source({"enrich": "2.0.0"})

    enriched = p.upstream["enrich"].get(
        "trips_enriched",
        {
            "pickup_date": "pickup_date",
            "distance_band": "distance_band",
            "spend_segment": "spend_segment",
            "trip_distance": "trip_distance",
            "total_amount": "total_amount",
            "high_value_trip_int": "high_value_trip_int",
        },
    )

    grouped = (
        enriched.group_by(["pickup_date", "distance_band", "spend_segment"])
        .aggregate(
            trip_count=enriched.count(),
            total_revenue=enriched.total_amount.sum(),
            avg_trip_distance=enriched.trip_distance.mean(),
            high_value_trip_count=enriched.high_value_trip_int.sum(),
        )
    )

    p.sink({"trips_by_day_distance": grouped})
    p.flow([None], notes="v2 aggregate")
    return p
```

Pull updated pond versions:

```bash
duckstring catchment ponds pull -f catchment.json
```

Set basin outlet to `aggregate@2.0.0` interactively:

```bash
duckstring basin create taxi_dev -i --force
```

Outlet values:

- Outlet pond: `aggregate`
- Outlet version: `2.0.0`

Hydrate + pulse:

```bash
duckstring basin hydrate taxi_dev
duckstring basin pulse taxi_dev
```

Verify versioned outputs:

```bash
duckstring periscope enrich --list-versions
duckstring periscope aggregate --list-versions
duckstring periscope aggregate --version 1.0.0 trips_by_day_distance --limit 5
duckstring periscope aggregate --version 2.0.0 trips_by_day_distance --limit 5
```

Shortcut basin update:

```bash
duckstring basin create taxi_dev --catchment-path catchment.json --outlet aggregate=2.0.0 --force
```

## 9) Expected Project Layout (Abridged)

```text
duckstring-demo/
  catchment.json
  landing/
  ponds/
    scope/1.0.0/pond.py
    enrich/1.0.0/pond.py
    enrich/2.0.0/pond.py
    aggregate/1.0.0/pond.py
    aggregate/2.0.0/pond.py
  snapshots/
    scope_snapshot.py
    enrich_snapshot.py
    aggregate_snapshot.py
  basins/
    taxi_dev/basin.json
  .duckstring/
    ponds/...
    data/
      scope/1.0.0/...
      enrich/1.0.0/...
      enrich/2.0.0/...
      aggregate/1.0.0/...
      aggregate/2.0.0/...
    state/
```

## Notes

- Ingestion into landing paths is external to Duckstring; Duckstring starts from already landed files.
- Snapshot runs are local development checks; basin hydration/pulse is your integrated runtime check.
- During development, interactive commands are recommended; shortcut forms are shown last for automation.
