#!/usr/bin/env python3
"""Turn one run directory into the numbers the write-up quotes.

    analysis/energy_analysis.py data/<run_id>/
    analysis/energy_analysis.py --boot-cycle data/boot/     # measure E_boot alone
    analysis/energy_analysis.py data/*/ --summary           # one row per run

Reads gen.csv, power.csv, events.csv and manifest.json; writes summary.json into
the run directory and prints the same thing.

Alignment, and why most of it is free
-------------------------------------
gen.csv and power.csv are both written on the Mac from the same time.time(), so
stimulus and power share a clock exactly and energy segmentation needs no
reconciliation at all. That is the whole reason power logging lives on the Mac.

Only events.csv is on Pi time, and it is aligned by the CLAPPERBOARD: the daemon
burns every core for 2 s at t=0, which shows up in the power trace as a step of
several watts with a sharp edge. Matching that edge to the daemon's logged
timestamp pins the two clocks to within one sample without trusting NTP. The ssh
offsets in the manifest are a cross-check; when they disagree with the
clapperboard by more than 100 ms, the clapperboard wins.

Event alignment is by INDEX: the Nth GEN maps to the Nth RES. Robust to every
clock problem above. Timestamps are used only to decide which slice of the power
trace belongs to which event.

Finding the power states is the subtle part
--------------------------------------------
Two obvious approaches both fail, and they fail silently on cells of the primary
matrix rather than loudly:

  A two-way split (halted vs awake) is wrong for a run where the Pi never halts.
  There is no halted state to find, so the split lands between idle and inference
  and reports the inference power as P_idle and the idle power as P_halt.

  A power threshold is wrong for finding boots, because the idle plateau that
  follows a boot is above any threshold drawn below the boot -- so the window
  runs on through the whole awake phase, reporting a boot two to three times
  longer and twice as energetic as it really is. And in a dormancy=0 run there is
  no idle plateau at all, so any threshold derived from P_idle collapses instead.

So states are found by DWELL (a state is a level the trace rests at for seconds;
an inference spike lasts ~100 ms and no occupancy threshold can separate the two
once there are enough events), and boots are found by TRANSITION (a boot is the
state entered on leaving halted, and it ends when that state is left in turn).
Verified dormancy -1 rules out automatic halt/boot states even when awake
workloads create multiple sustained levels. The requested setting alone does
not prove that the firmware accepted it.
Both are verified against tools/make_synthetic_run.py, whose ground truth is
known -- see check_synthetic.

Stdlib only, deliberately -- this has to run on a laptop with nothing installed
at 2am on Sep 17.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path


# --------------------------------------------------------------------- input

def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def read_power(path: Path) -> tuple[list[float], list[float], list[float]]:
    """-> (t, watts, joules_cumulative)"""
    t, w, j = [], [], []
    for row in read_csv(path):
        try:
            t.append(float(row["t_mac"]))
            w.append(float(row["power_W"]))
            j.append(float(row["energy_J"]))
        except (KeyError, ValueError):
            continue
    return t, w, j


def integrate(t: list[float], w: list[float], t0: float, t1: float) -> tuple[float, float]:
    """Trapezoidal energy over [t0, t1] -> (joules, seconds actually covered)."""
    e = span = 0.0
    for i in range(1, len(t)):
        a, b = t[i - 1], t[i]
        if b <= t0 or a >= t1 or b <= a:
            continue
        lo, hi = max(a, t0), min(b, t1)
        if hi <= lo:
            continue
        wa = w[i - 1] + (w[i] - w[i - 1]) * ((lo - a) / (b - a))
        wb = w[i - 1] + (w[i] - w[i - 1]) * ((hi - a) / (b - a))
        e += (wa + wb) / 2 * (hi - lo)
        span += hi - lo
    return e, span


# ------------------------------------------------------------- state finding

def smooth(w: list[float], window_s: float = 1.0, rate: float = 100.0
           ) -> list[float]:
    """Boxcar mean over window_s. For STATE DETECTION only, never integration.

    A state is a second-scale plateau, but the per-sample noise on this meter is
    a few hundred mW -- wider than the +-0.25 W band find_levels classifies with.
    Measured idle never stayed inside that band for a whole second, so the idle
    plateau was invisible and P_idle came back n/a on a trace that plainly has
    one. Averaging first collapses the noise by sqrt(k) and leaves the plateaus
    where they are. Energy is always integrated from the raw samples.
    """
    k = max(1, int(round(window_s * rate)))
    if k <= 1 or len(w) < k:
        return list(w)
    out, run = [], sum(w[:k])
    half = k // 2
    for i in range(len(w)):
        if i > half and i + k - half <= len(w):
            run += w[i + k - half - 1] - w[i - half - 1]
        out.append(run / k)
    return out


def find_levels(t: list[float], w: list[float], bin_w: float = 0.05,
                min_occupancy: float = 0.01, min_dwell_s: float = 1.0,
                tol_w: float = 0.25, merge_w: float = 0.3) -> list[dict]:
    """Power *states* present in a trace: [{watts, occupancy, dwell_s}], ascending.

    Histogram peaks, filtered by how long the trace actually DWELLS at each. That
    dwell filter is what distinguishes a state from a transient: an inference
    spike lasts ~100 ms while every real power state lasts seconds, and once a
    run has enough events the spikes accumulate real occupancy, so no occupancy
    threshold alone can reject them.
    """
    if not w:
        return []
    n = len(w)
    bins: dict[int, int] = {}
    for v in w:
        k = int(v / bin_w)
        bins[k] = bins.get(k, 0) + 1

    peaks: list[float] = []
    floor = max(2, int(n * min_occupancy * 0.2))
    for k in sorted(bins, key=lambda k: -bins[k]):
        if bins[k] < floor:
            continue
        centre = (k + 0.5) * bin_w
        if all(abs(centre - p) > merge_w for p in peaks):
            peaks.append(centre)

    levels = []
    for centre in peaks:
        runs, i = [], 0
        while i < n:
            if abs(w[i] - centre) <= tol_w:
                j = i
                while j < n and abs(w[j] - centre) <= tol_w:
                    j += 1
                runs.append((t[min(j, n - 1)] - t[i], i, j))
                i = j
            else:
                i += 1
        # Transit slivers are not visits. Every ramp between two states clips the
        # edge of this band for a few milliseconds, and a noisy ramp clips it
        # several times. Left in the list they outvote the real thing: the halted
        # state measured here had one 98.1 s run and twelve sub-50 ms slivers, so
        # the median dwell came out at 0.02 s and a 98-second plateau was rejected.
        # Drop them first, then judge -- and measure occupancy and mean watts over
        # the dwells alone, so ramp samples do not drag the level off its value.
        sustained = [r for r in runs if r[0] >= min_dwell_s]
        if not sustained:
            continue
        occupancy = sum(j - i for _, i, j in sustained) / n
        dwell = statistics.median([d for d, _, _ in sustained])
        if occupancy >= min_occupancy:
            samples = [w[k] for _, i, j in sustained for k in range(i, j)]
            levels.append({"watts": statistics.fmean(samples),
                           "occupancy": occupancy, "dwell_s": dwell})
    return sorted(levels, key=lambda d: d["watts"])


def find_boot_windows(t: list[float], w: list[float], levels: list[float],
                      min_s: float = 8.0
                      ) -> tuple[list[tuple[float, float]], int | None]:
    """Boot windows, defined by the transition that creates them.

    A boot is the state the trace enters when it LEAVES the halted state, and it
    ends when it leaves that state in turn. Classify every sample to its nearest
    detected level, find the maximal runs at the halted level, and take the
    window from the end of each such run until the level changes again.

    -> (windows, index of the level boots occupy)
    """
    if len(levels) < 2 or not t:
        return [], None
    assign = [min(range(len(levels)), key=lambda i: abs(v - levels[i])) for v in w]
    n = len(assign)

    windows, entered, i = [], [], 0
    while i < n:
        if assign[i] != 0:                      # level 0 is halted
            i += 1
            continue
        j = i
        while j < n and assign[j] == 0:
            j += 1
        if j >= n:
            break
        lvl = assign[j]                         # the level entered on leaving halt
        k = j
        while k < n and assign[k] == lvl:
            k += 1
        if t[min(k, n - 1)] - t[j] >= min_s:
            windows.append((t[j], t[min(k, n - 1)]))
            entered.append(lvl)
        i = max(k, j + 1)

    return windows, (statistics.mode(entered) if entered else None)


def read_ready_uptimes(run_dir: Path, max_boot_s: float = 120.0) -> list[float]:
    """The uptime= values on the daemon's "# ready" lines, fresh boots only.

    The Pi 4 has no RTC and tier3-daemon.service deliberately starts before the
    network, so the daemon's WALL CLOCK at "# ready" is only whatever timesyncd
    restored from disk -- in the measured trace it read about 100 s BEFORE the
    wake that caused it. Its monotonic uptime is sound; its wall clock is not.
    Anything above max_boot_s is a daemon started by hand on an already-running
    Pi, not a wake, so it is not a boot.
    """
    log = run_dir / "daemon.log"
    if not log.exists():
        return []
    ups: list[float] = []
    for line in log.read_text().splitlines():
        if not line.startswith("# ready "):
            continue
        for tok in line.split():
            if tok.startswith("uptime="):
                try:
                    u = float(tok[len("uptime="):])
                except ValueError:
                    break
                if u <= max_boot_s:
                    ups.append(u)
                break
    return ups


def boot_cycle_windows(t: list[float], w: list[float], w_s: list[float],
                       p_halt: float, p_idle: float, p_fw: float | None,
                       uptimes: list[float], halt_tol: float = 0.25,
                       min_halt_s: float = 20.0
                       ) -> list[tuple[float, float, float]]:
    """Boot windows for a dedicated halt->wake trace, bracketed by EDGES.

    find_boot_windows treats a boot as the power STATE entered on leaving halt.
    That holds in synthesis and fails on this hardware: the measured boot is a
    ~2.7 W firmware stage for 9.4 s and then a noisy 3.4-5.1 W kernel and
    userspace phase whose mean (3.45 W) sits 0.19 W from idle (3.26 W) -- far
    inside the noise, so no level separates them and the level-based window
    closes at the end of the firmware stage instead, reporting T_boot = 8.5 s
    against a true 25.6 s.

    So bracket it instead. The window opens where the trace leaves the halted
    floor and closes at kernel_start + the daemon's uptime at "# ready", with
    kernel_start the first sustained crossing above the midpoint between the
    firmware plateau and idle. That midpoint is the one threshold in the boot
    that IS well separated.

    -> [(wake, ready, kernel_start)], one per halted run that has an uptime.
    """
    n = len(w_s)
    if n == 0 or not uptimes:
        return []
    # A trace that opens awake caught a boot whose wake came before logging:
    # its "# ready" has no wake here, and pairing by index would shift every
    # boot onto the previous one's uptime (s18_tboot: T_boot 87 s from 77.5).
    if abs(w_s[0] - p_halt) > halt_tol:
        uptimes = uptimes[1:]

    wakes: list[float] = []
    i = 0
    while i < n:
        if abs(w_s[i] - p_halt) <= halt_tol:
            j = i
            while j < n and abs(w_s[j] - p_halt) <= halt_tol:
                j += 1
            if t[min(j, n - 1)] - t[i] >= min_halt_s and j < n:
                wakes.append(t[j])
            i = j
        else:
            i += 1

    out: list[tuple[float, float, float]] = []
    for idx, wake in enumerate(wakes):
        if idx >= len(uptimes):
            break
        # No firmware level from find_levels (a busy trace splits the plateau
        # across several levels): read it off 1-4 s after this wake instead.
        # (halt+idle)/2 is NOT a fallback -- measured, it sits on the ~2.65 W
        # plateau and fires on a blip 4 s into the 9 s firmware stage.
        fw = p_fw
        if fw is None:
            plateau = [w_s[k] for k in range(n) if wake + 1.0 <= t[k] <= wake + 4.0]
            fw = statistics.median(plateau) if plateau else p_halt
        thresh = (fw + p_idle) / 2
        kstart = None
        for k in range(n):
            if t[k] <= wake or w[k] <= thresh:
                continue
            m = k
            while m < n and t[m] - t[k] < 1.0:
                m += 1
            if m > k and statistics.fmean(w[k:m]) > thresh:
                kstart = t[k]
                break
        if kstart is None:
            continue
        out.append((wake, kstart + uptimes[idx], kstart))
    return out


def find_clapperboard(t: list[float], w: list[float], duration: float = 2.0,
                      min_step_w: float = 0.8, near: float | None = None,
                      tol_s: float = 2.0) -> float | None:
    """Start time of the first sustained step of >= min_step_w lasting ~duration.

    near: where the ssh clock offset puts it. A trace that opens below idle
    (harness, 2.4 W) makes plain idle a "step", and a 1.4 s idle stretch then
    passes for the burn 3 s early; only plateaus within tol_s of near count.
    """
    if len(t) < 20:
        return None
    # With near, the level just before it: the burn rides on idle, and against
    # the trace-start level it never ends.
    pre = [y for x, y in zip(t, w) if near is not None and near - 5 <= x <= near - 0.5]
    baseline = statistics.median(pre or w[:min(len(w), 200)])
    threshold = baseline + min_step_w
    i, n = 0, len(t)
    while i < n:
        if w[i] < threshold:
            i += 1
            continue
        j = i
        while j < n and w[j] >= threshold:
            j += 1
        held = t[min(j, n - 1)] - t[i]
        # Accept a plateau roughly as long as the burn: inference spikes are far
        # shorter, and boot ramps are far longer.
        if 0.6 * duration <= held <= 2.0 * duration and \
                (near is None or abs(t[i] - near) <= tol_s):
            return t[i]
        i = j + 1
    return None


# ------------------------------------------------------------------ analysis

def analyse_run(run_dir: Path, args) -> dict:
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    gen = read_csv(run_dir / "gen.csv")
    events = read_csv(run_dir / "events.csv")
    t, w, j = read_power(run_dir / "power.csv")

    out: dict = {"run_id": run_dir.name, "params": manifest.get("params", {})}
    real_gen = [g for g in gen if g.get("image_id") != "NONE"]
    out["n_gen"] = len(real_gen)
    out["n_flicker"] = len(gen) - len(real_gen)
    out["n_res"] = len(events)

    # ------------------------------------------------------------ detection
    if real_gen:
        out["detection_rate"] = len(events) / len(real_gen)
        out["n_missed"] = len(real_gen) - len(events)
    out["n_booted_events"] = sum(1 for e in events
                                 if e.get("state_at_evt") == "booted")
    out["n_classified"] = sum(1 for e in events if int(e.get("class_id", -1)) >= 0)
    out["n_fired"] = sum(int(e.get("fired", 0)) for e in events)

    lat = sorted(float(e["latency_ms"]) for e in events if e.get("latency_ms"))
    if lat:
        out["latency_ms"] = {"p50": lat[len(lat) // 2],
                             "p95": lat[int(len(lat) * .95)], "max": lat[-1],
                             "mean": statistics.fmean(lat)}
    inf = [float(e["infer_ms"]) for e in events if e.get("infer_ms")]
    if inf:
        out["infer_ms_p50"] = statistics.median(inf)

    # --------------------------------------------------------------- power
    if not t:
        out["warning"] = "no power.csv -- energy numbers unavailable"
        return out

    duration = t[-1] - t[0]
    rate = len(t) / duration if duration else 0.0
    out["power_samples"] = len(t)
    out["duration_s"] = duration
    out["sample_rate_hz"] = rate
    if not 80 <= rate <= 120:
        out["warning"] = (f"{rate:.1f} samples/s, expected ~100 -- dropped USB "
                          f"reports make energy integration unreliable")

    # Two independent routes to the same number. If they disagree, one is wrong.
    e_trapz, _ = integrate(t, w, t[0], t[-1])
    e_counter = j[-1] - j[0]
    out["energy_J_trapezoid"] = e_trapz
    out["energy_J_meter_counter"] = e_counter
    out["energy_cross_check_pct"] = (100 * abs(e_trapz - e_counter) / e_counter
                                     if e_counter else float("nan"))
    out["avg_power_W"] = e_trapz / duration if duration else float("nan")

    # ---------------------------------------------------------- the states
    # States are found on the smoothed trace, energy always on the raw one.
    w_s = smooth(w, args.smooth_s, rate if 1 < rate < 1e6 else 100.0)
    levels = find_levels(t, w_s, min_dwell_s=args.min_dwell_s)
    out["levels_W"] = [{k: round(v, 4) for k, v in lv.items()} for lv in levels]
    watts = [lv["watts"] for lv in levels]

    # Windows come off the RAW trace: smoothing ramps the halt->boot edge through
    # the idle level, so the level "entered on leaving halt" reads as idle for a
    # few hundred ms and the window is lost.
    never_halt = manifest.get("dormancy_ms_verified") == -1
    boots, boot_level = ([], None) if never_halt else find_boot_windows(
        t, w, watts, args.min_boot_s)
    p_halt = watts[0] if len(watts) >= 2 and not never_halt else None
    p_boot_level = watts[boot_level] if boot_level is not None else None

    # Idle is whatever stable state is left once halted and boot are named. In a
    # dormancy=0 run there is none, and saying so is the honest answer.
    others = [lv for i, lv in enumerate(levels)
              if not (p_halt is not None and i == 0) and i != boot_level]
    p_idle = max(others, key=lambda d: d["occupancy"])["watts"] if others else None

    out["halted_state_present"] = p_halt is not None
    out["idle_state_present"] = p_idle is not None
    out["p_halt_est_W"] = p_halt
    out["p_idle_est_W"] = p_idle
    out["p_boot_level_W"] = p_boot_level
    notes = []
    if never_halt:
        notes.append("dormancy -1 verified -- awake power variations are not "
                     "halted or boot states")
    elif p_halt is None:
        notes.append("no halted state -- the Pi never halted in this run")
    if p_idle is None:
        notes.append("no idle plateau -- the Pi halted immediately after every "
                     "event, so this trace contains no P_idle to measure")
    if notes:
        out["note_levels"] = "; ".join(notes)

    split = (None if p_halt is None else
             (p_halt + p_idle) / 2 if p_idle is not None else (min(w) + max(w)) / 2)
    out["state_split_W"] = split
    out["frac_time_low_state"] = (sum(1 for v in w if v <= split) / len(w)
                                  if split is not None else None)

    # ---------------------------------------------------------- clapperboard
    t_pi = None
    log = run_dir / "daemon.log"
    if log.exists():
        for line in log.read_text().splitlines():
            if line.startswith("# clapperboard "):
                try:
                    t_pi = float(line.split()[2])
                except (IndexError, ValueError):
                    pass
                break
    cs = manifest.get("clock_start") or {}
    # the manifest offset is (Pi - Mac); the clapperboard gives (Mac - Pi)
    near = t_pi - cs["offset_s"] if (t_pi is not None
                                     and cs.get("offset_s") is not None) else None
    clap = find_clapperboard(t, w, args.clapperboard, near=near)
    out["clapperboard_t_mac"] = clap
    if clap is None:
        out["warning_clap"] = ("no clapperboard step found -- events.csv cannot be "
                               "placed on the power trace; check the daemon ran and "
                               "that the logger started first")
    elif t_pi is not None:
        out["pi_to_mac_offset_s"] = clap - t_pi
        if near is not None:
            disagree = abs(clap - near)
            out["clock_disagreement_s"] = disagree
            if disagree > 0.1:
                out["warning_clock"] = (
                    f"ssh offset and clapperboard disagree by "
                    f"{disagree:.3f} s -- trusting the clapperboard")

    # -------------------------------------------------- per-event energetics
    per_event = []
    for i, g in enumerate(real_gen):
        try:
            t0 = float(g["t_mac"])
            dur = float(g["duration_ms"]) / 1000.0
        except (KeyError, ValueError):
            continue
        t1 = float(real_gen[i + 1]["t_mac"]) if i + 1 < len(real_gen) else t[-1]
        e_win, span = integrate(t, w, t0, min(t0 + dur, t1))
        if span > 0:
            per_event.append({"energy_J": e_win, "span_s": span})
    if per_event:
        energies = [p["energy_J"] for p in per_event]
        out["energy_per_event_J"] = {
            "n": len(energies), "mean": statistics.fmean(energies),
            "median": statistics.median(energies),
            "min": min(energies), "max": max(energies)}
        # Net of idle is the marginal cost of one more event; wall is what the
        # whole box costs while working. Report both and say which is which.
        if p_idle is not None:
            out["energy_per_event_net_of_idle_J"] = statistics.fmean(
                [p["energy_J"] - p_idle * p["span_s"] for p in per_event])

    # ------------------------------------------------------- boot energetics
    out["boot_method"] = "level"
    if getattr(args, "boot_cycle", False) and p_halt is not None \
            and p_idle is not None:
        edges = boot_cycle_windows(t, w, w_s, p_halt, p_idle, p_boot_level,
                                   read_ready_uptimes(run_dir))
        if edges:
            boots = [(a, b) for a, b, _ in edges]
            out["boot_method"] = "edge"
            fw = [k - a for a, _, k in edges]
            out["firmware_stage_s"] = {"mean": statistics.fmean(fw),
                                       "min": min(fw), "max": max(fw)}
    out["n_boot_windows"] = len(boots)
    # The windows themselves, so plots.py shades what was integrated, not a re-guess.
    out["boot_windows_t"] = [[a, b] for a, b in boots]
    e_boots, t_boots, p_boots, net_boots = [], [], [], []
    for b0, b1 in boots:
        e, span = integrate(t, w, b0, b1)
        if span > 0:
            e_boots.append(e)
            t_boots.append(span)
            p_boots.append(e / span)
            if p_halt is not None:
                # Net of the halted floor: the floor would have been paid anyway
                # over this interval, so this is the marginal cost of the boot and
                # the numerator of the dormancy break-even.
                net_boots.append(e - p_halt * span)
    if net_boots:
        out["E_boot_net_of_halt_J"] = {"mean": statistics.fmean(net_boots),
                                       "min": min(net_boots), "max": max(net_boots)}
    if e_boots:
        out["E_boot_J"] = {"n": len(e_boots), "mean": statistics.fmean(e_boots),
                           "median": statistics.median(e_boots),
                           "min": min(e_boots), "max": max(e_boots)}
        out["T_boot_s"] = {"mean": statistics.fmean(t_boots),
                           "median": statistics.median(t_boots),
                           "min": min(t_boots), "max": max(t_boots)}
        out["P_boot_W"] = statistics.fmean(p_boots)
    return out


# -------------------------------------------------------------------- output

def report(out: dict) -> None:
    p = out.get("params", {})
    print(f"\n=== {out['run_id']} ===")
    if p:
        print(f"  interval {p.get('mean_interval')}s  duration {p.get('duration_ms')}ms"
              f"  dormancy {p.get('dormancy_ms')}ms  contrast {p.get('contrast')}"
              f"  model {p.get('model')}")

    print(f"\n  events   {out.get('n_gen', 0)} generated"
          f" ({out.get('n_flicker', 0)} flicker bait), {out.get('n_res', 0)} results")
    if "detection_rate" in out:
        print(f"  DETECTION RATE          {out['detection_rate']:.3f}"
              f"   ({out['n_missed']} missed)")
    print(f"  classified {out.get('n_classified', 0)}, "
          f"fired on target {out.get('n_fired', 0)}, "
          f"booted-state events {out.get('n_booted_events', 0)}")
    if "latency_ms" in out:
        l = out["latency_ms"]
        print(f"  latency ms  p50 {l['p50']:.0f}  p95 {l['p95']:.0f}  max {l['max']:.0f}")

    if "avg_power_W" not in out:
        print(f"\n  {out.get('warning', 'no power data')}")
        return

    print(f"\n  power    {out['power_samples']} samples over {out['duration_s']:.1f}s"
          f" @ {out['sample_rate_hz']:.1f} Hz")
    print(f"  AVERAGE POWER           {out['avg_power_W']:.3f} W")
    print(f"  total energy            {out['energy_J_trapezoid']:.1f} J"
          f"   (meter counter {out['energy_J_meter_counter']:.1f} J, "
          f"{out['energy_cross_check_pct']:.2f}% apart)")
    if out["energy_cross_check_pct"] > 2.0:
        print("  ^ the two routes to energy disagree by >2%. One is wrong; find out"
              "\n    which before quoting either.")

    print("  power states: " + (", ".join(
        f"{lv['watts']:.2f} W ({100 * lv['occupancy']:.0f}%, dwell {lv['dwell_s']:.1f}s)"
        for lv in out.get("levels_W", [])) or "none"))

    def lvl(x):
        return "n/a" if x is None else f"{x:.3f} W"
    low = out.get("frac_time_low_state")
    print(f"  P_halt {lvl(out.get('p_halt_est_W')):<10} "
          f"P_idle {lvl(out.get('p_idle_est_W')):<10} "
          f"boot level {lvl(out.get('p_boot_level_W')):<10}"
          + (f" ({100 * low:.0f}% low)" if low is not None else ""))
    if out.get("note_levels"):
        print(f"  note: {out['note_levels']}")

    if "energy_per_event_J" in out:
        e = out["energy_per_event_J"]
        print(f"\n  energy per event        {e['mean']:.2f} J wall"
              f"   (median {e['median']:.2f}, n={e['n']})")
        if "energy_per_event_net_of_idle_J" in out:
            print(f"  net of idle             "
                  f"{out['energy_per_event_net_of_idle_J']:.2f} J"
                  f"   <- the marginal cost of one more event")
        else:
            print("  net of idle             n/a (no idle plateau in this run)")

    if "E_boot_J" in out:
        e, tb = out["E_boot_J"], out["T_boot_s"]
        if out.get("boot_method") == "edge":
            fw = out.get("firmware_stage_s") or {}
            net = out.get("E_boot_net_of_halt_J") or {}
            print(f"\n  BOOT  (edge-bracketed: halt exit -> kernel step + daemon "
                  f"uptime at '# ready')")
            if fw:
                print(f"        firmware stage {fw['mean']:.2f} s before the kernel "
                      f"starts counting uptime")
            if net:
                print(f"        E_boot net of halt {net['mean']:.1f} J "
                      f"<- the marginal cost of a boot")
        print(f"\n  BOOT  n={e['n']}  E_boot {e['mean']:.1f} J"
              f" (median {e['median']:.1f}, {e['min']:.1f}-{e['max']:.1f})")
        print(f"        T_boot {tb['mean']:.1f} s "
              f"({tb['min']:.1f}-{tb['max']:.1f})   P_boot {out['P_boot_W']:.2f} W")
        if e["n"] < out.get("n_booted_events", 0):
            print(f"        NOTE: only {e['n']} boot windows found but the daemon "
                  f"logged {out['n_booted_events']} booted events -- the detector is "
                  f"missing boots. Lower --min-boot-s.")
    elif out.get("n_boot_windows") == 0:
        print("\n  no boot windows detected (dormancy never fired, or --min-boot-s "
              "is too high)")

    for key in ("warning", "warning_clap", "warning_clock"):
        if key in out:
            print(f"\n  WARNING: {out[key]}")
    if out.get("clapperboard_t_mac"):
        print(f"\n  clapperboard at t={out['clapperboard_t_mac']:.3f}"
              + (f", Pi->Mac offset {out['pi_to_mac_offset_s']:+.3f}s"
                 if "pi_to_mac_offset_s" in out else ""))


def check_synthetic(out: dict, truth: dict) -> bool:
    """Round-trip check against a synthetic run whose truth is known.

    If the analysis cannot recover constants from a trace where they ARE known,
    it will not recover them from a real one. This is the regression test, and it
    is what caught both of the state-finding failures described at the top.
    """
    checks = [
        ("P_halt",   out.get("p_halt_est_W"),                     truth.get("p_halt"), 0.06),
        ("P_idle",   out.get("p_idle_est_W"),                     truth.get("p_idle"), 0.06),
        ("P_boot",   out.get("P_boot_W"),                         truth.get("p_boot"), 0.20),
        ("T_boot",   (out.get("T_boot_s") or {}).get("mean"),     truth.get("t_boot"), 1.5),
        ("E_boot",   (out.get("E_boot_J") or {}).get("mean"),     truth.get("e_boot"), 6.0),
        ("n_boots",  out.get("n_boot_windows"),                   truth.get("n_boots"), 0.5),
        ("detected", out.get("n_res"),                            truth.get("n_detected"), 0.5),
    ]
    # A run that never halts contains no halted state and no boots; a dormancy=0
    # run contains no idle plateau. Those constants are genuinely unmeasurable
    # from such a trace and their absence is the correct answer, not a failure.
    absent = set()
    if out.get("halted_state_present") is False:
        absent |= {"P_halt"}
    if out.get("idle_state_present") is False:
        absent |= {"P_idle"}
    if not truth.get("n_boots"):
        absent |= {"P_boot", "T_boot", "E_boot"}

    print("\n  round-trip against known ground truth:")
    ok = True
    for name, got, want, tol in checks:
        if name in absent:
            print(f"    {name:<9} n/a -- this trace does not contain that state")
            continue
        if got is None or want is None:
            print(f"    {name:<9} unavailable")
            ok = False
            continue
        good = abs(got - want) <= tol
        ok &= good
        print(f"    {name:<9} got {got:9.3f}  truth {want:9.3f}  "
              f"{'ok' if good else 'FAIL (tol %.2f)' % tol}")
    print(f"  {'PASS' if ok else 'FAIL'}: analysis "
          f"{'recovers' if ok else 'does NOT recover'} the known constants")
    return ok


def summary_row(out: dict) -> str:
    p = out.get("params", {})
    return (f"{out['run_id'][:26]:<28} {str(p.get('mean_interval', '')):>8} "
            f"{str(p.get('dormancy_ms', '')):>9} "
            f"{out.get('detection_rate', float('nan')):>7.3f} "
            f"{out.get('avg_power_W', float('nan')):>8.3f} "
            f"{out.get('n_gen', 0):>5} {out.get('n_res', 0):>5}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+", type=Path, help="run directories")
    ap.add_argument("--summary", action="store_true", help="one line per run")
    ap.add_argument("--boot-cycle", action="store_true",
                    help="a dedicated halt->wake trace; report E_boot and exit")
    ap.add_argument("--clapperboard", type=float, default=2.0)
    ap.add_argument("--min-boot-s", type=float, default=8.0,
                    help="shortest excursion counted as a boot rather than a spike")
    ap.add_argument("--smooth-s", type=float, default=1.0,
                    help="boxcar width for state detection only; 0 disables")
    ap.add_argument("--min-dwell-s", type=float, default=1.0,
                    help="shortest dwell that counts as a power state, not a spike")
    ap.add_argument("--no-write", action="store_true", help="do not write summary.json")
    args = ap.parse_args()

    dirs = [d for d in args.runs if d.is_dir()]
    if not dirs:
        sys.exit("no run directories found")

    if args.summary:
        print(f"{'run':<28} {'interval':>8} {'dormancy':>9} {'detect':>7} "
              f"{'avg_W':>8} {'gen':>5} {'res':>5}")
    rows, failures = [], 0
    for d in sorted(dirs):
        out = analyse_run(d, args)
        rows.append(out)
        if args.summary:
            print(summary_row(out))
        else:
            report(out)
            mf = d / "manifest.json"
            truth = (json.loads(mf.read_text()).get("ground_truth")
                     if mf.exists() else None)
            if truth and not check_synthetic(out, truth):
                failures += 1
        if not args.no_write:
            (d / "summary.json").write_text(json.dumps(out, indent=2, default=str))

    if args.boot_cycle:
        boots = [r["E_boot_J"]["mean"] for r in rows if "E_boot_J" in r]
        if boots:
            print(f"\nE_boot across {len(boots)} run(s): "
                  f"{statistics.fmean(boots):.1f} J")
            print("Put this into data/constants.json and re-run")
            print("  analysis/power_model.py --measured data/constants.json")
        else:
            print("\nno boot windows found -- was the Pi actually halted first?")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
