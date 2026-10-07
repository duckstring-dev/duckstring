"""Shared SVG helpers for the blog figures: the dark chart palette (the site is dark only), text, bars and a
grouped horizontal bar chart. Categorical blue, orange and aqua, in that fixed order, are validated for
colour-vision deficiency on the dark surface; neutral grey is for reference bars."""

from __future__ import annotations

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
    """Grouped horizontal bars: one group per category, one bar per series, values labelled at the bar end."""
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


ARROW_DEFS = (f'<defs>'
              f'<marker id="ah-blue" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
              f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{BLUE}"/></marker>'
              f'<marker id="ah-orange" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
              f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{ORANGE}"/></marker>'
              f'<marker id="ah-muted" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
              f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{MUTED}"/></marker>'
              f'</defs>')


def node(cx, cy, label, w=120, h=36, stroke=MUTED, fill="#242422", weight=600, sub=None, dashed=False) -> str:
    """A rounded box centred on (cx, cy) with a label, and an optional smaller second line."""
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    out = (f'<rect x="{cx - w / 2:.1f}" y="{cy - h / 2:.1f}" width="{w}" height="{h}" rx="8" fill="{fill}" '
           f'stroke="{stroke}" stroke-width="1.5"{dash}/>')
    if sub:
        out += text(cx, cy - 2, label, 13, TEXT, "middle", weight) + text(cx, cy + 13, sub, 11, TEXT_2, "middle")
    else:
        out += text(cx, cy + 4.5, label, 13, TEXT, "middle", weight)
    return out


def arrow(x1, y1, x2, y2, color="muted", dashed=False, width=1.5) -> str:
    stroke = {"blue": BLUE, "orange": ORANGE, "muted": MUTED}[color]
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    return (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{stroke}" stroke-width="{width}"'
            f'{dash} marker-end="url(#ah-{color})"/>')
