"""SVG figures for the post "DuckDB v2 as a Pipeline Engine". The numbers are the benchmark's: the laptop runs in
bench/tpcds_chain/results/laptop_2026-10-23.json and the AWS runs in bench/tpcds_chain/results/aws/ (summarised
in bench/tpcds_chain/RESULTS.md), the mixed change unless a figure says otherwise.

    python docs/blog/_figures/pipeline_engine.py
"""

from __future__ import annotations

from pathlib import Path

from svg import AQUA, ARROW_DEFS, BLUE, GRID, MUTED, NEUTRAL, SURFACE, TEXT, TEXT_2, arrow, bar, legend, node, svg, text

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "docs" / "static" / "img" / "blog" / "duckdb-pipeline-engine"

# One colour per engine, in every figure. DuckDB's green is also "the rebuild" in the incremental figures, where
# each change takes its scenario's colour (validated together on the dark surface; a plain orange sat too close to
# the magenta, so new sales is a more amber one). Spark took blue when the scenarios took orange.
DUCKDB, DUCKSTRING, SPARK, STARROCKS = AQUA, BLUE, BLUE, NEUTRAL
NEW_SALES, FACT_CHANGES, MIXED = "#d27a14", "#d55181", "#9085e9"
SCENARIO_COLOURS = {"append": NEW_SALES, "fact": FACT_CHANGES, "mix": MIXED}


def shade(colour: str, t: float = 0.5) -> str:
    """``colour`` mixed toward the chart surface by ``t``: the darker part of a bar."""
    a = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(SURFACE[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b, strict=True))


def seconds(v: float) -> str:
    """Whole seconds for round values and anything from 20 s (with a thousands separator), else one decimal."""
    if v == int(v) or v >= 20:
        return f"{v:,.0f} s"
    return f"{v + 1e-9:.1f} s"


def fig_pipeline() -> str:
    """The six Ponds, left to right."""
    width, height = 720, 250
    body = [ARROW_DEFS]
    left, mid, right = 105, 345, 600
    body.append(node(left, 75, "sales_feed", 170, 46, sub="store_sales, store_returns"))
    body.append(node(left, 175, "dimensions", 170, 46, sub="five dimensions"))
    body.append(node(mid, 125, "sales_enriched", 170, 46, stroke=BLUE, sub="joined"))
    outs = [(45, "category_month", "by category and month"), (125, "store_quarter", "by store and quarter"),
            (205, "customer_value", "by customer and state")]
    for y, name, sub in outs:
        body.append(node(right, y, name, 190, 46, sub=sub))
        body.append(arrow(mid + 85, 125, right - 97, y))
    body.append(arrow(left + 85, 75, mid - 87, 118))
    body.append(arrow(left + 85, 175, mid - 87, 132))
    body.append(text(left, 245, "ingested", 11, MUTED, "middle"))
    body.append(text(right, 245, "aggregated", 11, MUTED, "middle"))
    return svg(width, height + 10, body, "The pipeline: two Ponds land the data, one joins it, three aggregate it")


def coloured_bars(title, rows, xmax, ticks, width=720, label_w=210, fmt=None) -> str:
    """Single bars with a colour each: rows of (label, value, colour, bold); colour None draws a dashed outline,
    for a reference. ``fmt`` formats values (seconds by default)."""
    fmt = fmt or seconds
    top, bar_h, gap = 14, 22, 14
    plot_w = width - label_w - 70
    bottom = top + len(rows) * (bar_h + gap) - gap
    body = []
    for t in ticks:
        x = label_w + plot_w * t / xmax
        body.append(f'<line x1="{x:.1f}" y1="{top - 6}" x2="{x:.1f}" y2="{bottom + 4}" stroke="{GRID}"/>')
        body.append(text(x, bottom + 20, fmt(t) if t else "0", 11, MUTED, "middle"))
    for i, (label, v, color, bold, *shown) in enumerate(rows):
        y = top + i * (bar_h + gap)
        body.append(text(label_w - 12, y + bar_h / 2 + 4.5, label, 13, TEXT if bold else TEXT_2, "end",
                         600 if bold else 400))
        w = plot_w * v / xmax
        if color is None:  # a reference, not an engine: drawn as an outline
            body.append(f'<rect x="{label_w + 0.75:.1f}" y="{y + 0.75:.1f}" width="{w - 1.5:.1f}" height="{bar_h - 1.5}" '
                        f'rx="4" fill="none" stroke="{MUTED}" stroke-width="1.5" stroke-dasharray="4 3"/>')
        else:
            body.append(bar(label_w, y, w, bar_h, color))
        value = shown[0] if shown else fmt(v)  # a row's optional fifth element replaces its value label
        body.append(text(label_w + w + 6, y + bar_h / 2 + 4.5, value, 13, TEXT if bold else TEXT_2))
    return svg(width, bottom + 32, body, title)


