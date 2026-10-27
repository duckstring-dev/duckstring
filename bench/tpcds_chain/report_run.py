"""One Duckstring run's records as a timeline and a summary: what ran when, what waited for memory, and what
memory each Duck used.

    python report_run.py DIR TAG [--out PREFIX]

Reads, from DIR (a node's /root/out, or a local run's directory, with the single.sh names):

    runs_{TAG}.json      the runner's results: phases, events, the Catchment's whole Run history
    status_{TAG}.jsonl   /api/status changes, sampled every 0.25 s (memory waits)
    res_{TAG}.json       resources.py: memory and CPU per process, the machine's memory
    mem_{TAG}/*.jsonl    each Duck's own samples: RSS, DuckDB's memory by tag, its limit, its spill

and writes PREFIX.png (default DIR/report_{TAG}) and PREFIX.json. The figure, on one clock (seconds from the
Catchment's start): every Ripple Run as a bar per Pond (with the wait from dispatch to the Ripple starting, and
any time spent waiting for a memory slot), releases and phases as rules, the machine's memory by Duck, each
Duck's memory against its DuckDB limit, and CPU by Duck. The summary gives per Pond: real Runs and passes, busy
and waiting time, peak RSS, DuckDB peak, limits and spill; and the phases and continuous lags.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

PONDS = ("sales_feed", "dimensions", "sales_enriched", "category_month", "store_quarter", "customer_value")
COLOURS = dict(zip(PONDS, ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"), strict=True))
INK, MUTED, GRID, WAIT = "#2b2b2b", "#77766f", "#e6e5df", "#9b9a93"
GB = 1e9


def _ts(s: str | None) -> float | None:
    return datetime.fromisoformat(s).timestamp() if s else None


def _size(s: str | None) -> float | None:
    """DuckDB's memory_limit text ('25.5 GiB', '4000.0 MB') in bytes."""
    m = re.match(r"([\d.]+)\s*([KMGT]i?B)", s or "")
    if not m:
        return None
    unit = m.group(2)
    base = 1024 if "i" in unit else 1000
    return float(m.group(1)) * base ** "KMGT".index(unit[0]) * base


def load(d: Path, tag: str) -> dict:
    runs = json.loads((d / f"runs_{tag}.json").read_text())
    status = []
    sp = d / f"status_{tag}.jsonl"
    if sp.exists():
        status = [json.loads(line) for line in sp.read_text().splitlines() if line.strip()]
    res = json.loads((d / f"res_{tag}.json").read_text()) if (d / f"res_{tag}.json").exists() else None
    mem: dict[str, list[dict]] = {}
    for f in sorted((d / f"mem_{tag}").glob("*.jsonl")) if (d / f"mem_{tag}").exists() else []:
        recs = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
        pond = next((r.get("pond") for r in recs if r.get("event") == "start"), None)
        if pond:
            mem.setdefault(pond, []).extend(recs)  # a Pond respawned keeps one series
    for recs in mem.values():
        recs.sort(key=lambda r: r["t"])
    return {"runs": runs, "status": status, "res": res, "mem": mem}


def waits(status: list[dict]) -> dict[str, list[tuple[float, float]]]:
    """Each Pond's intervals in the 'waiting' (for a memory slot) status, from the sampled changes."""
    out: dict[str, list[tuple[float, float]]] = {p: [] for p in PONDS}
    since: dict[str, float] = {}
    t = None
    for rec in status:
        t = rec["t"]
        for p, v in rec["ponds"].items():
            if v.get("status") == "waiting" and p not in since:
                since[p] = t
            elif v.get("status") != "waiting" and p in since:
                out[p].append((since.pop(p), t))
    for p, t0 in since.items():
        out[p].append((t0, t))
    return out


