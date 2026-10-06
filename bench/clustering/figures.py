"""SVG figures for the clustering blog post, drawn from the saved results (no plotting dependency):

    uv run python bench/clustering/figures.py [OUT_DIR]

OUT_DIR defaults to docs/static/img/blog/clustering. The figures use the dark chart palette, since the site is
dark only: categorical blue, orange and aqua in that fixed order (validated for colour-vision deficiency on the
dark surface), neutral grey for reference bars.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parents[1] / "docs" / "static" / "img" / "blog" / "clustering"

SURFACE = "#1a1a19"
TEXT = "#ffffff"
TEXT_2 = "#c3c2b7"
MUTED = "#8a897f"
GRID = "#383835"
BLUE, ORANGE, AQUA = "#3987e5", "#d95926", "#199e70"
NEUTRAL = "#6f6e66"
FONT = "Inter, system-ui, -apple-system, 'Segoe UI', sans-serif"


def svg(width: int, height: int, body: list[str], title: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
            f'height="{height}" role="img" aria-label="{title}" font-family="{FONT}">\n'
            f'<title>{title}</title>\n'
            f'<rect width="{width}" height="{height}" rx="8" fill="{SURFACE}"/>\n' + "\n".join(body) + "\n</svg>\n")


def text(x, y, s, size=13, fill=TEXT_2, anchor="start", weight=400) -> str:
    s = s.replace("&", "&amp;").replace("<", "&lt;")
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" '
            f'font-weight="{weight}">{s}</text>')


def bar(x, y, w, h, fill, rounded="end") -> str:
    """A horizontal bar anchored at ``x``; the data end (right) gets a 4px radius."""
    if w <= 0:
        return ""
    r = min(4, w / 2, h / 2)
    return (f'<path d="M{x:.1f},{y:.1f} h{w - r:.1f} a{r},{r} 0 0 1 {r},{r} v{h - 2 * r:.1f} '
            f'a{r},{r} 0 0 1 -{r},{r} h-{w - r:.1f} z" fill="{fill}"/>')


def legend(x, y, items) -> list[str]:
    out = []
    for name, color in items:
        out.append(f'<rect x="{x}" y="{y - 10}" width="12" height="12" rx="3" fill="{color}"/>')
        out.append(text(x + 18, y, name, 13, TEXT_2))
        x += 26 + 7.2 * len(name)
    return out


def hbars(title: str, categories: list[str], series: list[tuple[str, str, list]], xmax: float, fmt, ticks,
          width=720, label_w=150, note=None, highlight=None) -> str:
    """Grouped horizontal bars: one group per category, one bar per series, values labelled at the bar end.
    A ``None`` value draws a dashed "did not finish" outline instead of a bar."""
    top = 56 if len(series) > 1 else 40
    bar_h, gap, group_gap = 14, 2, 14
    group_h = len(series) * (bar_h + gap) - gap
    plot_w = width - label_w - 70
    height = top + len(categories) * (group_h + group_gap) + 34 + (22 if note else 0)
    body = [text(20, 26, title, 15, TEXT, weight=600)]
    if len(series) > 1:
        body += legend(label_w, 48, [(n, c) for n, c, _ in series])
    x0 = label_w
    bottom = top + len(categories) * (group_h + group_gap) - group_gap
    for t in ticks:
        x = x0 + plot_w * t / xmax
        body.append(f'<line x1="{x:.1f}" y1="{top - 6}" x2="{x:.1f}" y2="{bottom + 4}" stroke="{GRID}" stroke-width="1"/>')
        body.append(text(x, bottom + 20, fmt(t), 11, MUTED, "middle"))
    for gi, cat in enumerate(categories):
        gy = top + gi * (group_h + group_gap)
        weight = 600 if highlight and cat == highlight else 400
        body.append(text(x0 - 12, gy + group_h / 2 + 4.5, cat, 13, TEXT if weight == 600 else TEXT_2, "end", weight))
        for si, (_, color, values) in enumerate(series):
            v = values[gi]
            y = gy + si * (bar_h + gap)
            if v is None:
                w = plot_w
                body.append(f'<rect x="{x0}" y="{y}" width="{w}" height="{bar_h}" rx="4" fill="none" '
                            f'stroke="{MUTED}" stroke-width="1.5" stroke-dasharray="4 3"/>')
                body.append(text(x0 + 10, y + bar_h - 3, "did not finish: 60 GiB of spill", 12, TEXT_2))
                continue
            w = max(2.0, plot_w * min(v, xmax) / xmax)
            if len(series) == 1 and highlight and cat == highlight:
                color = BLUE
            body.append(bar(x0, y, w, bar_h, color))
            body.append(text(x0 + w + 6, y + bar_h - 3, fmt(v), 12, TEXT_2))
    if note:
        body.append(text(20, height - 14, note, 12, MUTED))
    return svg(width, height, body, title)


def pct(v: float) -> str:
    return f"{v:.0%}" if v >= 0.095 or v == 0 else f"{v:.1%}"


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
    body = [text(20, 26, "Two ways to walk a grid", 15, TEXT, weight=600)]
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
    body.append(text(20, height - 12, "Both visit every cell once. Z-order jumps at the edge of each quadrant; Hilbert only "
                     "ever steps to a neighbour.", 12, MUTED))
    return svg(width, height, body, "Z-order and Hilbert curves on an 8 by 8 grid")


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
    body = [text(20, 26, "Row-group boxes in four orders, and a filter on the second column", 15, TEXT, weight=600)]
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
    body.append(text(20, height - 30, "1,024 rows (every cell of a 32 by 32 grid), 48 rows per row group. Shaded band: "
                     "the filter. Blue: row groups whose", 12, MUTED))
    body.append(text(20, height - 12, "min/max can't rule it out. In random order every box spans the whole grid.", 12,
                     MUTED))
    return svg(width, height, body, "Row-group bounding boxes under random, sorted, Z-order and Hilbert orders")


def fig_buckets() -> str:
    b = json.loads((HERE / "results" / "sf10" / "buckets.json").read_text())
    total = b["rows"]
    width, plot_h, pad = 720, 150, 40
    panel_w = (width - 3 * pad - 30) / 2
    height = plot_h + 140
    body = [text(20, 26, "Rows per bucket for ss_net_paid, 16 buckets", 15, TEXT, weight=600)]
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
    body.append(text(20, height - 30, f"TPC-DS SF10 store_sales, {total / 1e6:.1f}M non-NULL rows. The median is 865, "
                     f"the maximum {b['max']:,.0f}: scaling puts most rows in the first", 12, MUTED))
    body.append(text(20, height - 12, "few buckets, so the curve has almost nothing to order them by.", 12, MUTED))
    return svg(width, height, body, "Bucket populations for a skewed column: scaled against ranked")


LAYOUT_NAMES = [("hash", "Random"), ("lexicographic", "Sorted (lexicographic)"), ("morton", "Morton"),
                ("hilbert", "Hilbert"), ("arank_morton", "Rank-Morton"), ("arank_hilbert", "Rank-Hilbert")]


def fig_pruning_skewed(q) -> str:
    cats = [n for _, n in LAYOUT_NAMES]
    series = [("1% range on ss_net_paid", BLUE,
               [q[("skewed", lay, "range ss_net_paid 1.0%")]["read"] for lay, _ in LAYOUT_NAMES]),
              ("1% range on ss_sold_date_sk", ORANGE,
               [q[("skewed", lay, "range ss_sold_date_sk 1.0%")]["read"] for lay, _ in LAYOUT_NAMES])]
    return hbars("Share of rows read, clustered on (date, net paid)", cats, series, 1.0, pct,
                 [0, 0.25, 0.5, 0.75, 1.0], note="TPC-DS SF100, 288M rows, 2,344 row groups. Lower is better.",
                 highlight="Rank-Hilbert")


def fig_pruning_keys(q) -> str:
    cats = [n for _, n in LAYOUT_NAMES]
    fams = [("1% range on date", BLUE, "range ss_sold_date_sk 1.0%"),
            ("1% range on item", ORANGE, "range ss_item_sk 1.0%"),
            ("1% range on customer", AQUA, "range ss_customer_sk 1.0%")]
    series = [(n, c, [q[("keys", lay, f)]["read"] for lay, _ in LAYOUT_NAMES]) for n, c, f in fams]
    return hbars("Share of rows read, clustered on (date, item, customer)", cats, series, 1.0, pct,
                 [0, 0.25, 0.5, 0.75, 1.0], note="TPC-DS SF100. Lower is better.", highlight="Rank-Hilbert")


def fig_query_time(q) -> str:
    cats = [n for _, n in LAYOUT_NAMES]
    series = [("1% range on ss_net_paid", NEUTRAL,
               [q[("skewed", lay, "range ss_net_paid 1.0%")]["ms"] for lay, _ in LAYOUT_NAMES])]
    return hbars("Query time for a 1% range on ss_net_paid (ms)", cats, series, 280, lambda v: f"{v:.0f} ms",
                 [0, 70, 140, 210, 280], note="TPC-DS SF100, median of 5 warm runs. A count and a sum over the matching rows.",
                 highlight="Rank-Hilbert")


def fig_write_cost(e) -> str:
    rows = [("No sort", ("skewed", "generated")), ("Sorted (lexicographic)", ("skewed", "lexicographic")),
            ("Morton", ("skewed", "morton")), ("Hilbert", ("skewed", "hilbert")),
            ("Rank-Morton, window ranks", ("skewed", "rank_morton")),
            ("Rank-Hilbert, exact ranks", ("skewed", "qrank_hilbert")),
            ("Rank-Hilbert", ("skewed", "arank_hilbert"))]
    vals = []
    for _, k in rows:
        r = e.get(k)
        vals.append(None if r is None or r.get("error") else r["stats_s"] + r["write_s"])
    return hbars("Time to write the clustered table (s)", [n for n, _ in rows], [("seconds", NEUTRAL, vals)], 320,
                 lambda v: f"{v:.0f} s", [0, 80, 160, 240, 320], label_w=210,
                 note="TPC-DS SF100, clustered on (date, net paid), 7 bits per column. One run each; runs varied by ~20%.",
                 highlight="Rank-Hilbert")


def fig_file_level() -> str:
    r = json.loads((HERE / "results" / "sf100" / "filelevel.json").read_text())
    fams = [("1% range on ss_net_paid", "range ss_net_paid 1.0%"), ("1% range on date", "range ss_sold_date_sk 1.0%"),
            ("0.1% box on both", "box all 0.10%"), ("November join", "join_month")]
    series = [("Sorted within files", BLUE, [r["256MB"]["families"][f]["sorted_rg"] for _, f in fams]),
              ("Files by key range, 256 MB", ORANGE, [r["256MB"]["families"][f]["part_rg"] for _, f in fams]),
              ("Files by key range, 1 GB", AQUA, [r["1GB"]["families"][f]["part_rg"] for _, f in fams])]
    return hbars("Share of rows read: sorting inside files, or only between them", [n for n, _ in fams], series, 0.5,
                 pct, [0, 0.1, 0.2, 0.3, 0.4, 0.5], label_w=180,
                 note="TPC-DS SF100, rank-Hilbert key on (date, net paid). Lower is better.")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    q = families("sf100")
    e = encodes("sf100")
    figs = {"curves.svg": fig_curves(), "row-groups.svg": fig_row_groups(), "buckets.svg": fig_buckets(),
            "pruning-skewed.svg": fig_pruning_skewed(q), "pruning-keys.svg": fig_pruning_keys(q),
            "query-time.svg": fig_query_time(q), "write-cost.svg": fig_write_cost(e),
            "file-level.svg": fig_file_level()}
    for name, content in figs.items():
        (OUT / name).write_text(content)
        print(OUT / name)


if __name__ == "__main__":
    main()