def fig_laptop() -> str:
    """The whole pipeline on the laptop, from the mixed case's snapshot. StarRocks ran in its container in a Colima
    VM with all 10 cores and 24 GB (median of three, results/laptop_starrocks_2026-10-25.json); the others natively."""
    return coloured_bars("The pipeline at 100 GB on a MacBook Air: DuckDB 25 s, StarRocks 62 s, Spark 200 s", [
        ("DuckDB 2.0", 25.24, DUCKDB, True),
        ("StarRocks 4.1", 61.99, STARROCKS, False),
        ("Spark 4.2", 200.01, SPARK, False),
    ], 220, [0, 50, 100, 150, 200])


def fig_sf100() -> str:
    """AWS, 100 GB: one m7gd.8xlarge, or four m7gd.2xlarge with the same cores and memory in total."""
    return coloured_bars("The pipeline at 100 GB on AWS: DuckDB on one machine against clusters of the same size", [
        ("DuckDB, 1 machine", 17.6, DUCKDB, True),
        ("StarRocks, 1 machine", 64.3, STARROCKS, False),
        ("StarRocks, 4 machines", 60.0, STARROCKS, False),
        ("Spark, 1 machine", 111.0, SPARK, False),
        ("Spark, 4 machines", 122.4, SPARK, False),
    ], 140, [0, 20, 40, 60, 80, 100, 120, 140], label_w=200)


def fig_sf1000() -> str:
    """AWS, 1 TB: one r7gd.8xlarge against four r7gd.2xlarge, everyone on the split-file snapshot. Spark's bar is
    solid to an estimate of its time tuned for this size (its 100 GB join scaled tenfold, the generous end of the
    1,000 to 1,250 s range) and shaded on to its measured time at its default settings."""
    width, label_w, top, bar_h, gap = 720, 200, 14, 22, 14
    xmax, ticks = 1800, [0, 300, 600, 900, 1200, 1500, 1800]
    plot_w = width - label_w - 130
    rows = [("DuckDB, 1 machine", 165.9, DUCKDB, True), ("StarRocks, 4 machines", 622.0, STARROCKS, False),
            ("Spark, 4 machines", 1604.7, SPARK, False)]
    spark_tuned = 1000.0
    bottom = top + len(rows) * (bar_h + gap) - gap
    body = [f'<defs><pattern id="untuned" width="6" height="6" patternUnits="userSpaceOnUse" '
            f'patternTransform="rotate(45)"><rect width="6" height="6" fill="{SPARK}" fill-opacity="0.25"/>'
            f'<line x1="0" y1="0" x2="0" y2="6" stroke="{SPARK}" stroke-width="2.5"/></pattern></defs>']
    for t in ticks:
        x = label_w + plot_w * t / xmax
        body.append(f'<line x1="{x:.1f}" y1="{top - 6}" x2="{x:.1f}" y2="{bottom + 4}" stroke="{GRID}"/>')
        body.append(text(x, bottom + 20, seconds(t) if t else "0", 11, MUTED, "middle"))
    for i, (label, v, color, bold) in enumerate(rows):
        y = top + i * (bar_h + gap)
        body.append(text(label_w - 12, y + bar_h / 2 + 4.5, label, 13, TEXT if bold else TEXT_2, "end",
                         600 if bold else 400))
        w = plot_w * v / xmax
        if label.startswith("Spark"):
            wt = plot_w * spark_tuned / xmax
            body.append(f'<rect x="{label_w + wt:.1f}" y="{y}" width="{w - wt:.1f}" height="{bar_h}" rx="4" '
                        f'fill="url(#untuned)" stroke="{SPARK}" stroke-width="1"/>')
            body.append(bar(label_w, y, wt, bar_h, color))
            body.append(text(label_w + wt - 8, y + bar_h / 2 + 4.5, "about 1,000 s tuned (estimate)", 12, TEXT,
                             "end", 600))
            body.append(text(label_w + w + 6, y + bar_h / 2 + 4.5, f"{seconds(v)} as run", 13, TEXT_2))
        else:
            body.append(bar(label_w, y, w, bar_h, color))
            body.append(text(label_w + w + 6, y + bar_h / 2 + 4.5, seconds(v), 13, TEXT if bold else TEXT_2))
    return svg(width, bottom + 32, body, "The pipeline at 1 TB on AWS: DuckDB 166 s, StarRocks 622 s, Spark 1,605 s "
                                         "as run and about 1,000 s if tuned")


