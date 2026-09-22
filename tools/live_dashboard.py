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
  BOOT TIME  runs from the power rise until boots.csv's last row carries the
             running kernel's boot_id. (sshd is up before the daemon, so a row
             from the previous boot must not count; the kernel id rules it out.)
             The daemon writes the row once the model and camera are loaded, just
             before "# ready", so this is wake -> ready as the audience sees it.
  RESULT     the last rows of the Pi's events.csv, fetched over ssh while it is up.
             While it is halted the last result stays on screen.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONSTANTS = json.loads((REPO / "data" / "constants.json").read_text())
MEAN_WINDOW_S = 0.5


class PiState:
    """Halted / booting / awake from the power trace plus boots.csv."""

    def __init__(self, halt_w: float):
        self.halt_w = halt_w
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

    def on_ready(self, boot_id: str, t: float) -> None:
        """The daemon of kernel boot `boot_id` is up (its boots.csv row exists)."""
        if boot_id == self.boot_id:
            return
        self.boot_id = boot_id
        if self.state == "BOOTING":
            self.last_boot_s = t - self.t_rise
            self.state, self.t_rise = "AWAKE", None


class PowerTail:
    """Follows a CSV that is still being written; keeps the last window_s of samples."""

    def __init__(self, path: Path, window_s: float):
        self.path = path
        self.fh = None
        self.buf = ""
        self.samples = deque(maxlen=int(window_s * 100))   # FNB58 streams 100 Hz
        self.e0 = self.t0 = None
        self.e_last = self.t_last = None

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
        self.boot_id = None
        self.t_boot_seen = None
        self.ok = False
        self.lock = threading.Lock()

    def follow(self, remote_dir: str) -> None:
        with self.lock:
            self.remote_dir, self.events, self.boot_id, self.ok = remote_dir, [], None, False

    def run(self) -> None:
        while True:
            d = self.remote_dir
            cmd = (f"cd {d} && head -n 1 events.csv && tail -n +2 events.csv | tail -n 6; "
                   f"echo ---; cat /proc/sys/kernel/random/boot_id; tail -n 1 boots.csv")
            try:
                r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2",
                                    self.host, cmd], capture_output=True, text=True,
                                   timeout=6)
                t = time.time()
                ev_txt, _, boot_txt = r.stdout.partition("---\n")
                events = list(csv.DictReader(io.StringIO(ev_txt)))
                kernel_id, _, row = boot_txt.partition("\n")
                row_id = row.split(",")[1] if row.count(",") >= 2 else None
                with self.lock:
                    if d != self.remote_dir:
                        continue                  # switched runs mid-fetch; drop it
                    self.ok = r.returncode == 0
                    if events:
                        self.events = events
                    if row_id and row_id == kernel_id.strip() != self.boot_id:
                        self.boot_id, self.t_boot_seen = row_id, t
            except subprocess.TimeoutExpired:
                with self.lock:
                    self.ok = False
            time.sleep(self.period_s)


