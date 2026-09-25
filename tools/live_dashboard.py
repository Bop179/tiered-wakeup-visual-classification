#!/usr/bin/env python3
"""Live demo dashboard: Pi state, boot time, power, latest classification.

    tools/live_dashboard.py                       # follows the newest data/<run_id>/
    tools/live_dashboard.py --run data/<run_id>   # pin one run (also replays a finished one)
    tools/live_dashboard.py --self-test

Run it next to tools/run_experiment.py, on the laptop screen. Everything it shows
comes from files that run already writes; it adds nothing to the Pi.

  POWER      tails the local power.csv that tools/fnb58_logger.py writes.
  STATE      comes from that power trace, not from ssh: a halted Pi is off the
             network, but the meter still sees it. Mean power over the last 0.5 s
             above --halt-w is "up". Going from halted to up starts BOOTING; the
             daemon's boots.csv row for that kernel boot makes it AWAKE.
  BOOT TIME  the firmware stage (wake -> kernel, --firmware-s) plus the daemon's own
             uptime in its boots.csv row, written once model and camera are loaded,
             just before "# ready". That is how docs/EXPERIMENTS.md measures T_boot.
             Not "until ssh sees it": the Pi rejoins WiFi ~20 s after it is ready,
             so that read ~40 s for a ~21 s boot. The row only counts once it carries
             the running kernel's boot_id, so a row from the previous boot cannot.
             The state still flips to AWAKE only when ssh sees the row, so it lags.
             Past --up-after-s of BOOTING it shows "UP, NO SSH": the boot is
             done, only the WiFi rejoin is slow, so the audience never sees a
             minute-long "boot".
  RESULT     the last rows of the Pi's events.csv, fetched over ssh while it is up.
             While it is halted the last result stays on screen.
  TIER 1     every stimulus in the local gen.csv, paired with the EVT it caused in
             events.csv (analysis/accuracy.py's pairing): when it fired, stimulus ->
             Pi delay (includes the boot if it woke the Pi), and whether a real
             stimulus was MISSED or a flicker falsely triggered. "noise" counts the
             short pulses Tier 2 rejected, from daemon.log (only heard while awake).
             Delays use raw Pi and Mac clocks (both NTP), so they carry that error.
             A manual run (run_experiment.py --n-events 0: black screen, you flash
             the LDR by hand) has no stimuli, so every EVT is listed as "manual".
  MODE       always-on or sleeping, from the dormancy Tier 2 actually confirmed in
             daemon.log. If the Uno never confirms it, the Uno cannot hear the Pi
             (usually its USB cable is still plugged in) and the title says so.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "analysis"))
from accuracy import _assign  # noqa: E402  same stimulus <-> EVT pairing as the scoring

CONSTANTS = json.loads((REPO / "data" / "constants.json").read_text())
MEAN_WINDOW_S = 0.5
BOOT_WINDOW_S = 90.0   # accuracy.py --boot-window: a booted EVT may trail its onset this far
MISS_AWAKE_S = 5.0     # Pi was up at onset: no EVT after this is a miss (2 s window + poll lag)


class PiState:
    """Halted / booting / awake from the power trace plus boots.csv."""

    def __init__(self, halt_w: float, firmware_s: float = 9.5):
        self.halt_w, self.firmware_s = halt_w, firmware_s
        self.state = None          # None until the first power sample
        self.t_rise = None
        self.boot_id = None
        self.last_boot_s = None

    def on_power(self, t: float, mean_w: float) -> None:
        up = mean_w > self.halt_w
        if not up:
            self.state, self.t_rise = "HALTED", None
        elif self.state == "HALTED":
            self.state, self.t_rise = "BOOTING", t
        elif self.state is None:
            self.state = "AWAKE"   # already up when we started; boot not observed

    def on_ready(self, boot_id: str, uptime_s: float) -> None:
        """The daemon of kernel boot `boot_id` is up; uptime_s is from its boots.csv row."""
        if boot_id == self.boot_id:
            return
        self.boot_id = boot_id
        if self.state == "BOOTING":
            self.last_boot_s = self.firmware_s + uptime_s
            self.state, self.t_rise = "AWAKE", None

    def label(self, now: float, up_after_s: float) -> str | None:
        """State to show. A boot is ~21 s; BOOTING much past that means the Pi is
        up and ssh just can't reach it yet."""
        if self.state == "BOOTING" and now - self.t_rise > up_after_s:
            return "UP, NO SSH"
        return self.state