def fig_transfer() -> str:
    """Best one-second rate of reading from S3 during each SF100 rebuild (aws/sampler.py). On one machine all its
    network traffic is S3. On a cluster, traffic between its machines leaves one and enters another, so the S3 read
    rate is the cluster's total in minus its total out each second (a lower bound while it also writes to S3). The
    references are AWS's rated bandwidth: a m7gd.8xlarge 15 Gbit/s; four m7gd.2xlarge 3.75 each sustained (15 in
    all) and up to 15 each in bursts."""
    def gbit(v):
        return f"{v:.0f} Gbit/s" if v == int(v) else f"{v:.1f} Gbit/s"
    return coloured_bars("Peak read rate from S3 during the 100 GB rebuild, against each setup's rated bandwidth", [
        ("Rated bandwidth, 1 machine", 15, None, False),
        ("DuckDB, 1 machine", 13.3, DUCKDB, True),
        ("StarRocks, 1 machine", 3.5, STARROCKS, False),
        ("Spark, 1 machine", 8.5, SPARK, False),
        ("Rated bandwidth, 4 machines", 15, None, False, "15 sustained, 60 burst"),
        ("StarRocks, 4 machines", 3.3, STARROCKS, False),
        ("Spark, 4 machines", 9.2, SPARK, False),
    ], 20, [0, 5, 10, 15, 20], label_w=220, fmt=gbit)


def rebuild_vs_change(title, rows, xmax, step, fmt) -> str:
    """For each scenario and scale, a green bar for DuckDB's rebuild over one in the scenario's colour for
    Duckstring's change: rows of (label, rebuild, change, colour)."""
    width, label_w = 720, 170
    top, bar_h, gap, group_gap = 36, 14, 2, 14
    group_h = 2 * bar_h + gap
    plot_w = width - label_w - 70
    bottom = top + len(rows) * (group_h + group_gap) - group_gap
    body = legend(label_w, 24, [("DuckDB, rebuild", DUCKDB), ("New sales", NEW_SALES), ("Fact changes", FACT_CHANGES),
                                ("Mixed", MIXED)])
    for t in range(0, xmax + 1, step):
        x = label_w + plot_w * t / xmax
        body.append(f'<line x1="{x:.1f}" y1="{top - 6}" x2="{x:.1f}" y2="{bottom + 4}" stroke="{GRID}"/>')
        body.append(text(x, bottom + 20, fmt(t) if t else "0", 11, MUTED, "middle"))
    for gi, (cat, rebuild, change, colour) in enumerate(rows):
        gy = top + gi * (group_h + group_gap)
        body.append(text(label_w - 12, gy + group_h / 2 + 4.5, cat, 13, TEXT_2, "end"))
        for si, (v, c) in enumerate([(rebuild, DUCKDB), (change, colour)]):
            y = gy + si * (bar_h + gap)
            w = max(2.0, plot_w * v / xmax)
            body.append(bar(label_w, y, w, bar_h, c))
            body.append(text(label_w + w + 6, y + bar_h - 3, fmt(v), 12, TEXT_2))
    return svg(width, bottom + 34, body, title)


def fig_incremental() -> str:
    """Duckstring absorbing each change against DuckDB rebuilding the pipeline, on the same AWS machine, both scales.
    DuckDB's rebuilds are the runs the other figures use; Duckstring's changes are from the reruns after the
    performance work (20261026T215026Z-sf100_rerun, median of three; 20261026T235158Z-sf1000_rerun, six memory
    slots), whose own DuckDB rebuilds were within 5% of these."""
    rows = [("New sales, 100 GB", 16.9, 4.0, NEW_SALES), ("Fact changes, 100 GB", 17.2, 5.4, FACT_CHANGES),
            ("Mixed, 100 GB", 17.6, 7.7, MIXED), ("New sales, 1 TB", 166.4, 12.9, NEW_SALES),
            ("Fact changes, 1 TB", 166.9, 23.5, FACT_CHANGES), ("Mixed, 1 TB", 166.9, 37.5, MIXED)]
    return rebuild_vs_change("Time to absorb each change against a full rebuild, at 100 GB and 1 TB", rows, 180, 30,
                             seconds)