def summarise(data: dict) -> dict:
    hist = data["runs"].get("history", [])
    w = waits(data["status"])
    ponds = {}
    for p in PONDS:
        runs = [r for r in hist if r["pond"] == p]
        real = [r for r in runs if r.get("ripples")]
        rips = [x for r in real for x in r["ripples"] if x.get("started_at") and x.get("finished_at")]
        busy = sum(_ts(x["finished_at"]) - _ts(x["started_at"]) for x in rips)
        queued = [_ts(x["started_at"]) - _ts(r["started_at"]) for r in real for x in r["ripples"][:1]
                  if x.get("started_at") and r.get("started_at")]
        m = data["mem"].get(p, [])
        samples = [r for r in m if "event" not in r]
        limits = sorted({r["limit"] for r in samples if r.get("limit")}, key=lambda s: _size(s) or 0)
        res_peak = (data["res"] or {}).get("peak", {}).get("rss", {}).get(f"duck:{p}")
        ponds[p] = {
            "runs": len(runs), "real_runs": len(real), "passes": len(runs) - len(real),
            "failed": sum(1 for r in runs if r.get("status") == "failed"),
            "ripple_busy_s": round(busy, 2),
            "dispatch_to_start_s_max": round(max(queued), 2) if queued else None,
            "memory_wait_s": round(sum(b - a for a, b in w[p]), 2), "memory_waits": len(w[p]),
            "memory_wait_s_max": round(max((b - a for a, b in w[p]), default=0), 2),
            "rss_peak_gb": round(max([r["rss"] or 0 for r in samples] + [res_peak or 0]) / GB, 2),
            "duckdb_peak_gb": round(max((r.get("db") or 0 for r in samples), default=0) / GB, 2),
            "spill_peak_gb": round(max((max(r.get("db_spill") or 0, r.get("temp_files") or 0) for r in samples),
                                       default=0) / GB, 2),
            "limits": limits,
            "limit_events": [{"t": r["t"], "limit": r["limit"], "ok": r["ok"]} for r in m
                             if r.get("event") == "memory_limit"],
        }
    res = data["res"] or {}
    return {
        "phases": [{k: v for k, v in ph.items() if k != "runs"} for ph in data["runs"].get("phases", [])],
        "env": data["runs"].get("env"), "ponds": ponds,
        "machine": {"ram_gb": round(res.get("ram_total", 0) / GB, 1),
                    "rss_peak_gb": round(res.get("peak", {}).get("rss_total", 0) / GB, 2),
                    "available_min_gb": round(min((s["available"] for s in res.get("timeline", [])), default=0) / GB, 2),
                    "swap_peak_gb": round(res.get("peak", {}).get("swap", 0) / GB, 2),
                    "disk_written_gb": round(res.get("disk_written", 0) / GB, 1)},
        "status_samples": len(data["status"]),
    }


def _style(ax, ylabel: str) -> None:
    ax.set_ylabel(ylabel, color=MUTED, fontsize=8)
    ax.tick_params(colors=MUTED, labelsize=7, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)