def tier1_log(gen: list[dict], events: list[dict], now: float, up_at, ok: bool,
              wake_edges: list[float] = ()):
    """(onset, what, delay_s or None, outcome) per stimulus, plus t_pi of EVTs that
    matched no stimulus. up_at(t): was the Pi awake at t. ok: is events.csv current.
    wake_edges: Mac times the Pi rose from halted, in order. A booted Pi's clock
    resumes from a stale save and stays behind until NTP syncs -- which a short
    dormancy never allows -- so a booted t_pi can land a minute before its own
    stimulus. Tier 2 wakes the Pi once per held event, so the k-th booted EVT is
    timed by the k-th wake edge instead; the delay shown is then stimulus -> wake."""
    onsets = [float(g["t_mac"]) for g in gen]
    times = [float(e["t_pi"]) for e in events]
    booted = [i for i, e in enumerate(events) if e["state_at_evt"] == "booted"]
    for i, t in zip(booted, wake_edges):
        times[i] = t
    assign = _assign(times, [e["state_at_evt"] for e in events], onsets, BOOT_WINDOW_S)
    got = {k: i for i, k in enumerate(assign) if k is not None}
    rows = []
    for k, g in enumerate(gen):
        bait = g["image_id"] == "NONE"
        what = "flicker" if bait else g["true_class"]
        if k in got:
            i = got[k]
            out = ("FALSE TRIGGER" if bait else
                   "woke Pi" if events[i]["state_at_evt"] == "booted" else "triggered")
            rows.append((onsets[k], what, times[i] - onsets[k], out))
            continue
        wait = MISS_AWAKE_S if up_at(onsets[k]) else BOOT_WINDOW_S
        if now - onsets[k] < wait or (not bait and not ok):
            out = "..."                   # may still arrive; never call a miss blind
        else:
            out = "ignored" if bait else "MISSED"
        rows.append((onsets[k], what, None, out))
    return rows, [times[i] for i, k in enumerate(assign) if k is None]


def read_gen(path: Path) -> list[dict]:
    """gen.csv as it is being written; a half-written last row is dropped."""
    try:
        with open(path, newline="") as fh:
            rows = list(csv.DictReader(fh))
    except FileNotFoundError:
        return []
    return [r for r in rows if r.get("t_mac") and r.get("true_class") is not None]


def mode_text(run_name: str, dorm_line: str | None) -> tuple[str, str]:
    """Title line: requested dormancy from the run id, confirmed from daemon.log."""
    m = re.search(r"_t(-?\d+)_", run_name)
    want = int(m.group(1)) if m else None
    desc = lambda ms: ("ALWAYS ON: never halts" if ms < 0
                       else f"NORMAL: halts after {ms / 1000:g} s idle")
    got = re.search(r"DORMANCY=(-?\d+)", dorm_line or "")
    if got:
        return desc(int(got.group(1))), "#2b6cb0"
    if dorm_line:                                     # "not acknowledged"
        req = desc(want) if want is not None else "dormancy"
        return f"{req}? Tier 2 did NOT confirm: check the Uno link", "#c53030"
    return (desc(want) + "  (waiting for Tier 2)" if want is not None else ""), "#718096"


