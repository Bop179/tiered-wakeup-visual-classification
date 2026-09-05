#!/usr/bin/env python3
"""Analytic power/detection model for the tiered wake-up cascade.

Committed BEFORE the experiment matrix runs, so the prediction is on record. If
the measurement disagrees, the disagreement is the finding.

The system
----------
Events arrive as a Poisson process of rate lambda. The Pi is awake or halted. An
event arriving while awake is always caught (~100 ms). An event arriving while
halted triggers a full boot -- T_boot at P_boot, camera blind throughout -- so it
is caught only if the stimulus outlives the boot. Events arriving *during* a boot
are lost outright (Tier 2 buffers only one pending event). After the last handled
event the Pi stays awake for the dormancy timeout T_d, then halts.

Exact solution, by renewal-reward
----------------------------------
Regenerate at each halt. One cycle is: wait Exp(lambda) halted, boot for T_boot,
then an awake phase that ends the first time a gap exceeds T_d. With

    W = exp(lambda*T_d) - 1          expected events handled while awake, per cycle
    B = lambda*T_boot                arrivals lost inside one boot
    s = 1 if the stimulus outlives a boot else 0

the awake phase lasts exactly W/lambda, and

    cycle     = (1 + W)/lambda + T_boot
    energy    = P_halt/lambda + P_boot*T_boot + P_idle*W/lambda + E_infer*(s + W)
    arrivals  = 1 + W + B
    detect    = (s + W) / (1 + W + B)
    P_avg     = energy / cycle

Three predictions, all falsifiable
-----------------------------------
1. BREAK-EVEN mean inter-event interval, where halting stops paying:

       1/lambda*  =  T_boot * (P_boot - P_idle) / (P_idle - P_halt)

   Note (P_boot - P_idle), not (P_boot - P_halt). A boot displaces time the Pi
   would have spent *idle*, not halted, because the events it swallows would
   have kept it awake anyway. At the estimates this is ~15 s, against ~45 s from
   the naive form and ~52 s from the first-order E_boot/(P_idle-P_halt) figure.
   See --naive.

2. THE OPTIMUM IS BANG-BANG. Both P_avg and detect are linear-fractional
   (Moebius) in W, hence monotone in W, hence monotone in T_d. The power-optimal
   dormancy timeout is therefore 0 or infinity -- never an interior value.
   Intermediate timeouts are not a power optimisation; they are bought detection.

3. THE PARETO FRONT IS EXACTLY A STRAIGHT LINE. Eliminating W between the two
   Moebius forms, the denominator cancels identically, leaving P affine in detect:

       dP/d(detect)  =  K  =  [ (P_idle - P_halt)/lambda - T_boot*(P_boot - P_idle) ]
                              / [ (1 - s)/lambda + T_boot ]

   constant in T_d. So average power against detection rate should plot as a
   straight line whose slope is fixed by the four measured constants and the
   event rate, and whose sign flips at the break-even above. A measured front
   that *curves* falsifies one of the assumptions -- and which one is the result.

Why the naive form is wrong (--naive)
--------------------------------------
The obvious renewal argument charges a boot to every event that finds the Pi
halted, at rate lambda*exp(-lambda*T_d). That silently assumes boots are rare
enough not to overlap arrivals. When lambda*T_boot is not small the Pi spends its
life booting, each boot swallows the events behind it, and the naive form
overestimates average power by more than 60% at a 15 s mean interval. It is the
sparse-event limit of the exact model, valid only for lambda*T_boot << 1.

--monte-carlo checks the closed form against a discrete-event simulation of the
same system. That check is what caught the naive form; keep running it.

Usage
-----
    analysis/power_model.py                          # predictions at estimates
    analysis/power_model.py --measured data/constants.json
    analysis/power_model.py --pareto -o pareto.csv   # front for overlay on data
    analysis/power_model.py --monte-carlo            # closed form vs simulation
    analysis/power_model.py --naive                  # the wrong model, for contrast
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from dataclasses import asdict, dataclass

# Pre-measurement estimates. Replaced by Sep 7 measurements via --measured.
# Every one of these is a guess until docs/EXPERIMENTS.md section 0 is filled in.
ESTIMATES = dict(
    p_idle=2.5,        # W, daemon idle, camera initialised
    p_halt=0.5,        # W, after `sudo halt` with WAKE_ON_GPIO=1
    p_boot=3.5,        # W, mean over the boot window
    t_boot=30.0,       # s, wake asserted -> daemon prints "# ready"
    e_infer=0.25,      # J per inference, net of idle
    latency_awake=0.1, # s, EVT received -> result, Pi already awake
)

# exp(LOG_CAP) is where a float64 stops being useful; beyond it T_d is infinite
# for every practical purpose.
LOG_CAP = 500.0


@dataclass(frozen=True)
class Params:
    p_idle: float
    p_halt: float
    p_boot: float
    t_boot: float
    e_infer: float
    latency_awake: float

    @property
    def e_boot(self) -> float:
        """Energy of one boot, J. The single most important measured number."""
        return self.p_boot * self.t_boot

    def survives_boot(self, duration_s: float) -> int:
        """s: is the stimulus still on screen when the daemon becomes ready?"""
        return 1 if duration_s >= self.t_boot + self.latency_awake else 0


def load_params(path: str | None) -> Params:
    values = dict(ESTIMATES)
    if path:
        with open(path) as fh:
            measured = json.load(fh)
        unknown = set(measured) - set(ESTIMATES)
        if unknown:
            sys.exit(f"unknown constants in {path}: {sorted(unknown)}")
        values.update(measured)
    return Params(**values)


# ---------------------------------------------------------- exact closed form


def _w(lam: float, t_d: float) -> float:
    """W = exp(lambda*T_d) - 1, the expected events handled per awake phase."""
    if t_d == math.inf or lam * t_d > LOG_CAP:
        return math.inf
    return math.expm1(lam * t_d)


def avg_power(p: Params, lam: float, t_d: float, duration_s: float) -> float:
    """Average system power, W. t_d may be math.inf (never halt)."""
    w = _w(lam, t_d)
    if w == math.inf:
        return p.p_idle + lam * p.e_infer
    s = p.survives_boot(duration_s)
    cycle = (1.0 + w) / lam + p.t_boot
    energy = (p.p_halt / lam + p.p_boot * p.t_boot
              + p.p_idle * w / lam + p.e_infer * (s + w))
    return energy / cycle


def detection_rate(p: Params, lam: float, t_d: float, duration_s: float) -> float:
    """Fraction of events classified, 0..1."""
    w = _w(lam, t_d)
    if w == math.inf:
        return 1.0
    s = p.survives_boot(duration_s)
    return (s + w) / (1.0 + w + lam * p.t_boot)


def marginal_slope(p: Params, lam: float, duration_s: float) -> float:
    """K = dP/d(detect), constant in T_d -- prediction 3.

    Positive: longer dormancy costs power, so halting is the cheap policy.
    Negative: halting is a net loss and always-awake wins.
    """
    s = p.survives_boot(duration_s)
    num = ((p.p_idle - p.p_halt) / lam - p.t_boot * (p.p_boot - p.p_idle)
           + p.e_infer * lam * ((1.0 / lam + p.t_boot) - s / lam) * 0.0)
    den = (1.0 - s) / lam + p.t_boot
    return num / den


def breakeven_interval(p: Params) -> float:
    """Mean inter-event interval at which halting stops paying, s (exact model)."""
    denom = p.p_idle - p.p_halt
    if denom <= 0:
        return math.inf
    return p.t_boot * (p.p_boot - p.p_idle) / denom


def breakeven_naive(p: Params) -> float:
    """The naive renewal figure, T_boot*(P_boot-P_halt)/(P_idle-P_halt)."""
    denom = p.p_idle - p.p_halt
    return math.inf if denom <= 0 else p.t_boot * (p.p_boot - p.p_halt) / denom


def breakeven_first_order(p: Params) -> float:
    """The back-of-envelope E_boot/(P_idle - P_halt) figure."""
    denom = p.p_idle - p.p_halt
    return math.inf if denom <= 0 else p.e_boot / denom


def avg_power_naive(p: Params, lam: float, t_d: float, duration_s: float) -> float:
    """The WRONG model, kept for contrast. Sparse-event limit, lambda*T_boot << 1."""
    e = 0.0 if t_d == math.inf else math.exp(-lam * t_d)
    detect = (1.0 - e) + e * p.survives_boot(duration_s)
    return (p.p_idle * (1.0 - e) + p.p_halt * e
            + lam * e * (p.p_boot - p.p_halt) * p.t_boot
            + lam * p.e_infer * detect)


# --------------------------------------------------------------- monte carlo


def simulate(p: Params, lam: float, t_d: float, duration_s: float,
             n_events: int = 20000, seed: int = 0) -> dict:
    """Discrete-event simulation -- the reference the closed form is checked against."""
    rng = random.Random(seed)
    energy = 0.0
    t_cursor = 0.0          # everything before this is already integrated
    halt_at = t_d           # scheduled halt; inf means never
    state = "awake"
    detected = missed_blind = missed_boot = boots = 0

    t_event = 0.0
    for _ in range(n_events):
        t_event += rng.expovariate(lam)

        # An event during a boot is lost: nothing is listening, and Tier 2
        # buffers only one pending event.
        if t_event < t_cursor:
            missed_blind += 1
            continue

        if state == "awake" and halt_at <= t_event:
            energy += p.p_idle * (halt_at - t_cursor)
            t_cursor = halt_at
            state = "halted"

        if state == "awake":
            energy += p.p_idle * (t_event - t_cursor)
            t_cursor = t_event
            energy += p.e_infer
            detected += 1
            halt_at = t_event + t_d
        else:
            energy += p.p_halt * (t_event - t_cursor)
            energy += p.p_boot * p.t_boot
            t_cursor = t_event + p.t_boot
            boots += 1
            state = "awake"
            if p.survives_boot(duration_s):
                energy += p.e_infer
                detected += 1
            else:
                missed_boot += 1
            halt_at = t_cursor + t_d

    # Integrate the tail so energy and horizon cover the same window.
    horizon = t_event
    if state == "awake":
        if halt_at < horizon:
            energy += p.p_idle * (halt_at - t_cursor) + p.p_halt * (horizon - halt_at)
        else:
            energy += p.p_idle * (horizon - t_cursor)
    else:
        energy += p.p_halt * (horizon - t_cursor)

    return {
        "avg_power_W": energy / horizon,
        "detection_rate": detected / n_events,
        "events": n_events,
        "detected": detected,
        "missed_during_boot": missed_boot,
        "missed_blind_arrival": missed_blind,
        "boots": boots,
        "horizon_s": horizon,
    }


# --------------------------------------------------------------------- output

# Defaults for the prediction tables; override with --dormancies / --intervals
# once the Sep 7 measurements move the break-even.
DORMANCIES = [0.0, 5.0, 15.0, 30.0, 60.0, math.inf]
INTERVALS = [15.0, 45.0, 120.0]


def fmt(t: float) -> str:
    return "never" if t == math.inf else f"{t:g}"


def print_predictions(p: Params, duration_s: float, naive: bool) -> None:
    power = avg_power_naive if naive else avg_power
    if naive:
        print("### --naive: the SPARSE-EVENT model, wrong when lambda*T_boot is not small\n")

    print("Constants")
    for k, v in asdict(p).items():
        print(f"  {k:<14} {v:g}")
    print(f"  {'E_boot':<14} {p.e_boot:g} J   (derived)")
    print()

    print("Prediction 1 -- break-even mean inter-event interval")
    print(f"  exact       T_boot*(P_boot-P_idle)/(P_idle-P_halt) = "
          f"{breakeven_interval(p):.1f} s")
    print(f"  naive       T_boot*(P_boot-P_halt)/(P_idle-P_halt) = "
          f"{breakeven_naive(p):.1f} s")
    print(f"  first order E_boot/(P_idle-P_halt)                 = "
          f"{breakeven_first_order(p):.1f} s")
    print("  Events rarer than the exact figure: halting wins. Denser: stay awake.")
    print()

    s = p.survives_boot(duration_s)
    print("Prediction 2/3 -- Pareto slope K = dP/d(detect), constant in T_d")
    for iv in INTERVALS:
        k = marginal_slope(p, 1.0 / iv, duration_s)
        verdict = ("break-even" if abs(k) < 1e-9 else
                   "halting wins" if k > 0 else "always-awake wins")
        print(f"  mean interval {iv:6.0f} s   K = {k:+7.3f} W per unit detection"
              f"   {verdict}")
    print()

    print(f"Grid at event duration {duration_s:g} s "
          f"({'survives' if s else 'does NOT survive'} a {p.t_boot:g} s boot)")
    if s:
        print("  NOTE: at this duration every event that is not swallowed by a boot")
        print("  is caught, so the tradeoff is weak. Pick a duration below T_boot")
        print("  for the primary matrix -- around 0.5*T_boot.")
    print()
    header = f"  {'interval':>9} " + "".join(f"{fmt(t):>12}" for t in DORMANCIES)
    print(f"{'AVERAGE POWER (W)':>28}")
    print(header)
    for iv in INTERVALS:
        lam = 1.0 / iv
        row = "".join(f"{power(p, lam, t, duration_s):12.3f}" for t in DORMANCIES)
        print(f"  {iv:7.0f} s {row}")
    print()
    print(f"{'DETECTION RATE':>26}")
    print(header)
    for iv in INTERVALS:
        lam = 1.0 / iv
        row = "".join(f"{detection_rate(p, lam, t, duration_s):12.3f}"
                      for t in DORMANCIES)
        print(f"  {iv:7.0f} s {row}")


def check_linearity(p: Params, duration_s: float) -> float:
    """Max deviation of the predicted front from a straight line, in W.

    Prediction 3 says this is zero. If it is not, the algebra is wrong.
    """
    worst = 0.0
    for iv in INTERVALS:
        lam = 1.0 / iv
        pts = []
        for i in range(1, 200):
            t_d = 5.0 * iv * i / 199.0
            pts.append((detection_rate(p, lam, t_d, duration_s),
                        avg_power(p, lam, t_d, duration_s)))
        (d0, p0), (d1, p1) = pts[0], pts[-1]
        if abs(d1 - d0) < 1e-12:
            continue
        slope = (p1 - p0) / (d1 - d0)
        worst = max(worst, max(abs(pw - (p0 + slope * (d - d0))) for d, pw in pts))
    return worst


def write_pareto(p: Params, duration_s: float, path: str, n: int = 80) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["mean_interval_s", "dormancy_s", "detection_rate",
                    "avg_power_W", "pareto_slope_W"])
        for iv in INTERVALS:
            lam = 1.0 / iv
            k = marginal_slope(p, lam, duration_s)
            for i in range(n + 1):
                t_d = math.inf if i == n else 5.0 * iv * i / (n - 1)
                w.writerow([iv, fmt(t_d),
                            f"{detection_rate(p, lam, t_d, duration_s):.6f}",
                            f"{avg_power(p, lam, t_d, duration_s):.6f}",
                            f"{k:.6f}"])
    print(f"wrote {path}")


def run_monte_carlo(p: Params, duration_s: float, n_events: int, seed: int) -> int:
    print(f"Closed form vs simulation  (n={n_events} events, duration={duration_s:g} s)")
    print(f"  {'interval':>9} {'T_d':>7} {'P_cf':>8} {'P_mc':>8} {'dP%':>7} "
          f"{'det_cf':>8} {'det_mc':>8} {'boots':>7}")
    worst_p = worst_d = 0.0
    for iv in INTERVALS:
        lam = 1.0 / iv
        for t_d in DORMANCIES:
            sim = simulate(p, lam, t_d, duration_s, n_events=n_events, seed=seed)
            cf_p = avg_power(p, lam, t_d, duration_s)
            cf_d = detection_rate(p, lam, t_d, duration_s)
            err = 100.0 * (sim["avg_power_W"] - cf_p) / cf_p
            worst_p = max(worst_p, abs(err))
            worst_d = max(worst_d, abs(sim["detection_rate"] - cf_d))
            print(f"  {iv:7.0f} s {fmt(t_d):>7} {cf_p:8.3f} {sim['avg_power_W']:8.3f} "
                  f"{err:+6.1f}% {cf_d:8.3f} {sim['detection_rate']:8.3f} "
                  f"{sim['boots']:7d}")
    lin = check_linearity(p, duration_s)
    print()
    print(f"worst closed-form power error vs simulation: {worst_p:.2f}%")
    print(f"worst closed-form detection error:           {worst_d:.4f}")
    print(f"max deviation of the front from a straight line: {lin:.2e} W "
          f"(prediction 3 says 0)")
    ok = worst_p < 2.0 and worst_d < 0.01 and lin < 1e-9
    print("OK: closed form matches the simulation." if ok else
          "FAIL: closed form and simulation disagree. Fix the model before measuring.")
    return 0 if ok else 1


def main() -> int:
    global INTERVALS, DORMANCIES
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--measured", metavar="JSON",
                    help="measured constants, overriding the estimates")
    ap.add_argument("--duration", type=float, default=15.0,
                    help="event duration in seconds (default: 15, i.e. 0.5*T_boot)")
    ap.add_argument("--pareto", action="store_true",
                    help="write the predicted Pareto front as CSV")
    ap.add_argument("-o", "--out", default="predicted_pareto.csv")
    ap.add_argument("--monte-carlo", action="store_true",
                    help="check the closed form against a discrete-event simulation")
    ap.add_argument("--naive", action="store_true",
                    help="use the sparse-event model instead, for contrast")
    ap.add_argument("--intervals", type=float, nargs="+", metavar="S",
                    help="mean inter-event intervals for the tables "
                         f"(default: {' '.join(f'{v:g}' for v in INTERVALS)})")
    ap.add_argument("--dormancies", type=float, nargs="+", metavar="S",
                    help="dormancy timeouts for the tables; 'never' is always appended")
    ap.add_argument("--n-events", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    if args.intervals:
        INTERVALS = list(args.intervals)
    if args.dormancies:
        DORMANCIES = list(args.dormancies) + [math.inf]

    p = load_params(args.measured)
    if not args.measured:
        print("### USING PRE-MEASUREMENT ESTIMATES -- see docs/EXPERIMENTS.md section 0\n")

    if args.monte_carlo:
        return run_monte_carlo(p, args.duration, args.n_events, args.seed)

    print_predictions(p, args.duration, args.naive)
    if args.pareto:
        print()
        write_pareto(p, args.duration, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