def plot(data: dict, summary: dict, out: Path, title: str) -> None:
    runs = data["runs"]
    events = runs.get("events", [])
    res = data["res"] or {}
    t0 = min([e["t"] for e in events] + ([res["t0_epoch"]] if res.get("t0_epoch") else []))
    rel = lambda t: t - t0  # noqa: E731
    with_mem = [p for p in PONDS if data["mem"].get(p)]
    heights = [2.4, 1.6, *([0.9] * len(with_mem)), 1.1]
    fig, axes = plt.subplots(len(heights), 1, figsize=(13, sum(heights) + 1.2), sharex=True,
                             gridspec_kw={"height_ratios": heights, "hspace": 0.35})
    fig.patch.set_facecolor("white")
    gantt, machine, *ducks, cpu = axes
    fig.suptitle(title, x=0.01, ha="left", fontsize=11, color=INK)

    # Ripple Runs per Pond, newest Pond at the bottom; waits behind them.
    w = waits(data["status"])
    for i, p in enumerate(PONDS):
        y = len(PONDS) - 1 - i
        for a, b in w[p]:
            gantt.barh(y, rel(b) - rel(a), left=rel(a), height=0.8, color="none", edgecolor=WAIT, hatch="////",
                       linewidth=0)
        for r in (r for r in runs.get("history", []) if r["pond"] == p):
            if not r.get("ripples"):  # a pass: no Duck work
                t = _ts(r["started_at"])
                if t:
                    gantt.plot([rel(t)], [y - 0.3], marker="|", color=MUTED, markersize=4, linewidth=0)
                continue
            for x in r["ripples"]:
                a, b = _ts(x.get("started_at")), _ts(x.get("finished_at"))
                if a is None or b is None:
                    continue
                failed = x.get("status") == "failed"
                gantt.barh(y, max(b - a, 0.02), left=rel(a), height=0.5, color=COLOURS[p],
                           edgecolor="#e34948" if failed else "white", linewidth=1.5 if failed else 0.5)
            d = _ts(r.get("started_at"))
            first = min((_ts(x["started_at"]) for x in r["ripples"] if x.get("started_at")), default=None)
            if d and first and first - d > 0.05:  # dispatched, waiting for the Duck to start it
                gantt.plot([rel(d), rel(first)], [y, y], color=COLOURS[p], linewidth=1, alpha=0.6)
    gantt.set_yticks(range(len(PONDS)), list(reversed(PONDS)))
    gantt.set_ylim(-1.0, len(PONDS) - 0.4)
    _style(gantt, "")
    gantt.grid(False)
    n_phase = 0
    for e in events:
        if e["event"] == "release":
            for ax in axes:
                ax.axvline(rel(e["t"]), color=MUTED, linewidth=0.6, linestyle=(0, (2, 3)))
            gantt.text(rel(e["t"]), len(PONDS) - 0.45, f"b{e['batch']}", fontsize=6, color=MUTED, ha="center")
        elif e["event"] in ("phase_start", "continuous_start"):
            for ax in axes:
                ax.axvline(rel(e["t"]), color=INK, linewidth=0.6)
            gantt.text(rel(e["t"]), -0.55 - 0.35 * (n_phase % 2), " " + e.get("phase", "continuous"), fontsize=7,
                       color=INK)
            n_phase += 1

    # The machine's memory: each Duck's RSS stacked, the machine's use, the budget.
    tl = res.get("timeline", [])
    if tl:
        xs = [res["t0_epoch"] + s["t"] - t0 for s in tl]
        ys = [[s["rss"].get(f"duck:{p}", 0) / GB for s in tl] for p in PONDS]
        machine.stackplot(xs, ys, colors=[COLOURS[p] for p in PONDS], labels=PONDS, linewidth=0, alpha=0.9)
        machine.plot(xs, [s.get("used", 0) / GB for s in tl], color=INK, linewidth=1, label="machine in use")
        ram = res.get("ram_total", 0)
        budget = (summary.get("env") or {}).get("DUCKSTRING_MEMORY_BUDGET")
        machine.axhline(ram / GB, color=MUTED, linewidth=0.8, linestyle="--")
        machine.text(xs[0], ram / GB, f" RAM {ram / GB:.0f} GB", fontsize=6, color=MUTED, va="bottom")
        if budget is None and ram:
            budget_b = max(0.8 * ram - 2e9, 1e9)
            machine.axhline(budget_b / GB, color=MUTED, linewidth=0.8, linestyle=":")
            machine.text(xs[0], budget_b / GB, " slot budget (default)", fontsize=6, color=MUTED, va="bottom")
        machine.legend(fontsize=6, ncol=7, loc="upper left", frameon=False, bbox_to_anchor=(0, 1.25))
    _style(machine, "RSS, GB")

    # Each Duck: RSS, DuckDB's buffer use and its spill, against its memory_limit.
    for ax, p in zip(ducks, with_mem, strict=True):
        s = [r for r in data["mem"][p] if "event" not in r]
        xs = [rel(r["t"]) for r in s]
        ax.plot(xs, [(r.get("rss") or 0) / GB for r in s], color=COLOURS[p], linewidth=1.2, label="RSS")
        ax.fill_between(xs, [(r.get("db") or 0) / GB for r in s], color=COLOURS[p], alpha=0.25, linewidth=0,
                        label="DuckDB in use")
        ax.plot(xs, [max(r.get("db_spill") or 0, r.get("temp_files") or 0) / GB for r in s], color=INK,
                linewidth=0.8, linestyle=(0, (1, 1)), label="spilled")
        ax.step(xs, [(_size(r.get("limit")) or float("nan")) / GB for r in s], where="post", color=MUTED,
                linewidth=0.8, linestyle="--", label="DuckDB limit")
        ax.text(0.002, 0.92, p, transform=ax.transAxes, fontsize=7, color=INK, va="top")
        top = max([(r.get("rss") or 0) for r in s] + [(r.get("db") or 0) for r in s] + [1]) / GB
        ax.set_ylim(0, top * 1.35)  # the limit can sit far above use; keep use readable, the limit label says it
        lims = sorted({r["limit"] for r in s if r.get("limit")}, key=lambda v: _size(v) or 0)
        if lims:
            ax.text(0.998, 0.92, "limit " + " / ".join(lims), transform=ax.transAxes, fontsize=6, color=MUTED,
                    va="top", ha="right")
        _style(ax, "GB")
    if ducks:
        ducks[0].legend(fontsize=6, ncol=4, loc="upper center", frameon=False, bbox_to_anchor=(0.5, 1.35))

    # CPU by Duck, stacked, in cores.
    if tl and any("cpu" in s for s in tl):
        xs = [res["t0_epoch"] + s["t"] - t0 for s in tl]
        cpu.stackplot(xs, [[s.get("cpu", {}).get(f"duck:{p}", 0) for s in tl] for p in PONDS],
                      colors=[COLOURS[p] for p in PONDS], linewidth=0, alpha=0.9)
    _style(cpu, "CPU, cores")
    cpu.set_xlabel("seconds from the Catchment's start", color=MUTED, fontsize=8)
    fig.savefig(out, dpi=130, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", type=Path)
    ap.add_argument("tag")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    out = args.out or args.dir / f"report_{args.tag}"
    data = load(args.dir, args.tag)
    summary = summarise(data)
    out.with_suffix(".json").write_text(json.dumps(summary, indent=1))
    plot(data, summary, out.with_suffix(".png"), args.tag)
    print(json.dumps({"phases": summary["phases"], "machine": summary["machine"]}, indent=1))
    for p, v in summary["ponds"].items():
        print(f"{p:15s} real {v['real_runs']:4d} pass {v['passes']:4d} busy {v['ripple_busy_s']:7.2f}s "
              f"wait {v['memory_wait_s']:6.2f}s ({v['memory_waits']}) rss {v['rss_peak_gb']:5.2f} "
              f"db {v['duckdb_peak_gb']:5.2f} spill {v['spill_peak_gb']:5.2f} GB limits {v['limits']}")


if __name__ == "__main__":
    main()