class PowerTail:
    """Follows a CSV that is still being written; keeps the last window_s of samples."""

    def __init__(self, path: Path, window_s: float):
        self.path = path
        self.fh = None
        self.buf = ""
        self.samples = deque(maxlen=int(window_s * 100))   # FNB58 streams 100 Hz
        self.e0 = self.t0 = None
        self.e_last = self.t_last = None
        self.energy = []       # (s since first sample, J used), whole run, ~5 Hz

    def poll(self) -> None:
        if self.fh is None:
            if not self.path.exists():
                return
            self.fh = open(self.path, newline="")
            self.fh.readline()                            # header
        self.buf += self.fh.read()
        *lines, self.buf = self.buf.split("\n")           # keep a half-written line
        for row in csv.reader(lines):
            if not row:
                continue
            t, w, e = float(row[0]), float(row[4]), float(row[8])
            self.samples.append((t, w))
            if self.e0 is None:
                self.e0, self.t0 = e, t
            self.e_last, self.t_last = e, t
            if not self.energy or t - self.t0 - self.energy[-1][0] >= 0.2:
                self.energy.append((t - self.t0, e - self.e0))

    def up_at(self, t: float, halt_w: float) -> bool:
        """Was the Pi drawing more than halted power just before t? False if unknown."""
        near = [w for ts, w in self.samples if t - MEAN_WINDOW_S <= ts <= t]
        return bool(near) and sum(near) / len(near) > halt_w

    def wake_edges(self, halt_w: float) -> list[float]:
        """Times the Pi rose from halted, whole run, from the ~5 Hz energy record.
        ponytail: a boot with no EVT (failed boot, manual power cycle) shifts every
        later pairing by one; key edges to boots.csv rows if that starts happening."""
        edges, halted = [], False
        for (s1, e1), (s2, e2) in zip(self.energy, self.energy[1:]):
            if s2 <= s1:
                continue
            up = (e2 - e1) / (s2 - s1) > halt_w
            if halted and up:
                edges.append(self.t0 + s2)
            halted = not up
        return edges

    def mean_w(self) -> float | None:
        if not self.samples:
            return None
        t_end = self.samples[-1][0]
        recent = [w for t, w in reversed(self.samples) if t >= t_end - MEAN_WINDOW_S]
        return sum(recent) / len(recent)


class RemoteTail(threading.Thread):
    """Polls the Pi's events.csv and boots.csv. Failure is normal: it halts."""

    def __init__(self, host: str, remote_dir: str, period_s: float = 1.0):
        super().__init__(daemon=True)
        self.host, self.remote_dir, self.period_s = host, remote_dir, period_s
        self.events: list[dict] = []
        self.dorm_line = None
        self.noise = (0, "")       # Tier 2 "noise" lines in daemon.log: count, last
        self.boot_id = None
        self.boot_uptime = None
        self.ok = False
        self.lock = threading.Lock()

    def follow(self, remote_dir: str) -> None:
        with self.lock:
            self.remote_dir, self.events, self.boot_id, self.ok = remote_dir, [], None, False
            self.dorm_line, self.noise = None, (0, "")

    def run(self) -> None:
        while True:
            d = self.remote_dir
            cmd = (f"cd {d} && cat events.csv; "
                   f"echo ---; cat /proc/sys/kernel/random/boot_id; tail -n 1 boots.csv; "
                   f"echo ---; grep -aE 'param DORMANCY=|DORMANCY not acknowledged' "
                   f"daemon.log | tail -n 1; "
                   f"echo ---; grep -ac 't2: noise' daemon.log; "
                   f"grep -a 't2: noise' daemon.log | tail -n 1")
            try:
                r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2",
                                    self.host, cmd], capture_output=True, text=True,
                                   timeout=6)
                t = time.time()
                ev_txt, _, rest = r.stdout.partition("---\n")
                boot_txt, _, rest = rest.partition("---\n")
                dorm, _, noise_txt = rest.partition("---\n")
                n_noise, _, last_noise = noise_txt.partition("\n")
                events = list(csv.DictReader(io.StringIO(ev_txt)))
                kernel_id, _, row = boot_txt.partition("\n")
                cols = row.split(",")
                row_id = cols[1] if len(cols) >= 3 else None
                with self.lock:
                    if d != self.remote_dir:
                        continue                  # switched runs mid-fetch; drop it
                    self.ok = r.returncode == 0
                    if events:
                        self.events = events
                    if dorm.strip():
                        self.dorm_line = dorm.strip()
                    if n_noise.strip().isdigit():
                        self.noise = (int(n_noise), last_noise.strip())
                    if row_id and row_id == kernel_id.strip() != self.boot_id:
                        self.boot_id, self.boot_uptime = row_id, float(cols[2])
            except subprocess.TimeoutExpired:
                with self.lock:
                    self.ok = False
            time.sleep(self.period_s)


