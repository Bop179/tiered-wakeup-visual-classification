#!/usr/bin/env python3
"""Latency breakdown for Tier 2. Run on the Pi, Sep 7, before anything depends on it.

    tools/latency_bench.py --model int8 -n 200
    tools/latency_bench.py --model int8 --model-only -n 500     # no camera needed
    tools/latency_bench.py --compare -n 200                     # int8 vs fp32

Reports capture, inference and end-to-end as distributions, not means. The tail
is what determines whether an event is caught, and a mean hides it.

This is a Sep 7 blocking measurement: if inference is far slower than the ~100 ms
assumed in analysis/power_model.py, the tier boundary moves, and it is much
better to learn that on day one than during the matrix. Feed the measured
E_infer straight into data/constants.json.

Thermal note: a Pi 4 under sustained inference will throttle without a heatsink,
and a throttled run measures the cooling, not the workload. This checks
get_throttled before and after and refuses to report a clean result if the state
changed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pi"))


def vcgencmd(arg: str) -> str:
    try:
        return subprocess.run(["vcgencmd", arg], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


def summarise(name: str, values: list[float]) -> dict:
    if not values:
        return {}
    s = sorted(values)
    out = {"n": len(s), "min": s[0], "p50": pct(s, 0.5), "p95": pct(s, 0.95),
           "p99": pct(s, 0.99), "max": s[-1], "mean": sum(s) / len(s)}
    print(f"{name:<14} n={out['n']:<5} min {out['min']:7.2f}  p50 {out['p50']:7.2f}  "
          f"p95 {out['p95']:7.2f}  p99 {out['p99']:7.2f}  max {out['max']:7.2f}  "
          f"mean {out['mean']:7.2f}   ms")
    return out


def bench(model: str, n: int, warmup: int, model_only: bool,
          models_dir: Path, threads: int, swap_rgb: bool) -> dict:
    import classify
    clf = classify.Classifier(model, models_dir, threads)
    cam = None if model_only else classify.Camera(clf.width, clf.height, swap_rgb)

    if cam is not None:
        frame, _ = cam.capture()
    else:
        import numpy as np
        frame = np.random.randint(0, 256, (clf.height, clf.width, 3), dtype=np.uint8)

    for _ in range(warmup):
        clf.infer(frame)

    cap_ms, inf_ms, e2e_ms = [], [], []
    for _ in range(n):
        t0 = time.perf_counter()
        if cam is not None:
            frame, c = cam.capture()
            cap_ms.append(c)
        inf_ms.append(clf.infer(frame)[3])
        e2e_ms.append((time.perf_counter() - t0) * 1000.0)

    if cam is not None:
        cam.close()

    print(f"\n{model.upper()}  {clf.model_path.name}  threads={threads}")
    out = {"model": model, "file": clf.model_path.name, "threads": threads,
           "inference": summarise("inference", inf_ms),
           "end_to_end": summarise("end-to-end", e2e_ms)}
    if cap_ms:
        out["capture"] = summarise("capture", cap_ms)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", choices=["int8", "fp32"], default="int8")
    ap.add_argument("--compare", action="store_true", help="benchmark both models")
    ap.add_argument("-n", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=20,
                    help="untimed runs first; the first invoke allocates and is slow")
    ap.add_argument("--model-only", action="store_true", help="skip the camera")
    ap.add_argument("--models-dir", type=Path, default=REPO / "pi" / "models")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--swap-rgb", dest="swap_rgb", action="store_true", default=True)
    ap.add_argument("--no-swap-rgb", dest="swap_rgb", action="store_false")
    ap.add_argument("-o", "--out", type=Path, help="write results as JSON")
    args = ap.parse_args()

    before = vcgencmd("get_throttled")
    print(f"throttled before: {before}   temp: {vcgencmd('measure_temp')}")
    if before not in ("throttled=0x0", "unavailable"):
        print("WARNING: already throttled or under-volted. Fix the supply first --"
              "\n         every number below is measuring the power supply, not the Pi.")

    models = ["int8", "fp32"] if args.compare else [args.model]
    results = {}
    for m in models:
        results[m] = bench(m, args.n, args.warmup, args.model_only,
                           args.models_dir, args.threads, args.swap_rgb)

    after = vcgencmd("get_throttled")
    print(f"\nthrottled after:  {after}   temp: {vcgencmd('measure_temp')}")
    if after != before:
        print("WARNING: throttling state CHANGED during the run. Discard these"
              "\n         numbers, fit a heatsink, and run again.")

    if args.compare and all(m in results for m in ("int8", "fp32")):
        i = results["int8"]["inference"]["p50"]
        f = results["fp32"]["inference"]["p50"]
        print(f"\nINT8 is {f / i:.2f}x faster than FP32 at p50 "
              f"({i:.1f} ms vs {f:.1f} ms)")
        print("Energy per inference needs the FNB58, not this script -- and expect"
              "\nINT8 to draw MORE instantaneous power while using LESS energy per"
              "\ninference. Race to idle: power alone cannot tell efficient from stalled.")

    results["throttled_before"] = before
    results["throttled_after"] = after
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, indent=2))
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
