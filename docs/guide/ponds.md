# Ponds

A **Pond** is the core version-controlled unit in Duckstring's overall framework. It comes in three flavours:

- **Inlet**: A root node in the DAG, no upstream Ponds
- **Outlet**: A leaf node in the DAG, no downstream Ponds
- **Pond**: A general Pond, expected to have both upstream and downstream Ponds

Outlets are explicitly labelled, despite theoretically only being an Outlet until a downstream Pond consumes from it. This is because Ponds designed explicitly for *external consumption* are typically very different to those intended only to perform a logical operation. A Pond is designed with a usage contract in mind for *other Ponds* - an Outlet is designed for an *external use case*.



## Structure
```
root/
|-- src/
|   |-- pond.py
|-- pond.toml
|-- __main__.py
|-- .gitignore
|-- README.md
```