#!/usr/bin/env python3
"""Figures for the write-up and the video.

    analysis/plots.py data/                    # every figure it has data for
    analysis/plots.py data/ --only pareto
    analysis/plots.py data/ --dark             # dark steps, for slides
    analysis/plots.py data/<run_id> --only trace

Reads the summary.json that analysis/energy_analysis.py writes into each run
directory, so run that first.

Figures, in the order they should appear in the write-up:

  pareto      average power against detection rate, one line per event rate,
              with the model's predicted straight line dashed over each. THE
              HEADLINE -- lead with this, not with the camera.
  trace       one run's power over time, with boot windows shaded. The picture
              that makes the boot cost obvious at a glance.
  duration    detection rate against event duration, showing the blind window.
  quantization  INT8 vs FP32: latency, energy per inference, accuracy.
  roc         Tier 1 detection rate against false-trigger rate.

Chart conventions, applied deliberately
---------------------------------------
Categorical hues are assigned in a fixed order and never cycled, so a series
keeps its colour when the set of runs changes. No chart has two y-axes -- the
quantization comparison is three panels precisely because latency, energy and
accuracy do not share a scale, and putting two of them on one pair of axes would
invite exactly the wrong comparison. Every multi-series chart carries both a
legend and direct labels, so identity is never colour alone; the light palette's
aqua and yellow sit below 3:1 against the surface, and the direct labels are what
make that legible rather than decorative. Marks are thin, the grid is recessive,
and value text is ink-coloured rather than series-coloured.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "analysis"))

# Fixed categorical order. Assigned by series identity, never by rank, so a
# filtered-out run does not repaint the survivors.
LIGHT = {
    "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"],
    "surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "ink3": "#8a8880",
    "grid": "#e4e3de",
}
DARK = {
    "series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"],
    "surface": "#1a1a19", "ink": "#ffffff", "ink2": "#c3c2b7", "ink3": "#8a8880",
    "grid": "#33322f",
}


def style(plt, C) -> None:
    plt.rcParams.update({
        "figure.facecolor": C["surface"], "axes.facecolor": C["surface"],
        "savefig.facecolor": C["surface"],
        "axes.edgecolor": C["grid"], "axes.labelcolor": C["ink2"],
        "axes.titlecolor": C["ink"], "text.color": C["ink"],
        "xtick.color": C["ink2"], "ytick.color": C["ink2"],
        "grid.color": C["grid"], "grid.linewidth": 0.8,
        "axes.grid": True, "axes.axisbelow": True,
        "axes.spines.top": False, "axes.spines.right": False,
        "font.size": 10, "axes.titlesize": 12, "legend.frameon": False,
        "lines.linewidth": 2.0, "lines.markersize": 7,
    })


def load_runs(root: Path) -> list[dict]:
    out = []
    for s in sorted(root.rglob("summary.json")):
        try:
            out.append(json.loads(s.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return [r for r in out if r.get("avg_power_W") is not None]


def group_by(runs: list[dict], key: str) -> dict:
    groups: dict = {}
    for r in runs:
        v = r.get("params", {}).get(key)
        if v is not None:
            groups.setdefault(v, []).append(r)
    return dict(sorted(groups.items()))


def constants(path: Path | None) -> dict:
    import power_model
    return power_model.load_params(str(path) if path and path.exists() else None)


# ------------------------------------------------------------------- figures

def fig_pareto(runs, C, args, plt):
    """The headline: average power against detection rate, per event rate."""
    import power_model
    groups = group_by(runs, "mean_interval")
    if not groups:
        return None
    p = constants(args.constants)
    duration_s = (runs[0].get("params", {}).get("duration_ms") or 15000) / 1000.0

    fig, ax = plt.subplots(figsize=(7.2, 5.0))
    for i, (interval, rs) in enumerate(groups.items()):
        colour = C["series"][i % len(C["series"])]
        pts = sorted(((r["detection_rate"], r["avg_power_W"]) for r in rs
                      if r.get("detection_rate") is not None))
        if not pts:
            continue
        xs, ys = zip(*pts)
        ax.plot(xs, ys, "o-", color=colour, zorder=3,
                markeredgecolor=C["surface"], markeredgewidth=1.5,
                label=f"{interval:g} s mean interval")

        # The model's prediction: a straight line, slope K, sign-flipping at the
        # break-even. Dashed so measurement and prediction are never confused.
        lam = 1.0 / float(interval)
        px = [0.0, 1.0]
        py = [power_model.avg_power(p, lam, t, duration_s)
              for t in (0.0, math.inf)]
        ax.plot(px, py, "--", color=colour, linewidth=1.3, alpha=0.65, zorder=2)

        # Direct label. The light palette's aqua and yellow are below 3:1 against
        # the surface, so this label is what discharges the relief rule.
        ax.annotate(f"{interval:g} s", (xs[-1], ys[-1]),
                    textcoords="offset points", xytext=(8, 0),
                    color=C["ink2"], fontsize=9, va="center")

    be = None
    try:
        import power_model as pm
        be = pm.breakeven_interval(p)
    except Exception:
        pass
    ax.set_xlabel("detection rate")
    ax.set_ylabel("average system power (W)")
    ax.set_title("Dormancy buys detection at a fixed price per point"
                 + (f"\npredicted break-even at a {be:.0f} s mean interval"
                    if be else ""))
    ax.set_xlim(-0.03, 1.14)
    # Below the axes: these lines run corner to corner, so any in-axes placement
    # sits on top of a series.
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.13),
              ncol=min(4, len(groups)))
    ax.text(0.01, 0.98, "solid: measured   dashed: model", transform=ax.transAxes,
            ha="left", va="top", color=C["ink3"], fontsize=8)
    fig.tight_layout()
    return fig


def fig_trace(run_dir: Path, C, args, plt):
    """One run's power over time, with the boot windows shaded."""
    sys.path.insert(0, str(REPO / "analysis"))
    import energy_analysis as ea

    t, w, _ = ea.read_power(run_dir / "power.csv")
    if not t:
        return None
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    t0 = t[0]
    rel = [x - t0 for x in t]

    fig, ax = plt.subplots(figsize=(10.0, 3.8))
    # Single series: no legend box; the title names it.
    ax.plot(rel, w, color=C["series"][0], linewidth=1.0)

    levels = [lv["watts"] for lv in ea.find_levels(t, w, min_dwell_s=args.min_dwell_s)]
    boots, _ = ea.find_boot_windows(t, w, levels, args.min_boot_s)
    for k, (b0, b1) in enumerate(boots):
        ax.axvspan(b0 - t0, b1 - t0, color=C["series"][1], alpha=0.16, zorder=0,
                   label="boot" if k == 0 else None)

    gen = ea.read_csv(run_dir / "gen.csv")
    for g in gen:
        if g.get("image_id") == "NONE":
            continue
        try:
            ax.axvline(float(g["t_mac"]) - t0, color=C["ink3"], linewidth=0.6,
                       alpha=0.5, zorder=1)
        except (KeyError, ValueError):
            continue

    for name, key in (("P_idle", "p_idle_est_W"), ("P_halt", "p_halt_est_W")):
        v = summary.get(key)
        if v is not None:
            ax.axhline(v, color=C["ink3"], linewidth=1.0, linestyle=":", zorder=1)
            # A surface-coloured plate behind the text: these labels sit on the
            # plateau they name, so without it they are illegible.
            ax.annotate(f"{name} {v:.2f} W", (rel[-1], v), xytext=(-4, 5),
                        textcoords="offset points", ha="right",
                        color=C["ink2"], fontsize=8,
                        bbox=dict(facecolor=C["surface"], edgecolor="none",
                                  pad=1.5, alpha=0.92))

    eb = (summary.get("E_boot_J") or {}).get("mean")
    ax.set_xlabel("seconds into the run")
    ax.set_ylabel("power (W)")
    ax.set_title(f"{run_dir.name} — power over time"
                 + (f"   ({len(boots)} boots, {eb:.0f} J each)" if eb else "")
                 + "\nvertical rules are stimulus events; shading is a boot")
    ax.set_ylim(min(w) - 0.15, max(w) + 0.35)
    if boots:
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.22), ncol=1)
    fig.tight_layout()
    return fig


