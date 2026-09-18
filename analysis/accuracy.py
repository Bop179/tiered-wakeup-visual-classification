#!/usr/bin/env python3
"""Accuracy lost, events missed and inferences avoided, per run.

    analysis/accuracy.py data/<run_id>/          one run, or data/ for every run
    analysis/accuracy.py --self-test

The other half of the result. energy_analysis.py says what a power policy costs in
joules; this says what it costs in answers:

  detection   which stimulus events produced a result at all
  accuracy    top-1 through the camera against the offline ceiling in
              data/reference.csv on the SAME images, so the gap is capture
              degradation and nothing else
  misses      real events with no result, and what the ceiling would have got
              right on them: accuracy lost to the power policy itself
  inferences  results actually computed, against an always-on camera classifying at
              a fixed rate for the whole run (the "tokens saved")

Stdlib only, like energy_analysis.py.

Pairing results with stimulus events
------------------------------------
docs/INTERFACE.md pairs the Nth GEN with the Nth RES. That breaks as soon as an
event is missed, or two land inside one boot (Tier 2 buffers one pending event and
a later one overwrites it). So each result is matched to the stimulus onset that
explains it, using arduino_t_ms: Tier 2's millis(). The Pi's own clock cannot be
used for this -- it has no RTC, and after every wake it runs from the last saved
time until NTP catches up (a minute off in s12d).

  1. fit Arduino time -> Mac time on awake results: offset, then slope, because a
     Uno's ceramic resonator drifts by around 0.1-0.5 %. millis() freezes during
     Tier 2's deep sleep, so a run with `booted` rows gets one offset per sleep
     epoch instead (fit_epochs)
  2. each result, in time order, claims the latest unclaimed onset at or before
     it: within 2 s if the Pi was awake, within --boot-window if it booted (that
     covers firmware stamping an EVT at the event and at the send after boot)
  3. if too few awake results land on an onset, fall back to t_pi plus the
     manifest's clock offset, then to index order, and say which was used
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import random
import statistics
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PRE_S = 0.25            # a result may map a little before its onset (clock error)
AWAKE_WINDOW_S = 2.0
GEN_HEADER = ["t_mac", "event_idx", "image_id", "true_class", "true_class_id",
              "patch_contrast", "duration_ms", "is_target"]
EVENTS_HEADER = ["t_pi", "event_idx", "arduino_t_ms", "peak", "evt_duration_ms",
                 "state_at_evt", "capture_ms", "infer_ms", "latency_ms",
                 "class_id", "class_name", "confidence", "top5", "fired"]


# ---------------------------------------------------------------------- loading

def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def load_run(run_dir: Path) -> tuple[list[dict], list[dict], dict]:
    gen = [{"t": float(r["t_mac"]), "image_id": r["image_id"],
            "true_id": int(r.get("true_class_id") or -1),
            "target": r.get("is_target") == "1",
            "duration_s": float(r.get("duration_ms") or 0) / 1000.0,
            "bait": r["image_id"] == "NONE"}
           for r in read_csv(run_dir / "gen.csv")]
    evts = []
    for r in read_csv(run_dir / "events.csv"):
        first = (r.get("top5") or "").split(";")[0]
        raw = int(first.split(":")[0]) if ":" in first else int(r["class_id"])
        evts.append({"t_pi": float(r["t_pi"]), "ard_s": int(r["arduino_t_ms"]) / 1000.0,
                     "state": r.get("state_at_evt") or "awake",
                     "class_id": int(r["class_id"]), "raw_id": raw,
                     "fired": r.get("fired") == "1"})
    mf = run_dir / "manifest.json"
    return gen, evts, (json.loads(mf.read_text()) if mf.exists() else {})


def load_reference(path: Path) -> dict:
    return {r["image_id"]: {"pred": int(r["pred_id"]), "conf": float(r["confidence"]),
                            "true": int(r["true_class_id"])}
            for r in read_csv(path)}


# ---------------------------------------------------------------------- pairing

def _hits(xs: list[float], onsets: list[float], a: float, b: float) -> list[tuple]:
    """(x, onset) for each x that maps just after an onset -- at most one x per onset.

    One-to-one matters: without it, meaningless timestamps (0, 1, 2 s) all land
    within the window of a single onset, the least-squares slope collapses to 0,
    and everything "matches" that one onset.
    """
    pairs, taken = [], set()
    for x in xs:
        t = a + b * x
        k = bisect.bisect_right(onsets, t + PRE_S) - 1
        if k >= 0 and k not in taken and t - onsets[k] <= AWAKE_WINDOW_S:
            taken.add(k)
            pairs.append((x, onsets[k]))
    return pairs


def _lsq(pairs: list[tuple]) -> tuple[float, float]:
    px, py = zip(*pairs)
    mx, my = statistics.fmean(px), statistics.fmean(py)
    sxx = sum((x - mx) ** 2 for x in px)
    b = sum((x - mx) * (y - my) for x, y in pairs) / sxx if sxx > 0 else 1.0
    return my - b * mx, b


def fit_arduino(evts: list[dict], onsets: list[float]):
    """(a, b, hits, n) with t_mac = a + b * arduino_s, or None if it will not fit.

    Anchored on the first dozen awake results, then grown a doubling at a time.
    Fitting a whole run at once fails on long runs: a resonator 3000 ppm off drifts
    ~30 s over three hours, far enough that an unrefined slope lands late results
    on the NEXT onset, and those wrong pairs then drag the fit off.
    """
    xs = sorted(e["ard_s"] for e in evts if e["state"] != "booted")
    if len(xs) < 3 or not onsets:
        return None
    n = min(len(xs), 12)
    best, a, b = [], 0.0, 1.0
    for x0 in xs[:5]:
        for g in onsets[:n + 10]:
            pairs = _hits(xs[:n], onsets, g - x0, 1.0)
            if len(pairs) > len(best):
                best, a = pairs, g - x0
    while len(best) >= 3:
        a, b = _lsq(best)
        core = [(x, y) for x, y in best if abs(y - (a + b * x)) <= 0.5]
        if 3 <= len(core) < len(best):        # drop pairs the fit calls outliers
            a, b = _lsq(core)
        if n == len(xs):
            break
        n = min(len(xs), 2 * n)
        best = _hits(xs[:n], onsets, a, b)
    if abs(b - 1.0) > 0.02:       # 20000 ppm: no crystal or resonator is that far off
        return None
    best = _hits(xs, onsets, a, b)
    if len(best) < max(3, 0.6 * len(xs)):
        return None
    return a, b, len(best), len(xs)


def fit_epochs(evts: list[dict], onsets: list[float]):
    """(Mac time per result, epochs, hits, n) for a run that halts, or None.

    Tier 2's millis() freezes in deep sleep, so its offset steps at every wake, and
    every wake leaves a `booted` row. Each sleep epoch, in order, gets the offset that
    lands the most of its results on onsets after the previous epoch's last result
    (ties: the earliest). The firmware stamps a held EVT at detection, so the boot's
    own row counts too. An epoch that is only a boot, then halt again, lands on the
    next such onset, which is right unless Tier 1 missed an event while the Pi was
    halted.
    """
    epochs = []
    for i, e in enumerate(evts):
        if e["state"] == "booted" or not epochs:
            epochs.append([])
        epochs[-1].append(i)
    times, lo, hits, n_all = [0.0] * len(evts), 0, 0, 0
    for ep in epochs:
        xs = [evts[i]["ard_s"] for i in ep]
        a, b, best = None, 1.0, []
        # ponytail: 60-onset search window; a longer run of Tier 1 misses breaks it
        for k in range(lo, min(lo + 60, len(onsets))):
            for x0 in xs[:5]:
                pairs = _hits(xs, onsets[lo:], onsets[k] - x0, 1.0)
                if len(pairs) > len(best):
                    best, a = pairs, onsets[k] - x0
        if len(best) >= 3:            # refine for resonator drift in long epochs
            a2, b2 = _lsq(best)
            if abs(b2 - 1.0) <= 0.02:
                a, b = a2, b2
        if a is None:
            a = (onsets[lo] if lo < len(onsets) else float("inf")) - evts[ep[0]]["ard_s"]
        hits, n_all = hits + len(best), n_all + len(xs)
        for i in ep:
            times[i] = a + b * evts[i]["ard_s"]
        lo = bisect.bisect_right(onsets, max(times[i] for i in ep) + PRE_S)
    if hits < 0.6 * n_all:
        return None
    return times, len(epochs), hits, n_all


def _assign(times, states, onsets, boot_window) -> list[int | None]:
    claimed, out = set(), [None] * len(times)
    for i in sorted(range(len(times)), key=lambda i: times[i]):
        window = boot_window if states[i] == "booted" else AWAKE_WINDOW_S
        k = bisect.bisect_right(onsets, times[i] + PRE_S) - 1
        while k >= 0 and times[i] - onsets[k] <= window:
            if k not in claimed:
                claimed.add(k)
                out[i] = k
                break
            k -= 1
    return out


def pair(gen: list[dict], evts: list[dict], manifest: dict, boot_window: float):
    """Gen index for every result (None = matched no stimulus), and how."""
    onsets = [g["t"] for g in gen]
    states = [e["state"] for e in evts]
    if "booted" in states:
        ep = fit_epochs(evts, onsets)
        if ep:
            times, n_ep, hits, n = ep
            return (_assign(times, states, onsets, boot_window),
                    f"arduino_t_ms per sleep epoch ({n_ep} epochs; "
                    f"{hits}/{n} results on an onset)")
    fit = None if "booted" in states else fit_arduino(evts, onsets)
    if fit:
        a, b, hits, n = fit
        return (_assign([a + b * e["ard_s"] for e in evts], states, onsets, boot_window),
                f"arduino_t_ms (clock drift {1e6 * (b - 1):+.0f} ppm; "
                f"{hits}/{n} awake results on an onset)")
    offset = (manifest.get("clock_start") or {}).get("offset_s") or 0.0
    times = [e["t_pi"] - offset for e in evts]
    awake = [t for t, s in zip(times, states) if s != "booted"]
    if awake and len(_hits(awake, onsets, 0.0, 1.0)) >= max(1, 0.6 * len(awake)):
        return _assign(times, states, onsets, boot_window), "t_pi + manifest clock offset"
    real = [i for i, g in enumerate(gen) if not g["bait"]]
    return ([real[i] if i < len(real) else None for i in range(len(evts))],
            "index order -- NO usable timing; results after a miss may be misattributed")


# ---------------------------------------------------------------------- scoring

def _frac(n, d):
    return n / d if d else None


def score(gen, evts, ref, assign, method, args) -> dict:
    real = [i for i, g in enumerate(gen) if not g["bait"]]
    got = {k: evts[j] for j, k in enumerate(assign) if k is not None}
    out = {"method": method, "n_real": len(real), "n_results": len(evts),
           "n_detected": sum(1 for i in real if i in got)}
    out["n_missed"] = out["n_real"] - out["n_detected"]
    out["detection_rate"] = _frac(out["n_detected"], out["n_real"])
    out["n_booted_detected"] = sum(1 for i in real if i in got and got[i]["state"] == "booted")
    out["n_bait_triggers"] = sum(1 for k in assign if k is not None and gen[k]["bait"])
    out["n_spurious"] = assign.count(None)

    # Inferences against an always-on camera over the same stimulus span.
    span = (gen[-1]["t"] + gen[-1]["duration_s"] - gen[0]["t"]) if gen else 0.0
    out["stimulus_span_s"] = span
    out["n_inferences"] = len(evts)
    base = {}
    rates = [(f"always_on_{fps:g}fps", fps) for fps in args.baseline_fps]
    durs = [gen[i]["duration_s"] for i in real if gen[i]["duration_s"] > 0]
    if durs:     # the slowest always-on rate that still cannot miss an event
        rates.append(("always_on_one_frame_per_event_duration", 1.0 / statistics.median(durs)))
    for name, fps in rates:
        n = span * fps
        base[name] = {"fps": fps, "inferences": n, "avoided": n - len(evts),
                      "avoided_frac": _frac(n - len(evts), n)}
    out["baselines"] = base

    tgt = [i for i in real if gen[i]["target"]]
    fired = sum(1 for i in tgt if i in got and got[i]["fired"])
    false_fires = sum(1 for i in real if not gen[i]["target"] and i in got and got[i]["fired"])
    out["target"] = {"n_events": len(tgt), "fired": fired, "recall": _frac(fired, len(tgt)),
                     "false_fires": false_fires,
                     "precision": _frac(fired, fired + false_fires)}

    known = [i for i in real if gen[i]["true_id"] >= 0 and gen[i]["image_id"] in ref]
    if not known:
        out["accuracy"] = None
        out["accuracy_note"] = ("no scorable events: gen.csv has no class indices"
                                if not any(gen[i]["true_id"] >= 0 for i in real)
                                else "no stimulus image appears in the reference CSV")
        return out

    thr = args.conf_threshold
    ceil_ok = {i: ref[gen[i]["image_id"]]["pred"] == gen[i]["true_id"] for i in known}
    ceil_thr = {i: ceil_ok[i] and ref[gen[i]["image_id"]]["conf"] >= thr for i in known}
    det = [i for i in known if i in got]
    cam_thr = sum(got[i]["class_id"] == gen[i]["true_id"] for i in det)
    cam_raw = sum(got[i]["raw_id"] == gen[i]["true_id"] for i in det)
    ceil_det = sum(ceil_ok[i] for i in det)
    ceil_all = sum(ceil_ok.values())
    ceil_missed = ceil_all - ceil_det
    n = len(known)
    out["accuracy"] = {
        "n_scored": n,
        "n_not_in_reference": sum(1 for i in real if gen[i]["true_id"] >= 0) - n,
        "conf_threshold": thr,
        # On the events that produced a result, same images both sides.
        "camera_top1": _frac(cam_thr, len(det)),
        "camera_top1_raw": _frac(cam_raw, len(det)),
        "ceiling_top1_same_images": _frac(ceil_det, len(det)),
        "ceiling_top1_thresholded_same_images": _frac(sum(ceil_thr[i] for i in det), len(det)),
        "capture_loss": _frac(ceil_det - cam_raw, len(det)),
        "threshold_loss": _frac(cam_raw - cam_thr, len(det)),
        # Over every real event: a miss is a wrong answer.
        "end_to_end_top1": cam_thr / n,
        "ceiling_top1_all_events": ceil_all / n,
        "accuracy_lost_total": (ceil_all - cam_thr) / n,
        "lost_to_misses": ceil_missed / n,
        "lost_on_detected": (ceil_det - cam_thr) / n,
    }
    return out


# ---------------------------------------------------------------------- report

def _pct(x) -> str:
    return "  n/a" if x is None else f"{100 * x:5.1f}%"


def report(name: str, o: dict) -> None:
    print(f"\n{name}")
    print(f"  pairing       {o['method']}")
    print(f"  events        {o['n_real']} real: {o['n_detected']} answered, "
          f"{o['n_missed']} missed   detection {_pct(o['detection_rate'])}")
    print(f"                {o['n_booted_detected']} answered after a boot; "
          f"{o['n_bait_triggers']} results on flicker bait; "
          f"{o['n_spurious']} matched no stimulus")
    a = o.get("accuracy")
    if a:
        print(f"  accuracy      camera top-1 {_pct(a['camera_top1'])} "
              f"(raw {_pct(a['camera_top1_raw'])}) vs ceiling "
              f"{_pct(a['ceiling_top1_same_images'])} on the same {o['n_detected']} images")
        print(f"                capture loss {_pct(a['capture_loss'])}, "
              f"confidence-threshold loss {_pct(a['threshold_loss'])}")
        print(f"  end to end    {_pct(a['end_to_end_top1'])} of {a['n_scored']} events right "
              f"vs {_pct(a['ceiling_top1_all_events'])} possible: lost "
              f"{_pct(a['accuracy_lost_total'])} = {_pct(a['lost_to_misses'])} to misses "
              f"+ {_pct(a['lost_on_detected'])} on answered events")
    else:
        print(f"  accuracy      unavailable: {o.get('accuracy_note')}")
    t = o["target"]
    print(f"  target        {t['fired']}/{t['n_events']} fired (recall {_pct(t['recall'])}), "
          f"{t['false_fires']} false fires (precision {_pct(t['precision'])})")
    print(f"  inferences    {o['n_inferences']} run over {o['stimulus_span_s'] / 60:.1f} min")
    for k, v in o["baselines"].items():
        print(f"                vs {k} ({v['fps']:.3g} fps): {v['inferences']:.0f} -> "
              f"{v['avoided']:.0f} avoided ({_pct(v['avoided_frac'])})")


# ---------------------------------------------------------------------- self-test

def self_test(args) -> int:
    """Known-answer checks. Nothing here needs hardware or real runs."""
    rng = random.Random(7)
    ref = load_reference(args.reference) if args.reference.exists() else {}
    if ref:
        src = f"{args.reference} ({len(ref)} images)"
    else:
        for i in range(60):
            true = 100 + i % 20
            ref[f"synthetic_{i:03d}.jpg"] = {"pred": true if rng.random() < 0.65 else true + 1,
                                             "conf": rng.uniform(0.1, 0.99), "true": true}
        src = "a synthetic 60-image reference (data/reference.csv not found)"
    print(f"self-test against {src}")
    images = list(ref.items())
    failures = []

    def check(name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    def build(sel, misses=(), overwrite=None, bait_before=None, spurious_after=None,
              stamp_at_send=True, tpi_shift=0.0, ard_useless=False, tpi_useless=False):
        n, t0, drift = len(sel), 1_788_000_000.0, 1.003
        gaps = [max(3.0, rng.expovariate(1 / 20)) for _ in range(n)]
        if overwrite:
            gaps[overwrite[1]] = 10.0
            if overwrite[1] + 1 < n:
                gaps[overwrite[1] + 1] = 60.0
        if bait_before is not None:
            gaps[bait_before] = max(gaps[bait_before], 12.0)
        if spurious_after is not None and spurious_after + 1 < n:
            gaps[spurious_after + 1] = max(gaps[spurious_after + 1], 15.0)
        gen, onset, gi, t = [], [], [], t0
        for i, (img, r) in enumerate(sel):
            t += gaps[i] + (15.0 if i else 0.0)
            if i == bait_before:
                gen.append({"t": t - 6.0, "image_id": "NONE", "true_id": -1,
                            "target": False, "duration_s": 0.2, "bait": True})
            gi.append(len(gen))
            onset.append(t)
            gen.append({"t": t, "image_id": img, "true_id": r["true"],
                        "target": r["true"] == 955, "duration_s": 15.0, "bait": False})
        res, shift = [], 0.0
        for i, (img, r) in enumerate(sel):
            if i in misses or (overwrite and i == overwrite[0]):
                continue
            state, stamp, tpi = "awake", onset[i] + 0.06, onset[i] + 0.2
            if overwrite and i == overwrite[1]:
                state, shift = "booted", tpi_shift
                stamp = onset[overwrite[0]] + 35.3 if stamp_at_send else onset[i] + 0.06
                tpi = onset[overwrite[0]] + 35.5
            raw = r["pred"]
            res.append((stamp, tpi + shift, state, raw,
                        raw if r["conf"] >= args.conf_threshold else -1, gi[i]))
        if bait_before is not None:
            bt = gen[gi[bait_before] - 1]["t"]
            res.append((bt + 0.1, bt + 0.2, "awake", -1, -1, gi[bait_before] - 1))
        if spurious_after is not None:
            st = onset[spurious_after] + 8.0
            res.append((st, st + 0.1, "awake", -1, -1, None))
        res.sort()
        evts = [{"t_pi": (0.0 if tpi_useless else tpi),
                 "ard_s": (float(j) if ard_useless else (s - t0) * drift + 777.0),
                 "state": st, "raw_id": raw, "class_id": cls, "fired": cls == 955}
                for j, (s, tpi, st, raw, cls, _) in enumerate(res)]
        return gen, evts, [r[5] for r in res]

    manifest = {"clock_start": {"offset_s": 0.0}}

    # A: the camera answers exactly what the reference did, nothing missed. Through
    # the CSV loaders, so the file formats are exercised too.
    gen, evts, _ = build(images)
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        with open(d / "gen.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(GEN_HEADER)
            for k, g in enumerate(gen):
                w.writerow([f"{g['t']:.3f}", k, g["image_id"], "x", g["true_id"], "0.800",
                            int(g["duration_s"] * 1000), int(g["target"])])
        with open(d / "events.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(EVENTS_HEADER)
            for k, e in enumerate(evts):
                w.writerow([f"{e['t_pi']:.3f}", k, int(e["ard_s"] * 1000), 800, 40, e["state"],
                            "12.0", "95.0", "110.0", e["class_id"], "x", "0.500",
                            f"{e['raw_id']}:0.500", int(e["fired"])])
        g2, e2, _ = load_run(d)
    assign, method = pair(g2, e2, manifest, args.boot_window)
    o = score(g2, e2, ref, assign, method, args)
    a = o["accuracy"]
    check("A  pairs on Arduino time despite 3000 ppm drift", method.startswith("arduino"), method)
    check("A  every event answered", o["detection_rate"] == 1.0)
    check("A  camera == reference gives zero capture loss",
          a["capture_loss"] == 0 and a["camera_top1_raw"] == a["ceiling_top1_same_images"],
          f"camera raw {_pct(a['camera_top1_raw'])} == ceiling {_pct(a['ceiling_top1_same_images'])}")

    # B: misses, a boot that swallows one event and forwards the next, flicker bait,
    # a trigger on nothing, and a Pi clock that jumps 100 s after the boot.
    for at_send in (True, False):
        tag = "stamped at send" if at_send else "stamped at event"
        gen, evts, expect = build(images[:40], misses=(12,), overwrite=(5, 6), bait_before=20,
                                  spurious_after=30, stamp_at_send=at_send, tpi_shift=-100.0)
        assign, method = pair(gen, evts, manifest, args.boot_window)
        o = score(gen, evts, ref, assign, method, args)
        check(f"B  every result on the right stimulus ({tag})", assign == expect,
              "" if assign == expect else f"got {assign} want {expect}")
        check(f"B  counts: 2 missed, 1 booted, 1 bait, 1 spurious ({tag})",
              (o["n_missed"], o["n_booted_detected"], o["n_bait_triggers"], o["n_spurious"])
              == (2, 1, 1, 1),
              f"{o['n_missed']}, {o['n_booted_detected']}, {o['n_bait_triggers']}, {o['n_spurious']}")

    # C: Arduino timestamps unusable (the synthetic generator's 0, 1000, 2000 ms).
    gen, evts, expect = build(images[:20], ard_useless=True)
    assign, method = pair(gen, evts, manifest, args.boot_window)
    check("C  falls back to t_pi and still pairs correctly",
          method.startswith("t_pi") and assign == expect, method)

    # D: no timing at all.
    gen, evts, expect = build(images[:20], ard_useless=True, tpi_useless=True)
    assign, method = pair(gen, evts, manifest, args.boot_window)
    check("D  falls back to index order and says so",
          method.startswith("index") and assign == expect, method)

    # E: a halting cell. millis() freezes in deep sleep, so Tier 2's clock loses the
    # whole sleep at every wake; the boot at 17 halts again before 18 (an epoch with
    # no awake result), and 25 is missed. The Pi clock is useless, as after a wake.
    t0 = t = 1_788_000_000.0
    gen, evts, expect, ard_off = [], [], [], 500.0
    for i, (img, r) in enumerate(images[:40]):
        t += rng.uniform(4.0, 30.0)
        booted = i % 7 == 3 or i == 18
        if booted:
            ard_off -= rng.uniform(40.0, 200.0)
        gen.append({"t": t, "image_id": img, "true_id": r["true"],
                    "target": r["true"] == 955, "duration_s": 15.0, "bait": False})
        if i == 25:
            continue
        cls = r["pred"] if r["conf"] >= args.conf_threshold else -1
        evts.append({"t_pi": 0.0, "ard_s": (t + 0.06 - t0) * 1.003 + ard_off,
                     "state": "booted" if booted else "awake", "raw_id": r["pred"],
                     "class_id": cls, "fired": cls == 955})
        expect.append(i)
    assign, method = pair(gen, evts, manifest, args.boot_window)
    check("E  halting cell: every result on the right stimulus, per sleep epoch",
          method.startswith("arduino_t_ms per sleep epoch") and assign == expect,
          method if assign == expect else f"got {assign} want {expect}")

    print("\nPASS: accuracy.py recovers every known answer" if not failures
          else f"\nFAIL: {len(failures)} check(s)")
    return 1 if failures else 0


# ---------------------------------------------------------------------- entry

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", type=Path, help="run directories, or roots to search")
    ap.add_argument("--reference", type=Path, default=REPO / "data" / "reference.csv",
                    help="tools/reference_predict.py output; use data/reference_pi.csv "
                         "if the Pi's runtime disagreed with the Mac's")
    ap.add_argument("--conf-threshold", type=float, default=0.30,
                    help="the daemon's --conf-threshold for these runs")
    ap.add_argument("--boot-window", type=float, default=90.0,
                    help="seconds a booted result may trail its stimulus onset")
    ap.add_argument("--baseline-fps", type=float, nargs="+", default=[1.0],
                    help="always-on classification rates to compare against")
    ap.add_argument("--no-write", action="store_true", help="do not write accuracy.json")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test(args)
    if not args.paths:
        ap.error("give run directories (or data/), or --self-test")

    ref = load_reference(args.reference)
    if not ref:
        print(f"# no reference at {args.reference} -- accuracy will be unavailable; "
              f"run tools/reference_predict.py first", file=sys.stderr)
    runs = []
    for p in args.paths:
        runs += [p] if (p / "gen.csv").exists() else sorted(q.parent for q in p.rglob("gen.csv"))
    if not runs:
        print("no runs found (looked for gen.csv)", file=sys.stderr)
        return 1
    for run in runs:
        gen, evts, manifest = load_run(run)
        if not gen:
            print(f"\n{run.name}: empty gen.csv, skipped")
            continue
        assign, method = pair(gen, evts, manifest, args.boot_window)
        out = score(gen, evts, ref, assign, method, args)
        out["run_id"] = run.name
        report(run.name, out)
        if not args.no_write:
            (run / "accuracy.json").write_text(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