def newest_run(data: Path, since: float = 0.0) -> Path | None:
    """Newest run whose power.csv was written after `since` (epoch seconds)."""
    runs = [p.parent for p in data.glob("*/power.csv") if p.stat().st_mtime > since]
    return max(runs, key=lambda p: (p / "power.csv").stat().st_mtime, default=None)


def move_to_display(title: str, index: int, full: bool = False) -> bool:
    """Put this process's window `title` on monitor `index`, native full screen if `full`.

    The macosx backend has no geometry API, and moving the window from outside
    (System Events) needs Accessibility permission, so talk to Cocoa in-process.
    NSScreen order matches event_display.py's --display (0 = built-in)."""
    import ctypes
    import ctypes.util
    from ctypes import c_char_p, c_double, c_long, c_void_p

    class Rect(ctypes.Structure):
        _fields_ = [("x", c_double), ("y", c_double), ("w", c_double), ("h", c_double)]

    class Point(ctypes.Structure):
        _fields_ = [("x", c_double), ("y", c_double)]

    if sys.platform != "darwin":
        return False
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    objc.objc_getClass.restype = objc.sel_registerName.restype = c_void_p

    def send(obj, sel, restype=c_void_p, *args, argtypes=()):
        fn = ctypes.CFUNCTYPE(restype, c_void_p, c_void_p, *argtypes)(
            ctypes.cast(objc.objc_msgSend, c_void_p).value)
        return fn(obj, objc.sel_registerName(sel.encode()), *args)

    screens = send(objc.objc_getClass(b"NSScreen"), "screens")
    if not 0 <= index < send(screens, "count", c_long):
        return False
    scr = send(screens, "objectAtIndex:", c_void_p, index, argtypes=(c_long,))
    vis = send(scr, "visibleFrame", Rect)     # bottom-left origin, minus menu bar
    wins = send(send(objc.objc_getClass(b"NSApplication"), "sharedApplication"), "windows")
    for i in range(send(wins, "count", c_long)):
        w = send(wins, "objectAtIndex:", c_void_p, i, argtypes=(c_long,))
        if send(send(w, "title"), "UTF8String", c_char_p) == title.encode():
            send(w, "setFrameTopLeftPoint:", None, Point(vis.x, vis.y + vis.h),
                 argtypes=(Point,))
            if full:   # goes full screen on whichever display the window is on now
                send(w, "toggleFullScreen:", None, None, argtypes=(c_void_p,))
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", type=Path, help="run dir (default: newest data/*/power.csv)")
    ap.add_argument("--host", default=os.environ.get("PI_HOST", "pi"),
                    help="ssh target for the Pi (default: $PI_HOST or 'pi')")
    ap.add_argument("--pi-repo", default="~/tiered-wakeup-visual-classification")
    ap.add_argument("--window", type=float, default=120, help="seconds of power shown")
    # Calibration knob. Halted is a flat ~2.00 W, but the first ~10 s of a boot sit at
    # 2.5-2.9 W, so the midpoint to idle (2.6 W) flickers all through the boot. Sit
    # just above halted instead. Move it if the rig's halted draw changes.
    ap.add_argument("--halt-w", type=float, default=CONSTANTS["p_halt"] + 0.15)
    # Wake edge -> kernel start, invisible to the Pi's own clock. 9.35 s (Sep 12) and
    # 9.54 s (Sep 18) in docs/EXPERIMENTS.md; re-measure if the boot config changes.
    ap.add_argument("--firmware-s", type=float, default=9.5)
    # Calibration knob: past this much BOOTING, show "UP, NO SSH". T_boot is
    # 20.8 s (Sep 18); leave margin so a slow real boot still reads as BOOTING.
    ap.add_argument("--up-after-s", type=float, default=30.0)
    ap.add_argument("--main-display", type=int, default=0,
                    help="monitor for the main window (default 0 = built-in, -1 = leave it)")
    ap.add_argument("--energy-display", type=int, default=1,
                    help="monitor for the energy window (default 1; -1 = leave it)")
    ap.add_argument("--windowed", action="store_true", help="place windows, skip full screen")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    # Without --run, only a run that is live now or starts later counts, so a
    # finished run's results never greet the audience.
    since = time.time() - 10
    run = args.run or newest_run(REPO / "data", since)
    if run is None:
        print("waiting for a run to start writing data/<run_id>/power.csv ...")
        while run is None:
            time.sleep(2)
            run = newest_run(REPO / "data", since)
    remote = RemoteTail(args.host, "")
    cur = {}

    def follow(run: Path) -> None:
        # Without --run, a newer run that starts while this is open takes over, so
        # the dashboard can be left up across demo runs.
        print(f"following {run.name}")
        cur.update(run=run, power=PowerTail(run / "power.csv", args.window),
                   pi=PiState(args.halt_w, args.firmware_s), checked=time.time())
        remote.follow(f"{args.pi_repo}/data/{run.name}")

    follow(run)
    remote.start()

    plt.rcParams.update({"font.size": 13, "toolbar": "None"})
    fig = plt.figure(figsize=(14, 8))
    fig.canvas.manager.set_window_title("Tiered wake-up: live")
    gs = fig.add_gridspec(2, 2, height_ratios=[1, 1.3], hspace=0.35, wspace=0.15)
    bottom = gs[1, :].subgridspec(1, 2, width_ratios=[1.7, 1], wspace=0.05)
    ax_state, ax_res, ax_p, ax_t1 = (fig.add_subplot(gs[0, 0]), fig.add_subplot(gs[0, 1]),
                                     fig.add_subplot(bottom[0]), fig.add_subplot(bottom[1]))
    for ax in (ax_state, ax_res, ax_t1):
        ax.axis("off")
    state_txt = ax_state.text(0.02, 0.72, "", fontsize=40, weight="bold",
                              transform=ax_state.transAxes)
    state_sub = ax_state.text(0.02, 0.05, "", fontsize=15, family="monospace",
                              transform=ax_state.transAxes, va="bottom")
    res_txt = ax_res.text(0.0, 0.72, "", fontsize=26, weight="bold",
                          transform=ax_res.transAxes)
    res_sub = ax_res.text(0.0, 0.05, "", fontsize=12, family="monospace",
                          transform=ax_res.transAxes, va="bottom")
    (line,) = ax_p.plot([], [], lw=1.2, color="#2b6cb0")
    ax_p.axhline(args.halt_w, ls="--", lw=1, color="gray")
    ax_p.set_xlabel("seconds ago")
    ax_p.set_ylabel("Pi power (W)")
    ax_p.set_xlim(-args.window, 0)
    ax_p.set_ylim(0, 6)
    ax_p.grid(alpha=0.3)
    colors = {"AWAKE": "#2f855a", "UP, NO SSH": "#b7791f", "BOOTING": "#c05621", "HALTED": "#4a5568"}
    # Tier 1 panel: one text per row so each outcome gets its own color.
    t1_colors = {"triggered": "#2f855a", "woke Pi": "#2f855a", "MISSED": "#c53030",
                 "FALSE TRIGGER": "#c53030", "ignored": "#718096", "...": "#718096",
                 "manual": "#2f855a", "manual, woke Pi": "#2f855a"}
    ax_t1.text(0.03, 0.97, "Tier 1  light trigger", fontsize=16, weight="bold",
               transform=ax_t1.transAxes, va="top")
    t1_sum = ax_t1.text(0.03, 0.86, "", fontsize=11, family="monospace",
                        transform=ax_t1.transAxes, va="top")
    t1_rows = [ax_t1.text(0.03, 0.60 - 0.065 * i, "", fontsize=11, family="monospace",
                          transform=ax_t1.transAxes, va="top") for i in range(9)]
    marks = [ax_p.vlines([], 0, 6)]           # stimulus onsets on the trace, by outcome
    title = fig.suptitle("", fontsize=18, weight="bold")

    def tick(_frame):
        if not args.run and time.time() - cur["checked"] > 2:
            cur["checked"] = time.time()
            newest = newest_run(REPO / "data", since)
            if newest and newest != cur["run"]:
                follow(newest)
        power, pi = cur["power"], cur["pi"]
        power.poll()
        with remote.lock:
            events, boot_id, boot_uptime, ok, dorm_line, noise = (
                remote.events, remote.boot_id, remote.boot_uptime, remote.ok,
                remote.dorm_line, remote.noise)
        text, color = mode_text(cur["run"].name, dorm_line)
        title.set_text(text)
        title.set_color(color)
        mean = power.mean_w()
        if mean is not None:
            pi.on_power(power.samples[-1][0], mean)
        if boot_id:
            pi.on_ready(boot_id, boot_uptime)

        if power.samples:
            t_end = power.samples[-1][0]
            line.set_data([t - t_end for t, _ in power.samples],
                          [w for _, w in power.samples])
        s = pi.label(time.time(), args.up_after_s) or "NO METER DATA"
        state_txt.set_text(f"Pi {s}")
        state_txt.set_color(colors.get(s, "black"))
        sub = []
        if pi.state == "BOOTING":
            sub.append(f"since wake  {time.time() - pi.t_rise:5.1f} s")
        if pi.last_boot_s is not None:
            sub.append(f"last boot   {pi.last_boot_s:5.1f} s  (wake -> ready)")
        if mean is not None:
            sub.append(f"power now   {mean:5.2f} W")
        if power.e0 is not None:
            used = power.e_last - power.e0
            always_on = CONSTANTS["p_idle"] * (power.t_last - power.t0)
            sub.append(f"energy used     {used:5.0f} J")
            sub.append(f"if never halted {always_on:5.0f} J  (saved {always_on - used:.0f} J)")
        state_sub.set_text("\n".join(sub))

        rows, stray = tier1_log(read_gen(cur["run"] / "gen.csv"), events, time.time(),
                                lambda t: power.up_at(t, args.halt_w), ok,
                                power.wake_edges(args.halt_w))
        if "_manual" in cur["run"].name:
            # No gen.csv rows, so every EVT is stray, in events.csv order.
            woke = [e["state_at_evt"] == "booted" for e in events]
            t1_sum.set_text(f"manual  {len(stray)} triggers, {sum(woke)} woke the Pi\n"
                            f"noise   {noise[0]} rejected by Tier 2")
            table = [(t, f"{'flashlight':<12}       ", "manual, woke Pi" if b else "manual")
                     for t, b in zip(stray, woke)]
        else:
            count = lambda *o: sum(r[3] in o for r in rows)
            n_real = sum(r[1] != "flicker" for r in rows)
            n_bait = len(rows) - n_real
            t1_sum.set_text(f"fired   {count('triggered', 'woke Pi'):>2}/{n_real:<3} missed {count('MISSED')}\n"
                            f"flicker ignored {count('ignored')}/{n_bait}  false {count('FALSE TRIGGER')}\n"
                            f"noise   {noise[0]} rejected by Tier 2, {len(stray)} stray EVT")
            table = [(t, f"{w[:12]:<12} {d:5.1f}s" if d is not None else f"{w[:12]:<12}      ", o)
                     for t, w, d, o in rows] + [(t, f"{'no stimulus':<12}       ", "NOISE") for t in stray]
        table.sort(key=lambda r: r[0])
        shown = table[-len(t1_rows):][::-1]
        for txt, r in zip(t1_rows, shown + [None] * len(t1_rows)):
            txt.set_text("" if r is None else
                         f"{time.strftime('%H:%M:%S', time.localtime(r[0]))} {r[1]} {r[2]}")
            txt.set_color("black" if r is None else t1_colors.get(r[2], "#c53030"))
        marks[0].remove()
        t_end = power.samples[-1][0] if power.samples else time.time()
        vis = [(t - t_end, t1_colors[o]) for t, _, _, o in rows if t >= t_end - args.window]
        marks[0] = ax_p.vlines([x for x, _ in vis], 0, 6, colors=[c for _, c in vis],
                               lw=2, alpha=0.6)

        if events:
            e = events[-1]
            name = e["class_name"].split(",")[0]
            res_txt.set_text("no confident class" if e["class_id"] == "-1"
                             else f"{name}  {float(e['confidence']):.0%}")
            rows = [f"{'#':>3} {'class':<18} {'conf':>5} {'lat ms':>7}"]
            for r in events[-6:][::-1]:
                rows.append(f"{r['event_idx']:>3} {r['class_name'].split(',')[0][:18]:<18} "
                            f"{float(r['confidence']):5.2f} {r['latency_ms']:>7}")
            res_sub.set_text("\n".join(rows))
        else:
            res_txt.set_text("waiting for first event")
            res_sub.set_text("")                      # drop the previous run's rows
        res_txt.set_color("black" if ok else "#718096")   # grey = Pi unreachable
        return line, state_txt, state_sub, res_txt, res_sub, title, t1_sum, marks[0], *t1_rows

    # Second window: cumulative energy, this run vs the same time spent always on.
    fig_e, ax_e = plt.subplots(figsize=(9, 5.5))
    fig_e.canvas.manager.set_window_title("Tiered wake-up: energy")
    (e_sleep,) = ax_e.plot([], [], lw=2.5, color="#2f855a", label="this run (sleeps)")
    (e_awake,) = ax_e.plot([], [], lw=2.5, ls="--", color="#c53030",
                           label=f"always on ({CONSTANTS['p_idle']:.2f} W idle)")
    ax_e.set_xlabel("seconds into run")
    ax_e.set_ylabel("energy used (J)")
    ax_e.grid(alpha=0.3)
    ax_e.legend(loc="upper left")
    e_txt = ax_e.text(0.98, 0.04, "", transform=ax_e.transAxes, ha="right",
                      fontsize=16, weight="bold")

    def tick_energy(_frame):
        pts = cur["power"].energy                 # filled by tick(); new list per run
        if not pts:
            e_sleep.set_data([], [])
            e_awake.set_data([], [])
            e_txt.set_text("waiting for meter data")
            return e_sleep, e_awake, e_txt
        ts = [t for t, _ in pts]
        awake = [CONSTANTS["p_idle"] * t for t in ts]
        e_sleep.set_data(ts, [e for _, e in pts])
        e_awake.set_data(ts, awake)
        ax_e.set_xlim(0, max(ts[-1], 10))
        ax_e.set_ylim(0, max(awake[-1], pts[-1][1], 10) * 1.1)
        saved = awake[-1] - pts[-1][1]
        e_txt.set_text(f"saved {saved:.0f} J ({saved / awake[-1]:.0%})" if awake[-1] else "")
        return e_sleep, e_awake, e_txt

    _anim = FuncAnimation(fig, tick, interval=250, cache_frame_data=False)
    _anim_e = FuncAnimation(fig_e, tick_energy, interval=500, cache_frame_data=False)
    for win, idx in (("Tiered wake-up: live", args.main_display),
                     ("Tiered wake-up: energy", args.energy_display)):
        if idx >= 0 and not move_to_display(win, idx, full=not args.windowed):
            print(f"# no display {idx}; '{win}' left where macOS put it", file=sys.stderr)
    plt.show()
    return 0


def self_test() -> int:
    p = PiState(halt_w=2.6, firmware_s=9.5)
    p.on_power(0.0, 3.3)
    assert p.state == "AWAKE"                  # up at start: boot not observed
    p.on_ready("A", 0.5)
    assert p.state == "AWAKE" and p.last_boot_s is None
    p.on_power(10.0, 2.0)
    assert p.state == "HALTED"
    p.on_power(40.0, 3.3)
    assert p.state == "BOOTING" and p.t_rise == 40.0
    p.on_power(45.0, 3.4)
    assert p.t_rise == 40.0                    # rise is not re-stamped while booting
    assert p.label(60.0, 30.0) == "BOOTING" and p.label(71.0, 30.0) == "UP, NO SSH"
    p.on_ready("A", 11.4)                      # stale id cannot end the boot
    assert p.state == "BOOTING"
    p.on_ready("B", 11.4)                      # seen late over ssh; uptime decides
    assert p.state == "AWAKE" and abs(p.last_boot_s - 20.9) < 1e-9
    assert p.label(999.0, 30.0) == "AWAKE"     # late ssh still records the true boot
    p.on_power(70.0, 1.9)
    assert p.state == "HALTED" and p.t_rise is None

    q = PiState(halt_w=2.6, firmware_s=9.5)    # dashboard started while halted
    q.on_power(0.0, 2.0)
    q.on_power(5.0, 3.3)
    q.on_ready("C", 10.5)
    assert q.state == "AWAKE" and abs(q.last_boot_s - 20.0) < 1e-9
    pt = PowerTail(Path(os.devnull), 10)
    pt.fh = io.StringIO("".join(f"{t / 100},0,0,0,3,0,0,0,{3 * t / 100}\n" for t in range(1001)))
    pt.poll()
    assert pt.energy[0] == (0.0, 0.0) and 45 <= len(pt.energy) <= 51   # ~5 Hz, not 100 Hz
    assert abs(pt.energy[-1][1] - 3 * pt.energy[-1][0]) < 1e-9
    gen = [{"t_mac": "100", "image_id": "banana.jpg", "true_class": "banana"},
           {"t_mac": "130", "image_id": "NONE", "true_class": "NONE"},
           {"t_mac": "160", "image_id": "orange.jpg", "true_class": "orange"},
           {"t_mac": "200", "image_id": "clock.jpg", "true_class": "clock"},
           {"t_mac": "240", "image_id": "NONE", "true_class": "NONE"}]
    evts = [{"t_pi": "121.0", "state_at_evt": "booted"},    # woke the Pi for the banana
            {"t_pi": "160.05", "state_at_evt": "awake"},    # orange, Pi already up
            {"t_pi": "240.1", "state_at_evt": "awake"},     # flicker got through
            {"t_pi": "300.0", "state_at_evt": "awake"}]     # nothing was shown
    rows, stray = tier1_log(gen, evts, 250.0, lambda t: t >= 121, ok=True, wake_edges=[121.0])
    assert [r[3] for r in rows] == ["woke Pi", "ignored", "triggered", "MISSED", "FALSE TRIGGER"]
    assert abs(rows[0][2] - 21.0) < 1e-9 and rows[3][2] is None and stray == [300.0]
    rows, _ = tier1_log(gen, evts, 250.0, lambda t: t >= 121, ok=False)
    assert rows[3][3] == "..."                 # ssh down: no miss called blind
    rows, _ = tier1_log(gen, [], 103.0, lambda t: False, ok=True)
    assert rows[0][3] == "..."                 # halted at onset: boot window applies
    # The Sep 22 screenshot: up, halt at 30 s, a banana wakes it at 80 s, and the
    # booted EVT's t_pi reads 42 because the Pi's clock resumed from the halt.
    w_at = lambda t: 3.2 if t < 30 or t >= 80 else 2.0
    lines, e = [], 0.0
    for n in range(12001):
        t = n / 100
        e += w_at(t) / 100
        lines.append(f"{t},0,0,0,{w_at(t)},0,0,0,{e}\n")
    pt = PowerTail(Path(os.devnull), 10)
    pt.fh = io.StringIO("".join(lines))
    pt.poll()
    edges = pt.wake_edges(2.15)
    assert len(edges) == 1 and 80.0 <= edges[0] <= 80.5, edges
    rows, stray = tier1_log([{"t_mac": "79.95", "image_id": "b.jpg", "true_class": "banana"}],
                            [{"t_pi": "42.0", "state_at_evt": "booted"}], 130.0,
                            lambda t: False, True, edges)
    assert rows[0][3] == "woke Pi" and not stray, (rows, stray)
    _, stray = tier1_log([], [{"t_pi": "42.0", "state_at_evt": "booted"},
                              {"t_pi": "90.0", "state_at_evt": "awake"}], 130.0,
                         lambda t: False, True, edges)
    assert stray == [edges[0], 90.0], stray     # manual run: zip(stray, events) holds
    run = "20260922T021858Z_i45_d25000_t15000_c0.8_int8_demo_normal"
    assert mode_text(run, None)[0].endswith("(waiting for Tier 2)")
    assert mode_text(run, "# param DORMANCY=15000")[0] == "NORMAL: halts after 15 s idle"
    assert mode_text(run.replace("t15000", "t-1"), "# param DORMANCY=-1")[0].startswith("ALWAYS ON")
    assert "did NOT confirm" in mode_text(run, "# WARN DORMANCY not acknowledged -- ...")[0]
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        old, new = Path(tmp, "old"), Path(tmp, "new")
        for d, mtime in ((old, 1000.0), (new, 2000.0)):
            d.mkdir()
            (d / "power.csv").write_text("")
            os.utime(d / "power.csv", (mtime, mtime))
        assert newest_run(Path(tmp)) == new
        assert newest_run(Path(tmp), since=1500.0) == new
        assert newest_run(Path(tmp), since=2500.0) is None   # stale runs are ignored
    print("self-test OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