def fig_memory() -> str:
    """Peak resident memory of the whole process tree (resources.py, every 0.25 s): DuckDB's rebuild against the
    window of Duckstring's change, from the same reruns (20261026T215026Z-sf100_rerun, median of three, four
    memory slots; 20261026T235158Z-sf1000_rerun, six slots)."""
    rows = [("New sales, 100 GB", 12.2, 4.2, NEW_SALES), ("Fact changes, 100 GB", 12.1, 4.9, FACT_CHANGES),
            ("Mixed, 100 GB", 12.1, 5.8, MIXED), ("New sales, 1 TB", 111.7, 14.0, NEW_SALES),
            ("Fact changes, 1 TB", 109.3, 14.7, FACT_CHANGES), ("Mixed, 1 TB", 109.3, 23.5, MIXED)]

    def gb(v: float) -> str:
        return f"{v:.0f} GB" if v >= 20 or v == int(v) else f"{v:.1f} GB"

    return rebuild_vs_change("Peak memory absorbing each change against a full rebuild, at 100 GB and 1 TB", rows, 120,
                             20, gb)


# Each Pond's change and first build in those reruns: (its Ripple's start, its Ripple's end, its output published),
# seconds from the request. The 100 GB runs are each scenario's median change of three.
POND_NAMES = ("sales_feed", "dimensions", "sales_enriched", "category_month", "store_quarter", "customer_value")
GANTT = {
    "100_append_change": [
        (0.01, 0.7, 0.83), (0.26, 1.43, 1.6), (1.63, 2.54, 2.78),
        (2.79, 3.11, 3.18), (2.79, 3.16, 3.23), (2.79, 3.78, 3.96),
    ],
    "100_append_first": [
        (0.89, 8.83, 11.71), (0.62, 2.22, 2.71), (12.28, 20.49, 20.68),
        (21.22, 23.45, 23.71), (21.21, 23.73, 23.91), (21.21, 23.96, 24.26),
    ],
    "1000_append_change": [
        (0.6, 2.36, 2.84), (0.27, 2.14, 2.33), (3.03, 6.57, 7.46),
        (7.7, 10.15, 10.3), (7.71, 10.39, 10.54), (7.7, 12.29, 12.84),
    ],
    "1000_append_first": [
        (0.94, 67.64, 99.64), (0.65, 2.93, 3.21), (100.32, 181.59, 182.51),
        (183.14, 198.99, 199.27), (183.17, 205.75, 206.28), (183.36, 208.14, 209.68),
    ],
    "100_fact_change": [
        (0.04, 0.93, 1.1), (0.27, 1.48, 1.66), (1.68, 3.54, 3.73),
        (3.74, 4.12, 4.18), (3.74, 4.14, 4.21), (3.75, 5.19, 5.34),
    ],
    "100_fact_first": [
        (0.91, 6.49, 6.88), (0.62, 2.33, 2.92), (7.43, 15.6, 15.79),
        (16.34, 18.48, 18.8), (16.34, 18.83, 18.99), (16.34, 19.02, 19.28),
    ],
    "1000_fact_change": [
        (0.6, 2.74, 3.43), (0.27, 1.67, 1.89), (3.62, 13.57, 14.37),
        (14.61, 17.39, 17.6), (14.61, 17.65, 17.76), (14.61, 23.07, 23.48),
    ],
    "1000_fact_first": [
        (0.93, 52.74, 55.17), (0.63, 2.76, 3.04), (55.8, 135.57, 136.37),
        (136.97, 152.6, 152.96), (136.98, 159.54, 160.12), (136.96, 161.73, 163.12),
    ],
    "100_mix_change": [
        (0.01, 1.6, 1.79), (0.26, 1.79, 1.97), (1.99, 5.7, 5.95),
        (5.96, 6.47, 6.54), (5.96, 6.5, 6.59), (5.97, 7.53, 7.69),
    ],
    "100_mix_first": [
        (1.24, 6.69, 7.08), (0.95, 2.71, 3.17), (7.62, 15.82, 16.01),
        (16.56, 18.57, 19.03), (16.57, 18.99, 19.21), (16.56, 19.28, 19.55),
    ],
    "1000_mix_change": [
        (0.6, 2.68, 3.38), (0.27, 2.37, 2.56), (3.58, 24.38, 25.43),
        (25.68, 29.24, 29.49), (25.68, 29.67, 29.91), (25.67, 36.91, 37.49),
    ],
    "1000_mix_first": [
        (0.92, 52.01, 54.4), (0.64, 3.17, 3.45), (54.99, 135.01, 135.88),
        (136.48, 154.43, 154.78), (136.49, 158.86, 159.48), (136.48, 160.9, 162.32),
    ],
}