def fig_duration(runs, C, args, plt):
    """Detection rate against event duration -- the blind window, directly."""
    groups = group_by(runs, "duration_ms")
    if len(groups) < 2:
        return None
    p = constants(args.constants)
    xs = [d / 1000.0 / p.t_boot for d in groups]
    ys = [sum(r["detection_rate"] for r in rs) / len(rs) for rs in groups.values()]

    fig, ax = plt.subplots(figsize=(6.4, 4.4))
    ax.plot(xs, ys, "o-", color=C["series"][0],
            markeredgecolor=C["surface"], markeredgewidth=1.5, zorder=3)
    ax.axvline(1.0, color=C["ink3"], linestyle=":", linewidth=1.0)
    ax.annotate("event outlives a boot →", (1.02, 0.04), color=C["ink2"], fontsize=9)

    interval = runs[0].get("params", {}).get("mean_interval")
    if interval:
        ceiling = 1.0 / (1.0 + p.t_boot / float(interval))
        ax.axhline(ceiling, color=C["series"][1], linestyle="--", linewidth=1.3)
        ax.annotate(f"predicted ceiling {ceiling:.2f}\n(arrivals lost inside each boot)",
                    (0.02, ceiling), xytext=(0, 6), textcoords="offset points",
                    color=C["ink2"], fontsize=9)
    ax.set_xlabel("event duration / boot time")
    ax.set_ylabel("detection rate")
    ax.set_ylim(-0.03, 1.05)
    ax.set_title("The cost of the blind window")
    fig.tight_layout()
    return fig