def newest_run(data: Path) -> Path | None:
    runs = [p.parent for p in data.glob("*/power.csv")]
    return max(runs, key=lambda p: (p / "power.csv").stat().st_mtime, default=None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", type=Path, help="run dir (default: newest data/*/power.csv)")
    ap.add_argument("--host", default="pi", help="ssh target for the Pi")
    ap.add_argument("--pi-repo", default="~/tiered-wakeup-visual-classification")
    ap.add_argument("--window", type=float, default=120, help="seconds of power shown")
    # Calibration knob. Halted is a flat ~2.00 W, but the first ~10 s of a boot sit at
    # 2.5-2.9 W, so the midpoint to idle (2.6 W) flickers all through the boot. Sit
    # just above halted instead. Move it if the rig's halted draw changes.
    ap.add_argument("--halt-w", type=float, default=CONSTANTS["p_halt"] + 0.15)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    run = args.run or newest_run(REPO / "data")
    while run is None:
        print("waiting for a run to start writing data/<run_id>/power.csv ...")
        time.sleep(2)
        run = newest_run(REPO / "data")
    remote = RemoteTail(args.host, "")
    cur = {}

    def follow(run: Path) -> None:
        # Without --run, a newer run that starts while this is open takes over, so
        # the dashboard can be left up across demo runs.
        print(f"following {run.name}")
        cur.update(run=run, power=PowerTail(run / "power.csv", args.window),
                   pi=PiState(args.halt_w), checked=time.time())
        remote.follow(f"{args.pi_repo}/data/{run.name}")

    follow(run)
    remote.start()

    plt.rcParams.update({"font.size": 13, "toolbar": "None"})
    fig = plt.figure(figsize=(13, 7.5))
    fig.canvas.manager.set_window_title("Tiered wake-up: live")
    gs = fig.add_gridspec(2, 2, height_ratios=[1, 1.3], hspace=0.35, wspace=0.15)
    ax_state, ax_res, ax_p = (fig.add_subplot(gs[0, 0]), fig.add_subplot(gs[0, 1]),
                              fig.add_subplot(gs[1, :]))
    for ax in (ax_state, ax_res):
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
    colors = {"AWAKE": "#2f855a", "BOOTING": "#c05621", "HALTED": "#4a5568"}

    def tick(_frame):
        if not args.run and time.time() - cur["checked"] > 2:
            cur["checked"] = time.time()
            newest = newest_run(REPO / "data")
            if newest and newest != cur["run"]:
                follow(newest)
        power, pi = cur["power"], cur["pi"]
        power.poll()
        with remote.lock:
            events, boot_id, t_seen, ok = (remote.events, remote.boot_id,
                                           remote.t_boot_seen, remote.ok)
        mean = power.mean_w()
        if mean is not None:
            pi.on_power(power.samples[-1][0], mean)
        if boot_id:
            pi.on_ready(boot_id, t_seen)

        if power.samples:
            t_end = power.samples[-1][0]
            line.set_data([t - t_end for t, _ in power.samples],
                          [w for _, w in power.samples])
        s = pi.state or "NO METER DATA"
        state_txt.set_text(f"Pi {s}")
        state_txt.set_color(colors.get(s, "black"))
        sub = []
        if pi.state == "BOOTING":
            sub.append(f"booting for {time.time() - pi.t_rise:5.1f} s")
        if pi.last_boot_s is not None:
            sub.append(f"last boot   {pi.last_boot_s:5.1f} s  (wake -> ready)")
        if mean is not None:
            sub.append(f"power now   {mean:5.2f} W")
        if power.e0 is not None:
            used = power.e_last - power.e0
            always_on = CONSTANTS["p_idle"] * (power.t_last - power.t0)
            sub.append(f"energy      {used:5.0f} J")
            sub.append(f"always-on   {always_on:5.0f} J  (idle, never halts)")
        state_sub.set_text("\n".join(sub))

        if events:
            e = events[-1]
            name = e["class_name"].split(",")[0]
            res_txt.set_text("no confident class" if e["class_id"] == "-1"
                             else f"{name}  {float(e['confidence']):.0%}")
            rows = [f"{'#':>3} {'class':<18} {'conf':>5} {'lat ms':>7}"]
            for r in events[::-1]:
                rows.append(f"{r['event_idx']:>3} {r['class_name'].split(',')[0][:18]:<18} "
                            f"{float(r['confidence']):5.2f} {r['latency_ms']:>7}")
            res_sub.set_text("\n".join(rows))
        else:
            res_txt.set_text("waiting for first event")
        res_txt.set_color("black" if ok else "#718096")   # grey = Pi unreachable
        return line, state_txt, state_sub, res_txt, res_sub

    _anim = FuncAnimation(fig, tick, interval=250, cache_frame_data=False)
    plt.show()
    return 0


def self_test() -> int:
    p = PiState(halt_w=2.6)
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
    p.on_ready("A", 50.0)                      # stale id cannot end the boot
    assert p.state == "BOOTING"
    p.on_ready("B", 61.0)
    assert p.state == "AWAKE" and abs(p.last_boot_s - 21.0) < 1e-9
    p.on_power(70.0, 1.9)
    assert p.state == "HALTED" and p.t_rise is None

    q = PiState(halt_w=2.6)                    # dashboard started while halted
    q.on_power(0.0, 2.0)
    q.on_power(5.0, 3.3)
    q.on_ready("C", 25.0)
    assert q.state == "AWAKE" and abs(q.last_boot_s - 20.0) < 1e-9
    print("self-test OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
