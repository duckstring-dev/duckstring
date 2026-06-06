# Duckstring
*There is no DAG.*

Duckstring treats data transformations as software packages. Upstream dependencies are declared per Pond (unit operation), defining the DAG without the need for its direct management. 

Ponds are upgraded and deployed to Duckstring's pull-based Catchment (orchestrator) atomically - like upgrading a package - with the earlier version continuing to execute until there are no consumers dependent on it. Upstream defines constraints on what it can consume, downstream defines when it's needed, and the Catchment optimally executes the sequence of Ponds supplying it with the best currency and frequency as possible.

You should not need to manage the DAG. You should not need global governance. You should know yourself and your suppliers and trust that you'll get what you need when you need it.

## Core Concepts

The main elements:

- **Catchment**: Control environment (FastAPI + UI + CLI)
- **Pond**: Versioned container with declared upstream dependencies
- **Ripple**: Unit operation within a Pond (e.g. a single transformation producing a table)

Ponds are typed or referred to in context:

- **Source**: A parent Pond
- **Sink**: A child Pond
- **Inlet**: A Pond with xternal dependencies and no Sources
- **Outlet**: A Pond with no Sinks (e.g. outputs final data products)

## Installation

```bash
pip install duckstring
```

## Quickstart

### 1) Connect to a Catchment

A Catchment is the execution environment and orchestrator, receiving Ponds and managing runs. It runs either as a local daemon or as a remote server, allowing you to start locally and seamlessly upgrade to a hosted/cloud server if you need to later.

Starting or connecting to a Catchment selects it as the default target of later commands - other connected Catchments can be referred to with the `-c {catchment name}` option where relevant.

#### Start a Catchment Server

To initiate a Catchment locally, run:

```bash
duckstring catchment init --name dev --port 5000 --root ~/.duckstring/dev
```

This will start a server with name 'dev' (prompted if none specified) at port 5000 and store Catchment details at `~/.duckstring/dev` (default is `~/.duckstring/{name}`). 

This can later be restarted with:

```bash
duckstring catchment start dev
```

#### Connect to a Remote Server

Alternatively, you can connect to a server running a Catchment:

```bash
duckstring catchment connect --name dev --path https://path.to.catchment
```

This will prompt for any necessary auth, and will add the Catchment under the specified name.

#### Connect to *duckstring.com*

There are future plans for a dedicated Catchment service at https://duckstring.com. If you're interested, please [contact me](mailto:dev@duckstring.com).

#### Experiment in the Playground

Shipped with Duckstring is a standalone web app to demonstrate how the Catchment orchestrator behaves. Start it with:

```bash
duckstring catchment playground
```

You may also visit https://playground.duckstring.com

### 2) Define Pond(s)

#### Demo Ponds

It's recommended to look at an example before attempting to make your own Ponds so that you can get a feel for the structure. 

To do so, cd to a target directory for the demo Ponds and run:

```bash
duckstring pond demo
```

This will create a sequence of Ponds:

```mermaid
flowchart LR
    subgraph products [products]
        direction LR
        products.ingest[ingest: 2s]
    end

    subgraph transactions [transactions]
        direction LR
        transactions.ingest[ingest: 1s]
    end

    subgraph sales [sales]
        direction LR
        sales.price_tiers[price_tiers: 1s] --> sales.join_lines[join_lines: 3s]
        sales.daily_sales[daily_sales: 2s] --> sales.join_lines
    end

    subgraph reports [reports]
        direction LR
        reports.monthly_summary[monthly_summary: 1s]
    end

    products --> sales
    transactions --> sales
    sales --> reports
```

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
duckstring deploy 
```

This will read the Pond name, version and type (Inlet, Pond, Outlet) from `pond.toml` and deploy the project contents to the Catchment.

Alternatively, you can import the Pond using the Catchment UI.

To upload from all Ponds within a directory, use:

```bash
duckstring deploy --all
```

#### From Git

If you are using git with a remote, you can deploy with:

```bash
duckstring deploy --git {branch|commit|tag}
```

This will use the current branch/commit/tag to define the Pond. Upon each execution the Catchment will clone the repository and run it.

This can also be specified using the Catchment UI.

### 3) Execute

Ponds are executed by sending a demand signal from an Outlet. This propagates backwards through the DAG until it reaches each upstream Inlet, causing them to execute, with children beginning upon completion of all of their parents.

Demand comes in two types:

- **push**: Executes in sequence from start to end, bringing all Ponds upstream of the target to a given freshness
- **pull**: Executes whenever Sources update, passing demand upstream

If running irregularly, **push** is simple and preferred.
If running frequently (~speed of slowest Ripple), **pull** is preferred, as it naturally throttles the entire sequence to the rate of the bottleneck Ripple.

These are triggered either once or continuously (**push** on a schedule, **pull** back-to-back):

| | Once | Continuously |
|---|---|---|
| **Push** | Pulse | Tide |
| **Pull** | Tap | Wave |

These examples will use the example Pond `reports`, version `1.0.0`, as the execution reference. All examples may also be alternatively executed using the Catchment UI.

Each of these triggers starts a status monitor that polls the Catchment, hanging up on complete for Pulse and Tap and remaining open until closed (Ctrl+C) for Tide and Wave. 

#### Pulse

```bash
duckstring trigger pulse reports
```

This emits a **push** on `reports`, running each Pond in its lineage once.

#### Tide

```bash
duckstring trigger tide reports 4
```

This executes a Pulse on `reports` any time its staleness (or time since last Pulse) exceeds 4 seconds, causing it to update every 4 seconds. The pipeline takes 7 seconds to execute, so multiple Pulses will be active simultaneously.

You can set the unit for the staleness limit with `--unit {seconds|minutes|hours|days|months|years}`.

Cancel the Tide with:

```bash
duckstring trigger stop reports
```

#### Tap

```bash
duckstring trigger tap reports
```

This emits a **pull** on `reports`, which propagates upstream on idle. Whenever a Pond starts, it sends the **pull** to its Sources, often causing them to start their next generation at the same time. This prepares each Pond for a subsequent Tap to be supplied immediately.

It can be useful to trigger a Tap whenever an application queries an Outlet, so that it updates at a frequency matching its consumption.

#### Wave

```bash
duckstring trigger wave reports
```

This executes a Tap on `reports` any time it starts, causing it to update as frequently as the longest-running Ripple (bottleneck) upstream. The pipeline takes 7 seconds to execute, and the bottleneck is 3s, so multiple Taps will be active simultaneously.

Use Wave whenever data should be supplied as fresh as possible. No Pond (or Ripple) will execute more frequently than the bottleneck process can consume it.

Cancel the Wave with:

```bash
duckstring trigger stop reports
```

#### Windows

Windows set an allowed period in which an Inlet can start, with data considered 'fresh' until the end of that period. They are especially useful in cases where a foreign data source that the Inlet consumes from is known to update only at some frequency (e.g. daily).

It is particularly useful to couple Windows with Wave. A one-day Window with a Wave consumer downstream will run the sequence of Ponds between them *once* daily, matching a Tide with a one-day staleness limit. Unlike Tide, however, the time of the Window can be set for the Inlet explicitly, so that consumption only occurs when the foreign source is known to have updated. Periods of "do not consume" (e.g. during writes) can also be specified this way.

Set Windows against `products` with:

```bash
duckstring window products --for 4 --every 10 --offset 2 --unit seconds
```

This sets a 4s duration Window every 10s at a 2s offset. At 2:15pm, that means the next active Window would be at 2:15:02pm, lasting to 2:15:06pm.




#### Wave

```bash
duckstring trigger wave reports
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