def fig_quantization(runs, C, args, plt):
    """INT8 vs FP32. Three panels, because three measures, and never two y-axes."""
    groups = group_by(runs, "model")
    if len(groups) < 2:
        return None
    order = [m for m in ("int8", "fp32") if m in groups]
    colours = [C["series"][0], C["series"][1]]

    panels = [
        ("inference latency", "ms",
         lambda rs: _mean(rs, lambda r: r.get("infer_ms_p50"))),
        ("energy per event, net of idle", "J",
         lambda rs: _mean(rs, lambda r: r.get("energy_per_event_net_of_idle_J"))),
        ("average power", "W",
         lambda rs: _mean(rs, lambda r: r.get("avg_power_W"))),
    ]
    fig, axes = plt.subplots(1, len(panels), figsize=(10.5, 3.9))
    for ax, (title, unit, fn) in zip(axes, panels):
        vals = [fn(groups[m]) for m in order]
        bars = ax.bar([m.upper() for m in order], vals, color=colours[:len(order)],
                      width=0.55)
        for b, v in zip(bars, vals):
            if v is None or (isinstance(v, float) and math.isnan(v)):
                continue
            # Value text wears ink, not the series colour.
            ax.annotate(f"{v:.3g}", (b.get_x() + b.get_width() / 2, v),
                        xytext=(0, 4), textcoords="offset points",
                        ha="center", color=C["ink"], fontsize=9)
        ax.set_title(title, fontsize=11)
        ax.set_ylabel(unit)
        ax.grid(axis="x", visible=False)
    fig.suptitle("Quantization as an energy lever — expect INT8 to draw MORE "
                 "power and use LESS energy", fontsize=11, color=C["ink"])
    fig.tight_layout()
    return fig


def _mean(rs, fn):
    vals = [fn(r) for r in rs]
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else float("nan")