# DuckDB's mixed rebuild, stage by stage (one script, so back to back): the 100 GB median of three
# (20261024T051820Z-sf100_single) and the 1 TB run (20261024T060655Z-sf1000_shakedown). It has no separate
# landing steps: the join reads the inputs itself.
REBUILD = {"100": [11.25, 2.92, 0.69, 1.17], "1000": [103.82, 25.46, 6.23, 20.39]}


def fig_ponds(rebuild: bool = True) -> str:
    """Each Pond's part of each change as a Gantt chart: scenarios down, scales across, one time axis per scale.
    A bar runs from the Pond's Ripple starting to its output being published (the darker end), labelled with its
    time; DuckDB's rebuild sits underneath on the same axis. How many times faster is fig_speedup's."""
    width, label_w, gap = 720, 122, 18
    panel_w = (width - label_w - 8 - gap) / 2
    plot_w = panel_w - 78  # room for the labels after the longest bar
    scales = ([("100", "100 GB", 18, [0, 5, 10, 15]), ("1000", "1 TB", 180, [0, 50, 100, 150])] if rebuild else
              [("100", "100 GB", 8, [0, 2, 4, 6, 8]), ("1000", "1 TB", 40, [0, 10, 20, 30, 40])])
    scenarios = [("append", "New sales"), ("fact", "Fact changes"), ("mix", "Mixed")]
    row_h, bar_h, head_h, group_gap = 15, 10, 20, 14
    top = 64
    group_h = head_h + len(POND_NAMES) * row_h
    rebuild_h = head_h + len(POND_NAMES) * row_h if rebuild else -group_gap
    bottom = top + len(scenarios) * (group_h + group_gap) + rebuild_h
    items = [("running", TEXT_2), ("publishing", shade(TEXT_2))] + ([("DuckDB, rebuild", DUCKDB)] if rebuild else [])
    body = legend(label_w, 22, items)
    for ci, (_key, name, xmax, ticks) in enumerate(scales):
        x0 = label_w + ci * (panel_w + gap)
        body.append(text(x0, 52, name, 13, TEXT, "start", 600))
        for t in ticks:
            x = x0 + plot_w * t / xmax
            body.append(f'<line x1="{x:.1f}" y1="{top - 4}" x2="{x:.1f}" y2="{bottom + 2}" stroke="{GRID}"/>')
            body.append(text(x, bottom + 18, f"{t} s" if t else "0", 11, MUTED, "middle"))
    for gi, (sc, title) in enumerate(scenarios):
        gy = top + gi * (group_h + group_gap)
        body.append(text(10, gy + 13, title, 13, TEXT, "start", 600))
        for pi, pond in enumerate(POND_NAMES):
            y = gy + head_h + pi * row_h
            body.append(text(label_w - 10, y + bar_h - 1, pond, 11, TEXT_2, "end"))
            for ci, (key, _, xmax, _) in enumerate(scales):
                x0 = label_w + ci * (panel_w + gap)
                a, b, c = GANTT[f"{key}_{sc}_change"][pi]
                px = lambda v, x0=x0, xmax=xmax: x0 + plot_w * v / xmax  # noqa: E731
                colour = SCENARIO_COLOURS[sc]
                body.append(f'<rect x="{px(a):.1f}" y="{y}" width="{max(px(b) - px(a), 1.5):.1f}" height="{bar_h}" '
                            f'rx="2" fill="{colour}"/>')
                if px(c) - px(b) >= 0.5:
                    body.append(f'<rect x="{px(b):.1f}" y="{y}" width="{px(c) - px(b):.1f}" height="{bar_h}" '
                                f'rx="2" fill="{shade(colour)}"/>')
                body.append(text(px(c) + 5, y + bar_h - 1, f"{c - a:.1f} s", 10.5, TEXT_2))
    if rebuild:
        gy = top + len(scenarios) * (group_h + group_gap)
        body.append(text(10, gy + 13, "Rebuild", 13, TEXT, "start", 600))
        for pi, pond in enumerate(POND_NAMES):
            y = gy + head_h + pi * row_h
            body.append(text(label_w - 10, y + bar_h - 1, pond, 11, TEXT_2, "end"))
            for ci, (key, _, xmax, _) in enumerate(scales):
                x0 = label_w + ci * (panel_w + gap)
                if pi < 2:  # no landing step: the join reads the inputs itself
                    body.append(text(x0 + 5, y + bar_h - 1, "0 s (skipped)", 10.5, MUTED))
                    continue
                stage = pi - 2
                a = sum(REBUILD[key][:stage])
                dur = REBUILD[key][stage]
                px = lambda v, x0=x0, xmax=xmax: x0 + plot_w * v / xmax  # noqa: E731
                body.append(f'<rect x="{px(a):.1f}" y="{y}" width="{max(px(a + dur) - px(a), 1.5):.1f}" '
                            f'height="{bar_h}" rx="2" fill="{DUCKDB}"/>')
                body.append(text(px(a + dur) + 5, y + bar_h - 1, f"{dur:.1f} s" if dur < 20 else f"{dur:.0f} s",
                                 10.5, TEXT_2))
    return svg(width, bottom + 30, body, "Each Pond's part of each change at 100 GB and 1 TB, above DuckDB's rebuild")


