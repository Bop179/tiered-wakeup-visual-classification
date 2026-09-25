#!/usr/bin/env python3
"""Simulated expected outcome per stimulus -> <run>/predicted_event.csv.

NOT a measurement. Replays gen.csv through a toy model of the system:
Pi starts awake, halts after `dormancy` idle (never if -1), Tier 1 wakes it on
a stimulus, and it is ready T_BOOT later. A stimulus is captured if the Pi is
ready before it goes dark, and a captured image is assumed classified right.
Every row carries source=simulated so it can't be mistaken for events.csv.

  python3 tools/predict_events.py data/<run> [data/<run> ...]
"""
import csv, json, sys
from pathlib import Path

T_BOOT = 25.6  # s, wake -> "# ready", EXPERIMENTS.md 0.4 (n=3)
# ponytail: assumes 100% accuracy once captured and instant halts; add the
# measured awake accuracy / halt time if the prediction needs to be sharper.


def predict(stimuli, t0, dormancy_s, duration_s):
    ready_at, awake_until = t0, float("inf") if dormancy_s < 0 else t0 + dormancy_s
    for s in stimuli:
        t = s["t"]
        if ready_at <= t < awake_until:
            state = "awake"
        elif t < ready_at:
            state = "booting"          # woken by an earlier stimulus, not ready yet
        else:
            state, ready_at = "halted", t + T_BOOT
        captured = ready_at < t + duration_s
        if dormancy_s >= 0:
            awake_until = max(ready_at, t if captured else 0) + dormancy_s
        yield state, ready_at, captured


def run(d):
    d = Path(d)
    m = json.loads((d / "manifest.json").read_text())
    p = m["params"]
    dormancy_s = p["dormancy_ms"] / 1000 if p["dormancy_ms"] >= 0 else -1
    rows = list(csv.DictReader(open(d / "gen.csv")))
    stim = [{"t": float(r["t_mac"])} for r in rows]
    out = open(d / "predicted_event.csv", "w", newline="")
    w = csv.writer(out)
    w.writerow(["t_mac", "event_idx", "image_id", "true_class", "is_target",
                "pi_state", "t_ready_mac", "outcome", "predicted_class",
                "predicted_fired", "source"])
    n = 0
    for r, (state, ready, cap) in zip(rows, predict(
            stim, m["clock_start"]["t_mac_mid"], dormancy_s, p["duration_ms"] / 1000)):
        n += cap
        w.writerow([r["t_mac"], r["event_idx"], r["image_id"], r["true_class"],
                    r["is_target"], state, f"{ready:.3f}",
                    "captured" if cap else "missed",
                    r["true_class"] if cap else "none",
                    int(cap and r["is_target"] == "1"), "simulated"])
    out.close()
    print(f"{d.name}: {n}/{len(rows)} predicted captured")


if __name__ == "__main__":
    # self-check: never-sleep catches all; 15 s dormancy + 15 s stimulus misses after a gap
    assert all(c for *_, c in predict([{"t": t} for t in (5, 100, 500)], 0, -1, 15))
    got = [c for *_, c in predict([{"t": t} for t in (5, 100)], 0, 15, 15)]
    assert got == [True, False], got
    for d in sys.argv[1:]:
        run(d)
