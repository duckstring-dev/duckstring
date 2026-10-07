"""SVG figures for the clustering post, drawn from the benchmark's saved results in bench/clustering/results:

    uv run python docs/blog/_figures/clustering.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from svg import AQUA, BLUE, GRID, MUTED, NEUTRAL, ORANGE, SURFACE, TEXT, TEXT_2, hbars, pct, svg, text

ROOT = Path(__file__).resolve().parents[3]
HERE = ROOT / "bench" / "clustering"
OUT = ROOT / "docs" / "static" / "img" / "blog" / "clustering"

# ---------------------------------------------------------------------------------------------- data


def families(run: str) -> dict:
    """``{(set, layout, family): mean rows-read fraction}`` and the same for median ms."""
    rows = [json.loads(line) for line in (HERE / "results" / run / "queries.jsonl").read_text().splitlines()]
    acc: dict = {}
    for r in rows:
        k = (r["set"], r["layout"], r["family"])
        a = acc.setdefault(k, [0, 0.0, 0.0])
        a[0] += 1
        a[1] += r["rg_fraction"]
        a[2] += r["median_ms"]
    return {k: {"read": a[1] / a[0], "ms": a[2] / a[0]} for k, a in acc.items()}


def encodes(run: str) -> dict:
    out = {}
    for line in (HERE / "results" / run / "encode.jsonl").read_text().splitlines():
        e = json.loads(line)
        out[(e["set"], e["layout"])] = e
    return out


# ---------------------------------------------------------------------------------------------- curves


def z_index(x: int, y: int, bits: int) -> int:
    d = 0
    for i in range(bits - 1, -1, -1):
        d = (d << 2) | (((x >> i) & 1) << 1) | ((y >> i) & 1)
    return d


def hilbert_index(n: int, x: int, y: int) -> int:
    d, s = 0, n // 2
    while s > 0:
        rx = 1 if x & s else 0
        ry = 1 if y & s else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x, y = n - 1 - x, n - 1 - y
            x, y = y, x
        s //= 2
    return d


def fig_curves() -> str:
    n, cell, pad = 8, 30, 20
    panel = n * cell
    width = 2 * panel + 3 * pad + 60
    height = panel + 110
    body = []
    for pi, (name, key) in enumerate([("Z-order (Morton)", lambda x, y: z_index(x, y, 3)),
                                      ("Hilbert", lambda x, y: hilbert_index(n, x, y))]):
        ox = 40 + pi * (panel + pad + 20)
        oy = 52
        body.append(text(ox + panel / 2, oy - 6, name, 13, TEXT, "middle", 600))
        for i in range(n + 1):
            body.append(f'<line x1="{ox}" y1="{oy + i * cell}" x2="{ox + panel}" y2="{oy + i * cell}" stroke="{GRID}"/>')
            body.append(f'<line x1="{ox + i * cell}" y1="{oy}" x2="{ox + i * cell}" y2="{oy + panel}" stroke="{GRID}"/>')
        order = sorted(((key(x, y), x, y) for x in range(n) for y in range(n)))
        pts = [(ox + (x + 0.5) * cell, oy + panel - (y + 0.5) * cell) for _, x, y in order]
        color = BLUE if pi == 1 else ORANGE
        body.append(f'<polyline points="{" ".join(f"{px:.1f},{py:.1f}" for px, py in pts)}" fill="none" '
                    f'stroke="{color}" stroke-width="2" stroke-linejoin="round"/>')
        body.append(f'<circle cx="{pts[0][0]:.1f}" cy="{pts[0][1]:.1f}" r="4" fill="{color}" stroke="{SURFACE}" '
                    f'stroke-width="2"/>')
        body.append(text(ox + panel / 2, oy + panel + 22, "first column →", 11, MUTED, "middle"))
        body.append(f'<text x="{ox - 10}" y="{oy + panel / 2}" font-size="11" fill="{MUTED}" text-anchor="middle" '
                    f'transform="rotate(-90 {ox - 10} {oy + panel / 2})">second column →</text>')
    return svg(width, height, body, "Z-order and Hilbert curves on an 8 by 8 grid", crop=(24, 20))


def fig_row_groups() -> str:
    """Toy table: every cell of a 32x32 grid once, written in four orders, in row groups of 48 rows (not a
    power of four, so they don't line up with the curves' quadrants, as real row groups don't). Each row
    group's min/max box is drawn; a filter on the second column reads the boxes it crosses."""
    n, per_group = 32, 48
    pts = [(x, y) for x in range(n) for y in range(n)]
    shuffled = pts[:]
    random.Random(7).shuffle(shuffled)
    orders = [
        ("Random", shuffled),
        ("Sorted by first, second", sorted(pts)),
        ("Z-order", sorted(pts, key=lambda p: z_index(p[0], p[1], 5))),
        ("Hilbert", sorted(pts, key=lambda p: hilbert_index(n, p[0], p[1]))),
    ]
    lo, hi = 13, 15  # the filter: second column between 13 and 15
    size, pad = 150, 26
    width = 4 * size + 3 * pad + 60
    height = size + 146
    body = []
    for pi, (name, order) in enumerate(orders):
        ox, oy = 30 + pi * (size + pad), 66
        s = size / n
        body.append(text(ox + size / 2, oy - 12, name, 13, TEXT, "middle", 600))
        body.append(f'<rect x="{ox}" y="{oy}" width="{size}" height="{size}" fill="none" stroke="{GRID}"/>')
        fy0, fy1 = oy + size - (hi + 1) * s, oy + size - lo * s
        body.append(f'<rect x="{ox}" y="{fy0:.1f}" width="{size}" height="{fy1 - fy0:.1f}" fill="{ORANGE}" '
                    f'fill-opacity="0.22"/>')
        read, groups = 0, -(-len(order) // per_group)
        for g in range(groups):
            rows = order[g * per_group:(g + 1) * per_group]
            x0, x1 = min(p[0] for p in rows), max(p[0] for p in rows)
            y0, y1 = min(p[1] for p in rows), max(p[1] for p in rows)
            hit = y1 >= lo and y0 <= hi
            read += hit
            body.append(f'<rect x="{ox + x0 * s + 1:.1f}" y="{oy + size - (y1 + 1) * s + 1:.1f}" '
                        f'width="{(x1 - x0 + 1) * s - 2:.1f}" height="{(y1 - y0 + 1) * s - 2:.1f}" rx="2" '
                        f'fill="{BLUE if hit else "none"}" fill-opacity="{(0.05 if pi == 0 else 0.22) if hit else 0}" '
                        f'stroke="{BLUE if hit else MUTED}" stroke-width="{1.5 if hit else 1}" '
                        f'stroke-opacity="{1 if hit else 0.7}"/>')
        body.append(text(ox + size / 2, oy + size + 24, f"reads {read} of {groups} row groups", 13,
                         TEXT if read < groups else TEXT_2, "middle", 600 if read < groups else 400))
    return svg(width, height, body, "Row group bounding boxes under random, sorted, Z-order and Hilbert orders", crop=(34, 40))


def fig_buckets() -> str:
    b = json.loads((HERE / "results" / "sf10" / "buckets.json").read_text())
    total = b["rows"]
    width, plot_h, pad = 720, 150, 40
    panel_w = (width - 3 * pad - 30) / 2
    height = plot_h + 140
    body = []
    ymax = 0.6
    for pi, (name, key, color) in enumerate([("Scaled between min and max", "scaled", ORANGE),
                                             ("Ranked (equal population)", "ranked", BLUE)]):
        ox, oy = 50 + pi * (panel_w + pad), 60
        body.append(text(ox, oy - 8, name, 13, TEXT, weight=600))
        for t in (0, 0.2, 0.4, 0.6):
            y = oy + plot_h - plot_h * t / ymax
            body.append(f'<line x1="{ox}" y1="{y:.1f}" x2="{ox + panel_w:.1f}" y2="{y:.1f}" stroke="{GRID}"/>')
            if pi == 0:
                body.append(text(ox - 8, y + 4, f"{t:.0%}", 11, MUTED, "end"))
        bw = panel_w / 16
        for i, c in enumerate(b[key]):
            v = c / total
            h = max(1.5, plot_h * v / ymax)
            x, y = ox + i * bw + 1, oy + plot_h - h
            r = min(4, h / 2, (bw - 2) / 2)
            body.append(f'<path d="M{x:.1f},{oy + plot_h:.1f} v-{h - r:.1f} a{r},{r} 0 0 1 {r},-{r} h{bw - 2 - 2 * r:.1f} '
                        f'a{r},{r} 0 0 1 {r},{r} v{h - r:.1f} z" fill="{color}"/>')
        first = b[key][0] / total
        body.append(text(ox + bw + 6, oy + plot_h - plot_h * first / ymax + 12, f"{first:.0%} of rows" if pi == 0
                         else "", 12, TEXT_2))
        if pi == 1:
            body.append(text(ox + panel_w / 2, oy + plot_h - plot_h * (1 / 16) / ymax - 8, "6.25% each", 12, TEXT_2,
                             "middle"))
        body.append(text(ox + panel_w / 2, oy + plot_h + 20, "bucket, low to high values", 11, MUTED, "middle"))
    return svg(width, height, body, "Bucket populations for a skewed column: scaled against ranked", crop=(32, 44))


LAYOUT_NAMES = [("hash", "Random"), ("lexicographic", "Sorted (lexicographic)"), ("morton", "Morton"),
                ("hilbert", "Hilbert"), ("arank_morton", "Rank-Morton"), ("arank_hilbert", "Rank-Hilbert")]


def fig_pruning_skewed(q) -> str:
    cats = [n for _, n in LAYOUT_NAMES]
    series = [("1% range on ss_net_paid", BLUE,
               [q[("skewed", lay, "range ss_net_paid 1.0%")]["read"] for lay, _ in LAYOUT_NAMES]),
              ("1% range on ss_sold_date_sk", ORANGE,
               [q[("skewed", lay, "range ss_sold_date_sk 1.0%")]["read"] for lay, _ in LAYOUT_NAMES])]
    return hbars("Share of rows read, clustered on (date, net paid)", cats, series, 1.0, pct,
                 [0, 0.25, 0.5, 0.75, 1.0],
                 highlight="Rank-Hilbert")


def fig_pruning_keys(q) -> str:
    cats = [n for _, n in LAYOUT_NAMES]
    fams = [("1% range on date", BLUE, "range ss_sold_date_sk 1.0%"),
            ("1% range on item", ORANGE, "range ss_item_sk 1.0%"),
            ("1% range on customer", AQUA, "range ss_customer_sk 1.0%")]
    series = [(n, c, [q[("keys", lay, f)]["read"] for lay, _ in LAYOUT_NAMES]) for n, c, f in fams]
    return hbars("Share of rows read, clustered on (date, item, customer)", cats, series, 1.0, pct,
                 [0, 0.25, 0.5, 0.75, 1.0], highlight="Rank-Hilbert")


def fig_query_time(q) -> str:
    cats = [n for _, n in LAYOUT_NAMES]
    series = [("1% range on ss_net_paid", NEUTRAL,
               [q[("skewed", lay, "range ss_net_paid 1.0%")]["ms"] for lay, _ in LAYOUT_NAMES])]
    return hbars("Query time for a 1% range on ss_net_paid (ms)", cats, series, 280, lambda v: f"{v:.0f} ms",
                 [0, 70, 140, 210, 280],
                 highlight="Rank-Hilbert")


def fig_write_cost(e) -> str:
    rows = [("No sort", ("skewed", "generated")), ("Sorted (lexicographic)", ("skewed", "lexicographic")),
            ("Morton", ("skewed", "morton")), ("Hilbert", ("skewed", "hilbert")),
            ("Rank-Morton", ("skewed", "arank_morton")), ("Rank-Hilbert", ("skewed", "arank_hilbert"))]
    vals = [e[k]["stats_s"] + e[k]["write_s"] for _, k in rows]
    return hbars("Time to write the clustered table (s)", [n for n, _ in rows], [("seconds", NEUTRAL, vals)], 240,
                 lambda v: f"{v:.0f} s", [0, 60, 120, 180, 240], label_w=190,
                 highlight="Rank-Hilbert")


def fig_file_level() -> str:
    r = json.loads((HERE / "results" / "sf100" / "filelevel.json").read_text())
    fams = [("1% range on ss_net_paid", "range ss_net_paid 1.0%"), ("1% range on date", "range ss_sold_date_sk 1.0%"),
            ("0.1% box on both", "box all 0.10%"), ("November join", "join_month")]
    series = [("Sorted within files", BLUE, [r["256MB"]["families"][f]["sorted_rg"] for _, f in fams]),
              ("Files by key range, 256 MB", ORANGE, [r["256MB"]["families"][f]["part_rg"] for _, f in fams]),
              ("Files by key range, 1 GB", AQUA, [r["1GB"]["families"][f]["part_rg"] for _, f in fams])]
    return hbars("Share of rows read: sorting inside files, or only between them", [n for n, _ in fams], series, 0.5,
                 pct, [0, 0.1, 0.2, 0.3, 0.4, 0.5], label_w=180)


RAMP = ["#9ec5f4", "#5598e7", "#2a78d6", "#1c5cab"]  # one hue, light to dark, validated as an ordinal ramp


def fig_bound() -> str:
    """The lower bound on rows read for a 1% range on one clustered column, s + N^(-1/k), against the number of
    row groups N, for k clustered columns. Theory only: the measured results are charted in their own sections."""
    import math

    s = 0.01
    width, height = 720, 380
    x0, y0, pw, ph = 70, 30, 520, 290
    nmin, nmax, ymin, ymax = 10, 1e6, 0.005, 1.0

    def px(n):
        return x0 + pw * (math.log10(n) - math.log10(nmin)) / (math.log10(nmax) - math.log10(nmin))

    def py(v):
        return y0 + ph - ph * (math.log10(v) - math.log10(ymin)) / (math.log10(ymax) - math.log10(ymin))

    body = []
    for v, label in [(0.01, "1%"), (0.02, "2%"), (0.05, "5%"), (0.1, "10%"), (0.2, "20%"), (0.5, "50%"), (1.0, "100%")]:
        body.append(f'<line x1="{x0}" y1="{py(v):.1f}" x2="{x0 + pw}" y2="{py(v):.1f}" stroke="{GRID}"/>')
        body.append(text(x0 - 8, py(v) + 4, label, 11, MUTED, "end"))
    for n, label in [(10, "10"), (100, "100"), (1e3, "1k"), (1e4, "10k"), (1e5, "100k"), (1e6, "1M")]:
        body.append(f'<line x1="{px(n):.1f}" y1="{y0}" x2="{px(n):.1f}" y2="{y0 + ph}" stroke="{GRID}"/>')
        body.append(text(px(n), y0 + ph + 18, label, 11, MUTED, "middle"))
    body.append(text(x0 + pw / 2, y0 + ph + 38, "row groups (N)", 12, TEXT_2, "middle"))
    # The two scales tested: store_sales at SF10 and SF100, in 122,880-row row groups.
    for rows, label in ((28_800_991, "SF10"), (287_997_024, "SF100")):
        n = rows / 122_880
        body.append(f'<line x1="{px(n):.1f}" y1="{y0}" x2="{px(n):.1f}" y2="{y0 + ph}" stroke="{MUTED}" '
                    f'stroke-width="1" stroke-dasharray="2 3"/>')
        body.append(text(px(n) + 4, y0 + 12, label, 11, MUTED))
    # The floor: only the matching rows.
    body.append(f'<line x1="{x0}" y1="{py(s):.1f}" x2="{x0 + pw}" y2="{py(s):.1f}" stroke="{TEXT_2}" '
                f'stroke-width="1.5" stroke-dasharray="6 4"/>')
    for k in range(1, 5):
        pts = []
        for i in range(0, 121):
            n = 10 ** (math.log10(nmin) + i / 120 * (math.log10(nmax) - math.log10(nmin)))
            pts.append(f"{px(n):.1f},{py(min(ymax, s + n ** (-1 / k))):.1f}")
        body.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{RAMP[k - 1]}" stroke-width="2"/>')
        # Labels where the curves are well apart: 1 and 2 columns on the curve itself, 3 and 4 at the right end.
        label = "1 column" if k == 1 else f"{k} columns"
        at = {1: 25, 2: 3e4, 3: nmax, 4: nmax}[k]
        y = py(s + at ** (-1 / k))
        if k <= 2:
            body.append(text(px(at) + 6, y - 8, label, 12, RAMP[k - 1]))
        else:
            body.append(text(x0 + pw + 8, y + 4, label, 12, RAMP[k - 1]))
    return svg(width, height, body, "The lowest possible share of rows read for a 1% range, against the number of "
               "row groups, for one to four clustered columns")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    q = families("sf100")
    e = encodes("sf100")
    figs = {"curves.svg": fig_curves(), "row-groups.svg": fig_row_groups(), "buckets.svg": fig_buckets(),
            "pruning-skewed.svg": fig_pruning_skewed(q), "pruning-keys.svg": fig_pruning_keys(q),
            "query-time.svg": fig_query_time(q), "write-cost.svg": fig_write_cost(e),
            "file-level.svg": fig_file_level(), "bound.svg": fig_bound()}
    for name, content in figs.items():
        (OUT / name).write_text(content)
        print(OUT / name)


if __name__ == "__main__":
    main()
