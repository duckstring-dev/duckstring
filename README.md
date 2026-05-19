# Duckstring

Duckstring is a data pipeline framework built around modular, versioned nodes called **Ponds**. Each Pond specifies its immediate parents (with version), allowing for the formation of a DAG much like one would install packages. 
Pond execution is orchestrated within an environment - a **Catchment** - that controls storage and other global settings. It uses a pull-based system modelled after Kanban, with **Outlets**  (terminal Ponds) sending demand upstream. This allows each Pond to be modified and deployed independently, with any paths in the DAG that are not attached to any Outlet automatically skipped. 
Duckstring is built on the philosophy that most data pipelines are not truly "big data" and with good design can execute on a single compute node. It is primarily designed for batch and incremental workloads for tables on the order of tens of millions of rows (e.g. <50M).
The default engine is DuckDB, though this is configurable. Duckstring is however an independent project and is not affiliated with, endorsed by, or maintained by the DuckDB project.

## Core Concepts

- **Catchment**: Control environment - a FastAPI application
- **Pond**: Versioned transformation unit with declared upstream dependencies - the main element of version control
- **Inlet**: Pond with external dependencies and no upstream Ponds
- **Outlet**: Pond with no downstream Ponds (e.g. outputs final data products)
- **Ripple**: Unit operation within a Pond (e.g. a single transformation producing a table)

## Installation

```bash
pip install duckstring
```

## Quickstart

### 1) Connect to a Catchment

A Catchment is the execution environment, receiving Ponds and managing runs. It runs either as a local daemon or as a remote server, allowing you to start locally and seamlessly upgrade to a hosted/cloud server if you need to later.

#### Start a Catchment Server

To run a Catchment locally, run:

```bash
duckstring catchment start --name dev --port 5000 --root ~/.duckstring/dev
```

This will start a server with name 'dev' at port 5000 (the default, if none specified) and store Catchment details at `~/.duckstring/dev` (default is `~/.duckstring/{name}`). If any of these options are omitted you will be prompted on start.

#### Connect to a Remote Server

Alternatively, you can connect to a server running a Catchment:

```bash
duckstring catchment connect --name dev --path https://path.to.catchment
```

This will prompt for any necessary auth, and will add the Catchment under the specified name.

#### Connect to *duckstring.com*

There are future plans for a dedicated Catchment service at https://duckstring.com. If you're interested, please [contact me](mailto:dev@isaacharvey.com).

### 2) Define Pond(s)

#### Demo Ponds

If you want to see an example sequence of Ponds in action immediately, create three project directories and run one of these commands in each:

```bash
duckstring pond demo inlet
duckstring pond demo pond
duckstring pond demo outlet
```

It's recommended to do this before attempting to make your own so that you can get a feel for the structure.

#### Custom Pond

Create a project directory and run:

```bash
duckstring pond init example_pond
```

This will create a duckstring pond structure:

```text
root/
|-- src/
|   |-- pond.py
|-- pond.toml
|-- __main__.py
|-- .gitignore
|-- README.md
```

Here `pond.py` contains the code for a single Ripple operation (currently blank), and `pond.toml` specifies the Pond name "example_pond" and version (defaulting to "0.1.0").

### 3) Deploy to Catchment

#### From Local

From a Pond's project root run:

```bash
duckstring deploy dev
```

This will read the pond name, version and type (Inlet, Pond, Outlet) from `pond.toml` and deploy the project contents to the Catchment specified by name (here `dev`).

Alternatively, you can import the Pond using the Catchment UI.

#### From Git

If you are using git with a remote, you can deploy with:

```bash
duckstring deploy dev --git {branch|commit|tag}
```

This will use the current branch/commit/tag to define the Pond. Upon each execution the Catchment will clone the repository and run it.

This can also be specified using the Catchment UI.

### 3) Execute

Ponds are executed by sending a Demand signal from an Outlet. This propagates backwards through the DAG until it reaches each upstream Inlet, causing them to execute, with children beginning upon completion of all of their parents.

These examples will use the Pond `outlet`, version `1.0.0`, as the execution reference. All examples may also be alternatively executed using the Catchment UI.

#### Pulse

To initiate a single run:

```bash
duckstring pulse dev outlet
```

The `pulse` mode emits a Demand signal from `outlet`, and when it begins execution, sends a Stop signal. This causes it to execute exactly once.

This will automatically run against the maximum version available for that Pond. To use a specific version:

```bash
duckstring pulse dev outlet --version 1
```

#### Wave

To continuously run:

```bash
duckstring wave dev outlet
```

The `wave` mode emits a Demand signal from `outlet`, and when it begins execution, sends *another* Demand signal. This causes it to execute continuously, as frequently as the DAG allows (i.e. at a period equal to the execution time of the slowest Ripple in any Pond).

#### Tide

To run at a scheduled frequency:

```bash
duckstring tide dev outlet 15 2 * * * --local
```

This would run at 2:15am every day local time, using cron syntax. Omitting the `--local` flag defaults to UTC.

### 4) Monitor

To print out a summary of current processes in the Catchment:

```bash
duckstring status dev
```

This will print to CLI a summary for each Pond that is either currently executing or has Demand.

To include all Ponds:

```bash
duckstring status dev --all
```

### 5) Retrieve Data

#### Get

The simplest way to retrieve data is to load by the Ripple name. This returns the entire contents of the directory, and does not require that the data be in a tabular format (e.g. SQL-compatible).

```bash
duckstring get dev outlet daily
```

This writes a directory `./ponds/outlet/daily` with the 'daily' Ripple's contents. You may also override the default location:

```bash
duckstring get dev outlet daily --path ./daily_output
```

#### SQL Query

If the target is an SQL-compatible table (e.g. DuckDB or Parquet), an SQL statement may be sent directly, outputting the result to the command line:

```bash
duckstring query dev outlet --sql "SELECT * FROM daily WHERE id=1;"
```

Alternatively, include a file path:

```bash
duckstring query dev outlet --sql @path/to/query.sql
```

Omitting the `--sql` statement queries with a default SELECT * LIMIT 10 on the specified table:

```bash
duckstring query dev outlet daily
```

##### Write to file

To output to a file, include a flag for the file format, followed by the file name:

`--csv`: Comma-separated values
`--json`: JSON records
`--parquet`: Parquet file

This writes by default to `./ponds/outlet/daily/{filename}`. To overrite the default location you may use the `--path` flag.

For example, to execute an sql statement from file `query.sql` and write the result to CSV at the current directory:

```bash
duckstring query dev outlet --sql @query.sql --csv daily.csv --path .
```

## Further Reading

For more detail on each component, please read the corresponding documentation in `docs/`.