def fig_speedup() -> str:
    """How many times faster each step absorbed each change than DuckDB's rebuild ran the same stage: a bar per
    scenario, solid to its 100 GB multiple and a lighter extension to its 1 TB one. The landing steps are left out
    (the rebuild has none)."""
    width, label_w = 720, 132
    plot_w = width - label_w - 96
    xmax, ticks = 25, [0, 5, 10, 15, 20, 25]
    bar_h, gap, group_gap, top = 12, 2, 16, 58
    scenarios = [("append", "New sales"), ("fact", "Fact changes"), ("mix", "Mixed")]
    group_h = len(scenarios) * (bar_h + gap) - gap
    ponds = POND_NAMES[2:]
    bottom = top + len(ponds) * (group_h + group_gap) - group_gap
    body = legend(label_w, 22, [(n, SCENARIO_COLOURS[k]) for k, n in scenarios])
    kx = 470  # after the scenarios: solid is the 100 GB multiple, the lighter extension the rest of the 1 TB one
    body.append(f'<rect x="{kx}" y="12" width="12" height="12" rx="3" fill="{MUTED}"/>')
    body.append(text(kx + 18, 22, "100 GB", 13, TEXT_2))
    body.append(f'<rect x="{kx + 84}" y="12" width="12" height="12" rx="3" fill="{MUTED}" fill-opacity="0.4"/>')
    body.append(text(kx + 102, 22, "1 TB", 13, TEXT_2))
    px = lambda v: label_w + plot_w * v / xmax  # noqa: E731
    for t in ticks:
        body.append(f'<line x1="{px(t):.1f}" y1="{top - 6}" x2="{px(t):.1f}" y2="{bottom + 4}" stroke="{GRID}"/>')
        body.append(text(px(t), bottom + 20, f"{t}×", 11, MUTED, "middle"))
    body.append(f'<line x1="{px(1):.1f}" y1="{top - 6}" x2="{px(1):.1f}" y2="{bottom + 4}" stroke="{TEXT_2}" '
                f'stroke-dasharray="3 3"/>')
    body.append(text(px(1) + 4, top - 10, "1×: as fast as the rebuild", 11, MUTED))
    fmt = lambda v: f"{v:.1f}×" if v < 10 else f"{v:.0f}×"  # noqa: E731
    for gi, pond in enumerate(ponds):
        gy = top + gi * (group_h + group_gap)
        body.append(text(label_w - 12, gy + group_h / 2 + 4.5, pond, 13, TEXT_2, "end"))
        for si, (sc, _) in enumerate(scenarios):
            y = gy + si * (bar_h + gap)
            multiple = []
            for key in ("100", "1000"):
                a, _, c = GANTT[f"{key}_{sc}_change"][gi + 2]
                multiple.append(REBUILD[key][gi] / (c - a))
            m100, m1000 = multiple
            colour = SCENARIO_COLOURS[sc]
            body.append(f'<rect x="{label_w}" y="{y}" width="{px(m100) - label_w:.1f}" height="{bar_h}" '
                        f'fill="{colour}"/>')
            body.append(bar(px(m100) + 1, y, px(m1000) - px(m100) - 1, bar_h, colour).replace(
                "/>", ' fill-opacity="0.4"/>'))
            body.append(text(px(m1000) + 6, y + bar_h - 2, f"{fmt(m100)} to {fmt(m1000)}", 11, TEXT_2))
    return svg(width, bottom + 32, body, "How many times faster each step absorbed each change than the rebuild ran "
               "it, at 100 GB and 1 TB")