def fig_roc(roc_csv: Path, C, args, plt):
    """Tier 1: detection rate against false triggers per minute."""
    import csv as _csv
    if not roc_csv.exists():
        return None
    rows = list(_csv.DictReader(open(roc_csv, newline="")))
    if not rows:
        return None
    groups: dict = {}
    for r in rows:
        groups.setdefault(r.get("trimmer", "?"), []).append(r)

    fig, ax = plt.subplots(figsize=(6.4, 4.8))
    for i, (trim, rs) in enumerate(sorted(groups.items())):
        colour = C["series"][i % len(C["series"])]
        pts = []
        for r in rs:
            try:
                pts.append((float(r["false_per_min"]), float(r["detect_rate"])))
            except (KeyError, ValueError):
                continue
        if not pts:
            continue
        pts.sort()
        xs, ys = zip(*pts)
        ax.plot(xs, ys, "o-", color=colour, label=f"trimmer {trim}",
                markeredgecolor=C["surface"], markeredgewidth=1.5)
        ax.annotate(str(trim), (xs[-1], ys[-1]), xytext=(8, 0),
                    textcoords="offset points", color=C["ink2"], fontsize=9,
                    va="center")
    ax.set_xlabel("false triggers per minute")
    ax.set_ylabel("detection rate")
    ax.set_title("Tier 1: sensitivity against false triggers")
    ax.legend(loc="lower right")
    fig.tight_layout()
    return fig


# --------------------------------------------------------------------- entry

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path, help="data/ root, or one run directory")
    ap.add_argument("-o", "--out", type=Path, default=REPO / "analysis" / "figures")
    ap.add_argument("--only", nargs="+",
                    choices=["pareto", "trace", "duration", "quantization", "roc"])
    ap.add_argument("--dark", action="store_true", help="dark steps, for slides")
    ap.add_argument("--constants", type=Path, default=REPO / "data" / "constants.json")
    ap.add_argument("--roc-csv", type=Path,
                    default=REPO / "data" / "tier1_roc.csv")
    ap.add_argument("--min-boot-s", type=float, default=8.0)
    ap.add_argument("--min-dwell-s", type=float, default=1.0)
    ap.add_argument("--format", default="png", choices=["png", "pdf", "svg"])
    ap.add_argument("--dpi", type=int, default=200)
    args = ap.parse_args()

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        sys.exit("matplotlib not installed. Run tools/setup_mac.sh, then use "
                 ".venv/bin/python")

    C = DARK if args.dark else LIGHT
    style(plt, C)
    args.out.mkdir(parents=True, exist_ok=True)

    runs = load_runs(args.path if args.path.is_dir() else args.path.parent)
    print(f"{len(runs)} analysed run(s) under {args.path}")
    if not runs:
        print("Nothing to plot yet. Run analysis/energy_analysis.py on the run\n"
              "directories first -- it writes the summary.json this reads.")

    want = set(args.only) if args.only else {"pareto", "trace", "duration",
                                             "quantization", "roc"}
    made = []
    suffix = "_dark" if args.dark else ""
    builders = [
        ("pareto", lambda: fig_pareto(runs, C, args, plt)),
        ("duration", lambda: fig_duration(runs, C, args, plt)),
        ("quantization", lambda: fig_quantization(runs, C, args, plt)),
        ("roc", lambda: fig_roc(args.roc_csv, C, args, plt)),
    ]
    if (args.path / "power.csv").exists():
        builders.append(("trace", lambda: fig_trace(args.path, C, args, plt)))
    elif runs:
        newest = max((p.parent for p in args.path.rglob("power.csv")),
                     key=lambda d: d.stat().st_mtime, default=None)
        if newest:
            builders.append(("trace", lambda: fig_trace(newest, C, args, plt)))

    for name, build in builders:
        if name not in want:
            continue
        try:
            fig = build()
        except Exception as e:
            print(f"  {name}: failed -- {type(e).__name__}: {e}")
            continue
        if fig is None:
            print(f"  {name}: not enough data yet")
            continue
        path = args.out / f"{name}{suffix}.{args.format}"
        fig.savefig(path, dpi=args.dpi, bbox_inches="tight")
        plt.close(fig)
        made.append(path)
        print(f"  wrote {path}")

    if made:
        print(f"\n{len(made)} figure(s). Open them and look at them before "
              f"using them -- \nnothing here checks for label collisions or overflow.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
