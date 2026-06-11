---
title: Installation
description: Install the duckstring package and CLI.
---

# Installation

Duckstring is a single Python package containing the runtime library, the Catchment server, and the CLI:

```bash
pip install duckstring
```

Python **3.10 or later** is required. The install brings in DuckDB (the embedded analytical database every Pond computes against), FastAPI and uvicorn (the Catchment server), and Typer (the CLI).

## The CLI

The package installs two equivalent entry points — `duckstring` and the shorthand `ds`:

```bash
duckstring --help
ds --help
```

Shell completions (bash, zsh, fish, PowerShell) can be installed with:

```bash
duckstring --install-completion
```

## Verify

```bash
duckstring catchment list
```

A fresh install reports no Catchments registered and suggests how to create one — which is exactly the next step: head to the [Quickstart](quickstart.md).
