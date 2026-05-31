# Theory

This document outlines the theory governing the orchestration mechanics. 

## Motivation

Most of the time when running a sequence of data transformations (a pipeline), the process is set to run either on a schedule (e.g. cron job) or continuously (new run triggered immediately upon completion of previous). This certainly satisfies many purposes, but can often be wasteful - in currency (data freshness), compute, or both.

### General Constraints

Consider a process consisting of three transformations - call them "unit operations" - in series, each taking 10 minutes to complete. The total time from start to end of the pipeline (the *lead time*) is the sume of these durations: 30 minutes. More generally, where some operations run in parallel, the total lead time is the sum duration of the unit operations on the *critical path* - the longest route through the process.

For:

- Set of unit operations on critical path $P$
- $n$ total unit operations on critical path
- Unit operation $k$, where $k \in P$
- Duration $d_k$

$$
{Lead\ Time}=\sum_{k \in P}^{n} d_k
$$

Lead time like this is unavoidable and optimal. However, when run continuously, the total age of results (the *staleness*) can be up to double this. The minimum period between completed runs (the *cycle time*) for such continuous operation is equal to the lead time when running back-to-back like this. 

The result is a sawtooth function of staleness $T$, ranging from lead time $L$ to cycle time $C$ above that:

$$
T(t) = L + (t \bmod C)
$$

In general, the worst case currency is $L + C$, equal to $2L$ in the case of back-to-back execution.

### Continuous Parallel Execution

One way to reduce staleness is to run as much of the pipeline as possible in parallel. If instead of running the pipeline back-to-back, we run every *unit operation* back-to-back, the cycle time reduces to the duration of the longest unit operation (the *bottleneck*):

$$
C = \max_{k \in P} d_k
$$

If every unit operation is approximately the same duration, this optimally trades additional compute time for minimised staleness. However, if there is a great difference in durations for each unit operation, the faster operations will run more frequently than they can be consumed - their results (and compute) wasted.

### Change Gating

Unnecessary runs can be avoided by setting each unit operation to only execute if there have been changes upstream, e.g. by watermarking rows or runs and keeping track of the most recently consumed results from upstream. This is very common and effective, as it causes every unit operation to run at a minimum period equal to the maximum period of all operations upstream - that is, operations downstream are throttled by the upstream bottleneck.

There is, however, no such throttling for operations *upstream* of the bottleneck. If a bottleneck much longer than the other operations occurs late in the sequence of, most of the effort upstream is wasted.

### Globally-Defined Pipelines

In most pipeline orchestration systems, it's required to directly specify in some global context the graph of operations - often called the DAG (Directed Acyclic Graph). This very simply manages the sequencing of unit operations such that each runs only once the previous has completed, and allows setting the DAG to run on a given schedule (or upon some trigger).

This approach is often trivial for small DAGs, and satisfies most purposes. However, as it is governed globally, it requires significant oversight and can become unwieldy for very large DAGs. A change to any unit operation often requires rerunning the DAG from start to finish. Generally, only one run of the pipeline concurrently can be safely executed without side effects, meaning the staleness is rarely close to optimal.

If a path on the DAG is rarely used (or stops being used entirely), managing this can be difficult. At a minimum, the rate of update must be governed by some central authority, which can be difficult to do effectively in larger teams. Some options are:

- Maintain a separate DAG for lower-frequency paths
    - Difficult if they consume data from a higher-frequency path, e.g. aligning with completion times
- Just execute more frequently than necessary
    - Wasteful, though often done in practice due to governance difficulties

## Pull vs Push

Most of the approaches discussed above are considered *push* systems, borrowing terminology from scheduling in manufacturing, where the completion of some task is pushed downstream to enable further processing. The scheduling is inherently *supply-driven*, where the availability of some supply is what enables processing to continue. This requires accurate anticipation of consumption rate to avoid overproduction.

The alternative is *pull*, where scheduling is *demand-driven*. Under this approach, operations execute because of the presence of demand downstream. This has some advantages:

- Work is only done if there is consumption
- No demand forecasting is required - production rate naturally matches demand
- Unused/low-use paths in a DAG are automatically shut down or throttled to match their consumption rate
- Continuous execution (with change-gating) is throttled both upstream *and* downstream of the bottleneck

### Kanban

Kanban is a famously simple pull-based scheduling process, pioneered by Japanese manufacturing (especially Toyota). It involves sending tokens (classically, physical cards) back to a supplier when a product is consumed, allowing the supplier to keep track of how much stock has been consumed. Crucially, the tokens are delivered at the *start* of their being used for the downstream process, allowing the supplier to begin production immediately so that stock is available the next time it is needed.

Typically, the supplier will then log these tokens against a range:

- Red: High tokens, indicating high consumption and low stock -> accelerate production
- Yellow: Moderate tokens, standard consumption and stock -> standard production
- Green: Low tokens, low consumption and high stock -> stop production

