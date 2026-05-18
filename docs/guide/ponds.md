# Ponds

A **Pond** is the core version-controlled unit in Duckstring's overall framework. It comes in three flavours:

- **Inlet**: A root node in the DAG, no upstream Ponds
- **Outlet**: A leaf node in the DAG, no downstream Ponds
- **Pond**: A general Pond, expected to have both upstream and downstream Ponds

Outlets are explicitly labelled, despite theoretically only being an Outlet until a downstream Pond consumes from it. This is because Ponds designed explicitly for *external consumption* are typically very different to those intended only to perform a logical operation. A Pond is designed with a usage contract in mind for *other Ponds* - an Outlet is designed for an *external use case*.

Similarly, Inlets are explicitly labelled, as Ponds that interface at all with external data typically require explicit design. If a Pond has *any* external data sources, it should be designed to have *only* external data sources, with a downstream Pond consuming from it.

Generally, it's better to have too many Ponds than it is to have too few.

## Parents and Versioning

Each Pond defines its **Sources** (parent Ponds) and their minimum version, accepting any greater version within the same *major*. Ponds use strict semantic versioning (SemVer):

- *major*: Breaking change, e.g. schema change, table deletes, logic change
- *minor*: Non-breaking change, e.g. addition of new columns or tables, small logical changes with no potential downstream impact
- *patch*: Return to intended state, e.g. removal of an incorrect filter

A key aspect of the design is that multiple *major* versions for a given Pond could be executing concurrently - a Pond version only stops executing (and does so automatically) when it has no active Ponds depending on it downstream.

## Structure

A simple Pond project has this structure:

```
root/
|-- src/
|   |-- pond.py
|-- pond.toml
|-- __main__.py
|-- .gitignore
|-- README.md
```

### `pond.toml`

This declares the project as a Pond, and lists necessary details like the Pond name and its Sources. Here is are examples for the demo Ponds `inlet`, `pond` and `outlet`:

#### `inlet`
```toml
[pond]
name = "inlet"
version = "1.0.0"
type = "inlet"
```

As an Inlet, it is sufficient to include only the name and version, and to flag it as an Inlet

#### `pond`
```toml
[pond]
name = "pond"
version = "1.0.0"

[sources]
inlet = "1.0.0"
```

As a general Pond, `type` does not need to be specified. 

Sources are listed by name with their minimum SemVer. This will always resolve to use the maximum available version within the same *major*.


#### `outlet`
```toml
[pond]
name = "outlet"
version = "1.0.0"
type = "outlet"

[sources]
pond = "2.0.0"

[catchment.dev]
    [catchment.dev.sources]
        pond = "2.0.0?"

[catchment.qa]
    # Inherit defaults

[catchment.prod]
    # Inherit defaults
```

This demonstrates the ability to specify different details for each named Catchment. This should be rare and is generally discouraged, but it is possible that a given Pond name may not be globally unique in a specific Catchment, necessitating a rename.

Note the "?" in [catchment.dev.sources]. This flags the Source as "not required", meaning the Pond will execute even if 