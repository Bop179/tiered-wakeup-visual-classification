#!/usr/bin/env python3
"""Trigger-patch stimulus for Tier 1 on its own: scope work, trimmer tuning, the ROC.

    tools/trigger_patch.py flash                          # steady flashes until Esc
    tools/trigger_patch.py sweep --trimmer 2.5 --serial /dev/cu.usbmodem1101
    tools/trigger_patch.py quiet --quiet-s 300 --serial /dev/cu.usbmodem1101
    tools/trigger_patch.py flash --count 1 --contrast 1    # one wake flash
    tools/trigger_patch.py sweep --trimmer 2.5 --dry-run   # no window, simulated
    tools/trigger_patch.py --self-test

Needs pygame (tools/setup_mac.sh installs it). pyserial only with --serial. No
images, no model, no Pi.

The patch comes from tools/event_display.py's own patch_rect() and patch_level(),
so the ROC describes exactly the patch the matrix flashes: same corner, same size,
same luminance per contrast. Keep --display, --patch-corner and --patch-frac the
same as the matrix runs use.

Counting triggers
-----------------
--serial PORT   the Uno over USB. Every line matching --match (default ^EVT,) is a
                trigger, timestamped on this Mac's clock as it arrives, so it lines
                up with the flashes with no reconciliation. Unless --listen-only,
                this also answers like a minimal Pi -- "# ready", SET,DORMANCY,-1,
                ACK and RES,-1,0.000,0 to every EVT, SYNC echoed -- so the firmware
                never waits on a Pi that isn't there. The result is the ROC of the
                trigger path the system really uses: comparator, then persistence
                and refractory in Tier 2.
no --serial     press SPACE each time the comparator LED lights.

A trigger answers a flash if it lands between onset - 0.1 s and the end of the
flash + --tolerance. During a sweep, anything else is a stray.

sweep
-----
One trimmer position per run. A quiet window first -- static black, every trigger
counted, giving false_per_min -- then --per-contrast flashes at each --contrasts
value, highest first, so a dead circuit shows in the first minute. Appends one row
per contrast to data/tier1_roc.csv, in the columns analysis/plots.py reads:

    trimmer,contrast,false_per_min,detect_rate,n_events,notes

and every flash to data/tier1_roc_detail/. Run it at >= 6 trimmer positions, then
lock the trimmer and write the wiper voltage into docs/trigger_characterization.md.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import itertools
import math
import random
import re
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
from event_display import (DEFAULT_PATCH_CORNER, DEFAULT_PATCH_FRAC,  # noqa: E402
                           patch_level, patch_rect)

ROC_HEADER = ["trimmer", "contrast", "false_per_min", "detect_rate", "n_events", "notes"]
DETAIL_HEADER = ["trimmer", "flash_idx", "contrast", "t_on", "flash_ms", "detected",
                 "latency_s"]
PRE_S = 0.1


# ------------------------------------------------------------------ plan + match

def flash_plan(contrasts, per, flash_ms, gap_s, jitter_s, seed):
    """Flashes in order. per=None repeats the first contrast forever."""
    rng = random.Random(seed)
    counts = itertools.repeat(contrasts[0]) if per is None else \
        (c for c in contrasts for _ in range(per))
    for c in counts:
        yield {"contrast": c, "flash_s": flash_ms / 1000.0,
               "dwell_s": gap_s + rng.uniform(0.0, jitter_s)}


def match(flashes, triggers, tolerance, pre=PRE_S):
    """flashes: [(t_on, flash_s)]. Returns (detected, latency_s, strays)."""
    triggers = sorted(triggers)
    used = [False] * len(triggers)
    detected, latency = [], []
    for t_on, flash_s in flashes:
        first, k = None, bisect.bisect_left(triggers, t_on - pre)
        while k < len(triggers) and triggers[k] <= t_on + flash_s + tolerance:
            if first is None:
                first = triggers[k] - t_on
            used[k] = True
            k += 1
        detected.append(first is not None)
        latency.append(first)
    return detected, latency, used.count(False)


def roc_rows(trimmer, shown, detected, n_quiet, quiet_s, notes):
    fpm = f"{n_quiet / (quiet_s / 60.0):.3f}" if quiet_s > 0 else ""
    rows = []
    for c in dict.fromkeys(f["contrast"] for f in shown):
        hits = [d for f, d in zip(shown, detected) if f["contrast"] == c]
        rows.append([trimmer, f"{c:g}", fpm, f"{sum(hits) / len(hits):.3f}", len(hits), notes])
    return rows


def append_csv(path, header, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(header)
        w.writerows(rows)


# ------------------------------------------------------------------ trigger sources

class SerialSource:
    """The Uno over USB, answered like a minimal Pi so its firmware never blocks."""

    def __init__(self, port, baud, pattern, answer):
        try:
            import serial
        except ImportError:
            sys.exit("--serial needs pyserial:  .venv/bin/pip install pyserial")
        self.ser = serial.Serial(port, baud, timeout=0.05)
        self.pattern, self.answer = re.compile(pattern), answer
        self.times, self.n_lines = [], 0
        self._t0 = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _send(self, line):
        self.ser.write((line + "\n").encode("ascii"))

    def _run(self):
        buf, greeted = bytearray(), 0
        while not self._stop.is_set():
            # Opening the port resets an Uno and its bootloader takes ~1.5 s, so
            # greet twice, once it is surely listening.
            if self.answer and greeted < 2 and time.monotonic() - self._t0 > 2.5 + 2.0 * greeted:
                self._send("# ready")
                self._send("SET,DORMANCY,-1")
                greeted += 1
            buf.extend(self.ser.read(256))
            while b"\n" in buf:
                i = buf.index(b"\n")
                line = buf[:i].decode("ascii", "replace").strip()
                del buf[:i + 1]
                if not line:
                    continue
                t = time.time()
                self.n_lines += 1
                if self.pattern.search(line):
                    self.times.append(t)
                if not self.answer:
                    continue
                parts = line.split(",")
                if parts[0] == "EVT" and len(parts) >= 4:
                    self._send("ACK")
                    self._send("RES,-1,0.000,0")
                elif parts[0] == "SYNC":
                    self._send(f"SYNC,{int(time.monotonic() * 1000) & 0xFFFFFFFF}")
                elif parts[0] == "HALT":
                    self._send("ACK")

    def close(self):
        self._stop.set()
        self._thread.join(timeout=1.0)
        self.ser.close()


def simulate(flashes, quiet_s, threshold, false_per_min, seed):
    """A made-up Tier 1: logistic detection around `threshold`, Poisson noise."""
    rng = random.Random(seed)
    t, triggers = 1000.0, []

    def noise(t0, t1):
        x, rate = t0, false_per_min / 60.0
        while rate > 0:
            x += rng.expovariate(rate)
            if x >= t1:
                break
            triggers.append(x)

    quiet = (t, t + quiet_s)
    noise(*quiet)
    t += quiet_s
    shown = []
    for f in flashes:
        t_on = t + f["dwell_s"]
        noise(t, t_on)
        shown.append({**f, "t_on": t_on})
        if rng.random() < 1.0 / (1.0 + math.exp(-(f["contrast"] - threshold) / 0.04)):
            triggers.append(t_on + rng.uniform(0.05, 0.35))
        t = t_on + f["flash_s"]
    return shown, quiet, triggers


# ------------------------------------------------------------------ display

def run_window(args, flashes, quiet_s, serial_src):
    try:
        import pygame
    except ImportError:
        sys.exit("pygame not installed. Run tools/setup_mac.sh, or use --dry-run.")
    pygame.init()
    pygame.mouse.set_visible(False)
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN | pygame.SCALED,
                                     display=args.display, vsync=1)
    w, h = screen.get_size()
    rect = patch_rect(w, h, args.patch_corner, args.patch_frac)
    print(f"# display {args.display}: {w}x{h}, patch {rect[2]} px at {args.patch_corner}",
          file=sys.stderr)
    keys, state = [], {"abort": False}

    def pump():
        for e in pygame.event.get():
            if e.type == pygame.QUIT or (e.type == pygame.KEYDOWN
                                         and e.key in (pygame.K_ESCAPE, pygame.K_q)):
                state["abort"] = True
            elif e.type == pygame.KEYDOWN and e.key == pygame.K_SPACE:
                keys.append(time.time())
        return not state["abort"]

    def hold(until):
        while time.time() < until:
            if not pump():
                return False
            time.sleep(0.004)
        return True

    def black():
        screen.fill((0, 0, 0))
        pygame.display.flip()

    def triggers():
        return (serial_src.times if serial_src else []) + keys

    black()
    shown, quiet = [], (0.0, 0.0)
    # Opening a full-screen window can flash the corner; nothing before this counts.
    if hold(time.time() + args.lead_in):
        t_start = time.time()
        if quiet_s > 0:
            print(f"# quiet window, {quiet_s:g} s of static black", file=sys.stderr)
            hold(t_start + quiet_s)
            quiet = (t_start, time.time())
            n = sum(1 for t in triggers() if quiet[0] <= t <= quiet[1])
            print(f"#   {n} trigger(s) -> {n / (quiet_s / 60):.2f} /min", file=sys.stderr)
        for i, f in enumerate(flashes):
            if state["abort"] or not hold(time.time() + f["dwell_s"]):
                break
            screen.fill((0, 0, 0))
            pygame.draw.rect(screen, patch_level(f["contrast"]), rect)
            pygame.display.flip()
            t_on = time.time()               # vsync: flip returns once presented
            shown.append({**f, "t_on": t_on})
            ok = hold(t_on + f["flash_s"])
            black()
            if not ok:
                break
            if args.mode == "flash":
                hold(time.time() + min(args.tolerance, f["dwell_s"]))
                if serial_src or keys:
                    det, lat, _ = match([(t_on, f["flash_s"])], triggers(), args.tolerance)
                    verdict = f"answered in {lat[0]:.2f} s" if det[0] else "no trigger"
                else:
                    verdict = ""
                print(f"[{i:4d}] c={f['contrast']:.2f}  {verdict}", file=sys.stderr)
            elif (i + 1) % args.per_contrast == 0:
                print(f"#   contrast {f['contrast']:g} done", file=sys.stderr)
        hold(time.time() + args.tolerance + 0.5)   # late triggers
    pygame.quit()
    return shown, quiet, [t for t in triggers() if shown or quiet_s], state["abort"]


# ------------------------------------------------------------------ self-test

def self_test() -> int:
    failures = []

    def check(name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    check("patch geometry is event_display's", patch_rect(1920, 1080, "tr", 0.18)
          == (1726, 0, 194, 194) and patch_level(0.4) == (102, 102, 102))

    det, lat, strays = match([(10, 1.5), (20, 1.5), (30, 1.5)],
                             [9.95, 20.2, 20.4, 25.0, 32.4], tolerance=1.0)
    check("a trigger 50 ms before onset still counts", det[0] and abs(lat[0] + 0.05) < 1e-9)
    check("two triggers in one window: one detection, no stray", det[1] and strays == 1)
    check("a trigger inside the tolerance after the flash counts", det[2])
    det, _, strays = match([(10, 1.5)], [12.6], tolerance=1.0)
    check("a trigger past the tolerance is a stray, not a detection", not det[0] and strays == 1)

    shown = [{"contrast": c} for c in (1.0, 1.0, 0.5, 0.5)]
    rows = roc_rows("2.5", shown, [True, True, True, False], 2, 300.0, "t")
    check("ROC rows: false/min from the quiet window, rate per contrast",
          rows == [["2.5", "1", "0.400", "1.000", 2, "t"], ["2.5", "0.5", "0.400", "0.500", 2, "t"]],
          str(rows))

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "roc.csv"
        append_csv(p, ROC_HEADER, rows)
        append_csv(p, ROC_HEADER, rows)
        lines = p.read_text().splitlines()
        check("appending twice writes one header", lines.count(",".join(ROC_HEADER)) == 1
              and len(lines) == 5)

    plan = list(flash_plan([1.0, 0.05], 20, 1500, 3.0, 2.0, seed=1))
    shown, quiet, trig = simulate(plan, 300.0, threshold=0.3, false_per_min=0.0, seed=2)
    det, _, strays = match([(f["t_on"], f["flash_s"]) for f in shown], trig, 1.0)
    rows = roc_rows("x", shown, det, 0, 300.0, "")
    check("simulated circuit: 100% at full contrast, 0% far below threshold",
          rows[0][3] == "1.000" and rows[1][3] == "0.000" and strays == 0, str(rows))

    print("\nPASS: trigger_patch.py" if not failures else f"\nFAIL: {len(failures)} check(s)")
    return 1 if failures else 0


# ------------------------------------------------------------------ entry

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", choices=["flash", "sweep", "quiet"], default="flash")
    ap.add_argument("--trimmer", default="",
                    help="sweep: turns from the CCW end, or the wiper voltage -- be "
                         "consistent, it labels the ROC series")
    ap.add_argument("--contrast", type=float, default=1.0, help="flash: patch contrast")
    ap.add_argument("--count", type=int, default=0, help="flash: stop after N (0 = until Esc)")
    ap.add_argument("--contrasts", type=float, nargs="+",
                    default=[1.0, 0.8, 0.6, 0.4, 0.3, 0.2, 0.1, 0.05])
    ap.add_argument("--per-contrast", type=int, default=20)
    ap.add_argument("--quiet-s", type=float, default=300.0,
                    help="sweep/quiet: seconds of static black counted for false triggers")
    ap.add_argument("--flash-ms", type=int, default=1500)
    ap.add_argument("--gap-s", type=float, default=3.0, help="black before each flash")
    ap.add_argument("--jitter-s", type=float, default=2.0,
                    help="random extra black, so flashes are not periodic")
    ap.add_argument("--tolerance", type=float, default=1.0,
                    help="seconds after a flash ends that a trigger still counts")
    ap.add_argument("--serial", help="the Uno's USB port, e.g. /dev/cu.usbmodem1101")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--match", default=r"^EVT,", help="regex: which serial lines are triggers")
    ap.add_argument("--listen-only", action="store_true",
                    help="with --serial, do not answer like a Pi")
    ap.add_argument("--display", type=int, default=0)
    ap.add_argument("--patch-corner", choices=["tl", "tr", "bl", "br"], default=DEFAULT_PATCH_CORNER)
    ap.add_argument("--patch-frac", type=float, default=DEFAULT_PATCH_FRAC)
    ap.add_argument("--lead-in", type=float, default=10.0)  # macOS fullscreen takes 6-8 s
    ap.add_argument("--roc-out", type=Path,
                    help="default data/tier1_roc.csv (a --dry-run writes nothing unless given)")
    ap.add_argument("--detail-dir", type=Path, default=REPO / "data" / "tier1_roc_detail")
    ap.add_argument("--notes", default="", help="free text into the ROC rows")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true", help="no window: a simulated Tier 1")
    ap.add_argument("--sim-threshold", type=float, default=0.3)
    ap.add_argument("--sim-false-per-min", type=float, default=0.4)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if args.mode == "sweep" and not args.trimmer:
        ap.error("sweep needs --trimmer (turns from the CCW end, or the wiper voltage)")
    if not all(0.0 <= c <= 1.0 for c in args.contrasts + [args.contrast]):
        ap.error("contrasts must be in 0..1")

    if args.mode == "flash":
        if args.dry_run and not args.count:
            ap.error("flash --dry-run needs --count")
        flashes = flash_plan([args.contrast], args.count or None, args.flash_ms,
                             args.gap_s, args.jitter_s, args.seed)
        quiet_s = 0.0
    elif args.mode == "sweep":
        flashes = flash_plan(args.contrasts, args.per_contrast, args.flash_ms,
                             args.gap_s, args.jitter_s, args.seed)
        quiet_s = args.quiet_s
    else:
        flashes, quiet_s = iter(()), args.quiet_s

    if args.mode == "sweep":
        n = len(args.contrasts) * args.per_contrast
        est = quiet_s + n * (args.gap_s + args.jitter_s / 2 + args.flash_ms / 1000)
        print(f"# sweep, trimmer {args.trimmer}: {quiet_s:g} s quiet + {n} flashes "
              f"at {len(args.contrasts)} contrasts, about {est / 60:.0f} min", file=sys.stderr)

    serial_src = None
    if args.dry_run:
        shown, quiet, trig = simulate(list(flashes), quiet_s, args.sim_threshold,
                                      args.sim_false_per_min, args.seed)
        source, aborted = f"simulated (threshold {args.sim_threshold:g})", False
    else:
        if args.serial:
            serial_src = SerialSource(args.serial, args.baud, args.match, not args.listen_only)
            source = f"serial {args.match}"
        else:
            source = "SPACE taps"
            if args.mode != "flash" or args.count != 1:
                print("# no --serial: press SPACE each time the comparator LED lights",
                      file=sys.stderr)
        try:
            shown, quiet, trig, aborted = run_window(args, flashes, quiet_s, serial_src)
        finally:
            if serial_src:
                serial_src.close()

    if args.mode == "flash":
        return 0

    n_quiet = sum(1 for t in trig if quiet[0] <= t <= quiet[1])
    after = [t for t in trig if t > quiet[1]]
    if args.mode == "quiet":
        print(f"quiet window: {n_quiet} trigger(s) in {quiet_s:g} s = "
              f"{n_quiet / (quiet_s / 60):.3f} /min   [{source}]")
        return 0

    per = args.per_contrast
    complete = [c for c in dict.fromkeys(f["contrast"] for f in shown)
                if sum(1 for f in shown if f["contrast"] == c) == per]
    if aborted and len(complete) < len(args.contrasts):
        print(f"# aborted: keeping only the {len(complete)} contrast(s) with all {per} flashes",
              file=sys.stderr)
    shown = [f for f in shown if f["contrast"] in complete]
    det, lat, strays = match([(f["t_on"], f["flash_s"]) for f in shown], after, args.tolerance)
    notes = "; ".join(x for x in (source, f"quiet {quiet_s:g}s {n_quiet} trig",
                                  f"stray {strays}", args.notes) if x)
    rows = roc_rows(args.trimmer, shown, det, n_quiet, quiet_s, notes)

    print(f"\ntrimmer {args.trimmer}: {n_quiet} false in {quiet_s:g} s, {strays} stray  [{source}]")
    for r in rows:
        print(f"  contrast {r[1]:>5}  detect {r[3]}  of {r[4]}")

    roc_out = args.roc_out or (None if args.dry_run else REPO / "data" / "tier1_roc.csv")
    if roc_out and rows:
        append_csv(roc_out, ROC_HEADER, rows)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9.]+", "_", args.trimmer)
        detail = args.detail_dir / f"{safe}_{stamp}.csv"
        append_csv(detail, DETAIL_HEADER,
                   [[args.trimmer, i, f"{f['contrast']:g}", f"{f['t_on']:.3f}",
                     int(f["flash_s"] * 1000), int(d), "" if l is None else f"{l:.3f}"]
                    for i, (f, d, l) in enumerate(zip(shown, det, lat))])
        print(f"\nappended {len(rows)} row(s) to {roc_out}\nflashes in {detail}")
    elif args.dry_run:
        print("\n(dry run: nothing written -- pass --roc-out to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