Unlike manufacturing, where the number of units is meaningful, data pipelines are binary - either updated or not. Consequently, a Kanban-like process need only track the presence of *any* demand, where the Red/Green boundary is simply one:

- Has demand: Start production
- Has no demand: Stop production

### Demand-Based Orchestration

It is helpful to imagine a unit operation as a node in a directed graph (the DAG). Each node is aware of its parents, and can notify the parents of their demand or otherwise send signals upstream. A node does *not* necessarily have awareness of its children - only the capability to receive signals from them.

Each node follows the simple rules:

- Have my parents updated?
    - Change gating
    - Emulates a consumer being unable to proceed if there is no stock from a supplier
- Have I received demand from anyone downstream?
    - Demand gating
    - Emulates a consumer sending a Kanban token to its supplier
- If both (and I'm not already processing):
    - Send demand to all my parents
    - Clear my own demand
    - Start processing
- When my processing completes:
    - Indicate I have updated, so that processes waiting on me can begin

#### Examples

Consider a simple chain of nodes:

```mermaid
flowchart LR
    classDef running fill:#4CAF50,stroke:#388E3C,stroke-width:2px,color:#fff;
    classDef queued fill:#2196F3,stroke:#1976D2,stroke-width:1px,color:#fff;

    A .-> B
    B .-> C
```

Nodes without demand are no colour, nodes with demand but no changes upstream (queued) are orange, nodes that are running are blue, and those that are running *and have demand* (demanded) are green:

```mermaid
flowchart LR
    classDef running fill:#2196F3,stroke:#1976D2,color:#fff;
    classDef queued fill:#FF9800,stroke:#F57C00,color:#fff;
    classDef demanded fill:#4CAF50,stroke:#388E3C,color:#fff;

    A[Idle] .-> B[Queued]:::queued
    B .-> C[Running]:::running
    C .-> D[Demanded]:::demanded
```

We will denote the number of times a node has updated (its *generation*) with a colon, such that "A:3" indicates A has run 3 times. If a node is ahead of its child, the edge between them will be solid - otherwise, it will be dotted.

1) All start idle, with each node one generation ahead of the one after it:

    ```mermaid
    flowchart LR
        classDef running fill:#2196F3,stroke:#1976D2,color:#fff;
        classDef queued fill:#FF9800,stroke:#F57C00,color:#fff;
        classDef demanded fill:#4CAF50,stroke:#388E3C,color:#fff;

        A[A:3] --> B[B:2]
        B --> C[C:1]
    ```

2) C is given demand and enters the queued state:

    ```mermaid
    flowchart LR
        classDef running fill:#2196F3,stroke:#1976D2,color:#fff;
        classDef queued fill:#FF9800,stroke:#F57C00,color:#fff;
        classDef demanded fill:#4CAF50,stroke:#388E3C,color:#fff;

        A[A:3] --> B[B:2]
        B --> C[C:1]:::queued
    ```

3) As its parent is a generation ahead, it starts a run:

    1) Demand is sent to its parent, putting it in the queued state:

    ```mermaid
    flowchart LR
        classDef running fill:#2196F3,stroke:#1976D2,color:#fff;
        classDef queued fill:#FF9800,stroke:#F57C00,color:#fff;
        classDef demanded fill:#4CAF50,stroke:#388E3C,color:#fff;

        A[A:3] --> B[B:2]:::queued
        B --> C[C:1]:::queued
    ```

    2) Demand is cleared and the node started, putting it in the running state:

    ```mermaid
    flowchart LR
        classDef running fill:#2196F3,stroke:#1976D2,color:#fff;
        classDef queued fill:#FF9800,stroke:#F57C00,color:#fff;
        classDef demanded fill:#4CAF50,stroke:#388E3C,color:#fff;

        A[A:3] --> B[B:2]:::queued
        B --> C[C:1]:::running
    ```

4) B repeats the same, sending demand upstream and starting:

    ```mermaid
    flowchart LR
        classDef running fill:#2196F3,stroke:#1976D2,color:#fff;
        classDef queued fill:#FF9800,stroke:#F57C00,color:#fff;
        classDef demanded fill:#4CAF50,stroke:#388E3C,color:#fff;

        A[A:3] --> B[B:2]:::running
        B --> C[C:1]:::running
    ```

5) A has no parents, so can start as soon as it receives demand:

    ```mermaid
    flowchart LR
        classDef running fill:#2196F3,stroke:#1976D2,color:#fff;
        classDef queued fill:#FF9800,stroke:#F57C00,color:#fff;
        classDef demanded fill:#4CAF50,stroke:#388E3C,color:#fff;

        A[A:3]:::running --> B[B:2]:::running
        B --> C[C:1]:::running
    ```

6) All nodes run simultaneously


TODO:
- Outline behaviour with branching, e.g. with A -> C, B -> C, B -> D, to demonstrate that parents of less frequent pathways can be left stale while others execute
- 