# Each Pond's peak resident memory (resources.py, every 0.25 s) over the window of the mixed first build and of each
# change, in GB, from the same reruns (100 GB: median of three; 1 TB: one run).
STEP_MEMORY = {
    "100": {"first": [6.7, 0.9, 4.7, 1.0, 1.0, 3.9], "append": [1.1, 0.3, 1.6, 0.6, 0.6, 1.6],
            "fact": [1.1, 0.3, 2.8, 0.8, 0.9, 1.9], "mix": [1.2, 1.1, 3.7, 1.1, 1.1, 2.3]},
    "1000": {"first": [51.0, 1.9, 21.6, 1.4, 3.1, 31.8], "append": [2.4, 0.8, 5.1, 3.4, 3.4, 8.6],
             "fact": [2.3, 0.9, 12.2, 3.6, 3.7, 9.1], "mix": [2.3, 4.6, 22.1, 5.5, 5.6, 13.7]},
}


def fig_memory_steps() -> str:
    """Each step's peak memory in the (full) first build and in each change, at both scales, one axis per scale."""
    width, label_w, gap = 720, 122, 18
    panel_w = (width - label_w - 8 - gap) / 2
    plot_w = panel_w - 54
    scales = [("100", "100 GB", 8, [0, 2, 4, 6, 8]), ("1000", "1 TB", 60, [0, 20, 40, 60])]
    series = [("first", DUCKDB), ("append", NEW_SALES), ("fact", FACT_CHANGES), ("mix", MIXED)]
    bar_h, bar_gap, group_gap, top = 8, 2, 12, 64
    group_h = len(series) * (bar_h + bar_gap) - bar_gap
    bottom = top + len(POND_NAMES) * (group_h + group_gap) - group_gap
    body = legend(label_w, 22, [("Full build", DUCKDB), ("New sales", NEW_SALES), ("Fact changes", FACT_CHANGES),
                                ("Mixed", MIXED)])

    def gb(v: float) -> str:
        return f"{v:.0f} GB" if v >= 10 else f"{v:.1f} GB"

    for ci, (key, name, xmax, ticks) in enumerate(scales):
        x0 = label_w + ci * (panel_w + gap)
        body.append(text(x0, 52, name, 13, TEXT, "start", 600))
        for t in ticks:
            x = x0 + plot_w * t / xmax
            body.append(f'<line x1="{x:.1f}" y1="{top - 4}" x2="{x:.1f}" y2="{bottom + 2}" stroke="{GRID}"/>')
            body.append(text(x, bottom + 18, f"{t} GB" if t else "0", 11, MUTED, "middle"))
        for pi, pond in enumerate(POND_NAMES):
            gy = top + pi * (group_h + group_gap)
            if ci == 0:
                body.append(text(label_w - 10, gy + group_h / 2 + 4, pond, 11, TEXT_2, "end"))
            for si, (sc, colour) in enumerate(series):
                v = STEP_MEMORY[key][sc][pi]
                y = gy + si * (bar_h + bar_gap)
                w = max(1.5, plot_w * v / xmax)
                body.append(bar(x0, y, w, bar_h, colour))
                body.append(text(x0 + w + 4, y + bar_h - 0.5, gb(v), 9.5, TEXT_2))
    return svg(width, bottom + 30, body, "Each step's peak memory in a full build and in each change, at 100 GB and 1 TB")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fig in [("pipeline", fig_pipeline), ("laptop", fig_laptop), ("sf100", fig_sf100), ("sf1000", fig_sf1000),
                      ("transfer", fig_transfer), ("incremental", fig_incremental), ("ponds", fig_ponds),
                      ("speedup", fig_speedup), ("memory", fig_memory), ("memory-steps", fig_memory_steps)]:
        (OUT / f"{name}.svg").write_text(fig())
        print(OUT / f"{name}.svg")


if __name__ == "__main__":
    main()
