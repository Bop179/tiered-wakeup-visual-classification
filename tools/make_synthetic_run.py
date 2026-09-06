#!/usr/bin/env python3
"""Fabricate a physically plausible run directory, so the analysis can be tested now.

    tools/make_synthetic_run.py -o data/synthetic --mean-interval 45 --dormancy 30
    analysis/energy_analysis.py data/synthetic

Synthesises gen.csv, power.csv, events.csv, daemon.log and manifest.json from the
same discrete-event model as analysis/power_model.py, with a clapperboard, boot
ramps, inference spikes and measurement noise. Nothing here is data -- it exists
so that energy_analysis.py is already debugged when the first real trace arrives
on Sep 10, instead of being written and debugged against it.

It is also the fixture for the round-trip check that matters: feed it known
constants, and the analysis should recover them. If it cannot recover them from a
trace where the truth is known, it will not recover them from a real one.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--n-events", type=int, default=25)
    ap.add_argument("--mean-interval", type=float, default=45.0)
    ap.add_argument("--dormancy", type=float, default=30.0)
    ap.add_argument("--duration-ms", type=int, default=15000)
    ap.add_argument("--p-idle", type=float, default=2.5)
    ap.add_argument("--p-halt", type=float, default=0.5)
    ap.add_argument("--p-boot", type=float, default=3.5)
    ap.add_argument("--t-boot", type=float, default=30.0)
    ap.add_argument("--p-infer", type=float, default=5.5)
    ap.add_argument("--infer-s", type=float, default=0.12)
    ap.add_argument("--noise", type=float, default=0.05, help="W, gaussian")
    ap.add_argument("--rate", type=float, default=100.0, help="power samples/s")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    t_start = time.time()

    # --- timeline: (t, state) plus the events that actually got classified
    CLAP = 2.0
    segments: list[tuple[float, float, str]] = []      # (t0, t1, kind)
    gen_rows, evt_rows = [], []
    t_clap = t_start + 3.0
    segments.append((t_start, t_clap, "idle"))
    segments.append((t_clap, t_clap + CLAP, "clap"))

    cursor = t_clap + CLAP
    halt_at = cursor + args.dormancy
    state = "awake"
    t_event = cursor + 5.0
    idx = 0
    n_boots = 0

    for _ in range(args.n_events):
        gen_rows.append({"t_mac": t_event, "image_id": f"img_{idx:04d}.jpg",
                         "true_class": "banana" if idx % 7 == 0 else f"class_{idx % 9}",
                         "duration_ms": args.duration_ms})

        if t_event < cursor:                       # arrived mid-boot: blind
            pass
        else:
            if state == "awake" and halt_at <= t_event:
                segments.append((cursor, halt_at, "idle"))
                segments.append((halt_at, t_event, "halt"))
                cursor, state = t_event, "halted"
            if state == "awake":
                segments.append((cursor, t_event, "idle"))
                segments.append((t_event, t_event + args.infer_s, "infer"))
                cursor = t_event + args.infer_s
                halt_at = t_event + args.dormancy
                evt_rows.append({"t_pi": t_event, "state": "awake"})
            else:
                segments.append((cursor, t_event, "halt"))
                segments.append((t_event, t_event + args.t_boot, "boot"))
                cursor = t_event + args.t_boot
                n_boots += 1
                state = "awake"
                halt_at = cursor + args.dormancy
                if args.duration_ms / 1000.0 >= args.t_boot:
                    segments.append((cursor, cursor + args.infer_s, "infer"))
                    cursor += args.infer_s
                    evt_rows.append({"t_pi": t_event, "state": "booted"})
        idx += 1
        t_event += rng.expovariate(1.0 / args.mean_interval)

    t_end = max(cursor, t_event) + 10.0
    segments.append((cursor, t_end, "idle" if state == "awake" else "halt"))

    watts = {"idle": args.p_idle, "halt": args.p_halt, "boot": args.p_boot,
             "infer": args.p_infer, "clap": args.p_boot + 1.0}

    # --- power.csv
    step = 1.0 / args.rate
    energy = charge = 0.0
    with open(out / "power.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["t_mac", "sample_idx", "voltage_V", "current_A", "power_W",
                    "dp_V", "dn_V", "temp_C", "energy_J", "charge_C"])
        n = 0
        for t0, t1, kind in segments:
            if t1 <= t0:
                continue
            base = watts[kind]
            t = t0
            while t < t1:
                p = max(0.0, base + rng.gauss(0.0, args.noise))
                v = 5.1 + rng.gauss(0.0, 0.005)
                i = p / v
                energy += p * step
                charge += i * step
                w.writerow([f"{t:.3f}", n, f"{v:.5f}", f"{i:.5f}", f"{p:.5f}",
                            "0.000", "0.000", "31.0", f"{energy:.5f}",
                            f"{charge:.5f}"])
                n += 1
                t += step

    with open(out / "gen.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["t_mac", "event_idx", "image_id", "true_class", "true_class_id",
                    "patch_contrast", "duration_ms", "is_target"])
        for i, g in enumerate(gen_rows):
            w.writerow([f"{g['t_mac']:.3f}", i, g["image_id"], g["true_class"], -1,
                        "0.800", g["duration_ms"], int(g["true_class"] == "banana")])

    with open(out / "events.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["t_pi", "event_idx", "arduino_t_ms", "peak", "evt_duration_ms",
                    "state_at_evt", "capture_ms", "infer_ms", "latency_ms",
                    "class_id", "class_name", "confidence", "top5", "fired"])
        for i, e in enumerate(evt_rows):
            w.writerow([f"{e['t_pi']:.3f}", i, i * 1000, 800, 200, e["state"],
                        "12.0", f"{args.infer_s * 1000:.1f}",
                        f"{args.infer_s * 1000 + 20:.1f}", 955, "banana", "0.812",
                        "955:0.812", int(i % 7 == 0)])

    (out / "daemon.log").write_text(
        f"# clapperboard {t_clap:.3f} {CLAP:g}s\n# ready {t_clap + CLAP:.3f}\n")
    (out / "manifest.json").write_text(json.dumps({
        "run_id": out.name, "synthetic": True, "git_sha": "synthetic",
        "params": {"mean_interval": args.mean_interval,
                   "duration_ms": args.duration_ms, "contrast": 0.8,
                   "dormancy_ms": int(args.dormancy * 1000),
                   "n_events": args.n_events, "model": "int8"},
        "ground_truth": {"p_idle": args.p_idle, "p_halt": args.p_halt,
                         "p_boot": args.p_boot, "t_boot": args.t_boot,
                         "e_boot": args.p_boot * args.t_boot, "n_boots": n_boots,
                         "n_detected": len(evt_rows), "n_generated": len(gen_rows)},
        "clock_start": {"offset_s": 0.0},
    }, indent=2))

    print(f"wrote {out}")
    print(f"  ground truth: E_boot {args.p_boot * args.t_boot:.0f} J, "
          f"{n_boots} boots, {len(evt_rows)}/{len(gen_rows)} detected "
          f"({len(evt_rows) / len(gen_rows):.3f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
