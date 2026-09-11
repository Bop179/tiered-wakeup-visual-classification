#!/usr/bin/env python3
"""One command = one matrix cell. Run this, not the individual tools.

    tools/run_experiment.py --mean-interval 45 --duration-ms 15000 \
                            --dormancy-ms 30000 --contrast 0.8 --n-events 40
    tools/run_experiment.py --self-test       # end-of-run logic against a simulated Pi

Hand-running 24 conditions in week two will not fit, and a run assembled by hand
is a run whose parameters nobody can reconstruct afterwards. This script is what
makes the matrix affordable:

  1. record the git SHA and the model's SHA256 in data/<run_id>/
  2. measure the Mac<->Pi clock offset (ssh date), at the START
  3. start tools/fnb58_logger.py        -> power.csv
  4. write data/current_run.env on the Pi and start tier3-daemon.service, which
     runs pi/pi_daemon.py -> events.csv; wait for its "# ready"
  5. run tools/event_display.py         -> gen.csv     [blocks until done]
  6. stop the logger cleanly via the stop-file, so no CSV line is truncated
  7. release the Pi (below), then measure the clock offset again, at the END
  8. scp events.csv, daemon.log and boots.csv back
  9. write manifest.json and append a skeleton row to docs/EXPERIMENTS.md

A run without a manifest is a run that did not happen.

The daemon across halts, and the end of a run
---------------------------------------------
A halt ends the daemon. tier3-daemon.service -- install it once with
pi/install_service.sh -- restarts it at every boot while data/current_run.env
exists, appending to the same events.csv. Without the unit every event after a
cell's first halt is lost; --daemon-mode nohup is the old launch, kept only for a
Pi that does not have it yet.

Releasing the Pi: stopping the unit makes the daemon SET DORMANCY -1 on its way
out, so Tier 2 stops halting the Pi and it is still up for the next cell. A
short-dormancy cell ends with the Pi already halted, though, and then nothing
reaches it over the network. So this wakes it the way Tier 2 does -- a flash of
the trigger patch, via tools/trigger_patch.py. The unit restarts the daemon on
that boot, the daemon drains the event Tier 2 was holding (so it cannot leak into
the next cell as a phantom first result), and only then is the unit stopped.
Events from that post-run boot are moved out of events.csv into
events_post_run.csv, using boots.csv.

Clock note: the ssh offsets are a CROSS-CHECK, not the alignment. ssh latency is
high and asymmetric, so these are good to maybe tens of ms. The trusted anchor is
the clapperboard -- the daemon's 2 s full-core burn at t=0, which appears in the
power trace as an unmistakable step and pins the two clocks to within one sample.
If the two disagree by more than 100 ms, the clapperboard wins. The Pi has no RTC,
so after a post-run wake the END offset can be wrong until NTP resyncs; the
manifest flags it when that happened.

Dormancy is SET on Tier 2 by the daemon and read back from the CFG reply, so the
manifest records dormancy_ms_verified. --confirm-dormancy is only for firmware that
predates SET/GET.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STOP_FILE = REPO / "fnirsi_stop"
UNIT = "tier3-daemon.service"

POLL_S = 5.0
READY_TIMEOUT_S = 90.0
# Unreachable this long before the first wake flash: HALT_SETTLE_MS (20 s, when
# the wake line is ignored) plus shutdown, and long enough that a boot caused by a
# real, late event would already have come up.
DOWN_BEFORE_FLASH_S = 45.0
BOOT_WAIT_S = 120.0           # after a flash, before trying another
DRAIN_WAIT_S = 90.0           # post-run boot: wait for the woken event to be logged
MID_HALT_CHECK_S = 10.0
MAX_WAKE_FLASHES = 3
RELEASE_DEADLINE_S = 15 * 60


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def git_sha() -> str:
    r = sh(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"])
    dirty = sh(["git", "-C", str(REPO), "status", "--porcelain"]).stdout.strip()
    return (r.stdout.strip() or "unknown") + ("-dirty" if dirty else "")


def sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:16]


def clock_offset(host: str, timeout: float = 10.0) -> dict | None:
    """Pi clock minus Mac clock, seconds. Cross-check only -- see the docstring."""
    t0 = time.time()
    r = sh(["ssh", "-o", "BatchMode=yes", f"-o", f"ConnectTimeout={int(timeout)}",
            host, "date +%s.%N"], timeout=timeout + 5)
    t1 = time.time()
    if r.returncode != 0:
        return None
    try:
        t_pi = float(r.stdout.strip())
    except ValueError:
        return None
    return {"t_mac_mid": (t0 + t1) / 2, "t_pi": t_pi,
            "offset_s": t_pi - (t0 + t1) / 2, "rtt_s": t1 - t0}


def _insert_run_row(log: Path, row: str) -> bool:
    """Put `row` into the run-log table in EXPERIMENTS.md, not at end of file.

    The table sits under '## Run log' and is followed by more sections, so a
    plain append lands outside it and renders as loose text. Find the table,
    drop the placeholder row if it is still there, and insert after the last
    real row. Returns False if no such table exists, so the caller can say so
    rather than silently losing the row.
    """
    lines = log.read_text().splitlines()

    start = next((i for i, ln in enumerate(lines)
                  if ln.strip().lower().startswith("## run log")), None)
    if start is None:
        return False

    # The table is the first block of '|' lines after the heading. Stop at the
    # next heading so a later table is never mistaken for this one.
    first = last = None
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("#"):
            break
        if stripped.startswith("|"):
            if first is None:
                first = i
            last = i
        elif first is not None and stripped:
            break                      # non-empty, non-table line ends the table
    if first is None or last is None or last - first < 1:
        return False                   # header + separator is the minimum

    if "_(first run appends here)_" in lines[last]:
        lines[last] = row              # replace the placeholder
    else:
        lines.insert(last + 1, row)

    log.write_text("\n".join(lines) + "\n")
    return True


# ------------------------------------------------------------------- the Pi

class RemotePi:
    """The few things this script needs from the Pi, over ssh."""

    def __init__(self, host: str, repo: str):
        self.host, self.repo = host, repo

    def run(self, cmd: str, timeout: float = 20.0,
            input: str | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                                   self.host, cmd], capture_output=True, text=True,
                                  timeout=timeout, input=input)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, 255, "", "ssh timed out")

    def reachable(self) -> bool:
        return self.run("true", timeout=12).returncode == 0


def wait_ready(pi, remote_dir: str, timeout: float,
               now=time.time, sleep=time.sleep) -> bool:
    deadline = now() + timeout
    while now() < deadline:
        if pi.run(f"grep -q '^# ready' {remote_dir}/daemon.log").returncode == 0:
            return True
        sleep(1.0)
    return False


def wait_drain(pi, remote_dir: str, boot_id: str, timeout: float,
               now=time.time, sleep=time.sleep) -> bool:
    """True once the daemon started in `boot_id` has logged an event."""
    deadline = now() + timeout
    while now() < deadline:
        lines = pi.run(f"tail -n 1 {remote_dir}/boots.csv; "
                       f"tail -n +2 {remote_dir}/events.csv | wc -l").stdout.split()
        if len(lines) >= 2:
            row = lines[0].split(",")
            try:
                if row[1] == boot_id and int(lines[-1]) > int(row[3]):
                    return True
            except (IndexError, ValueError):
                pass
        sleep(POLL_S)
    return False


def release_pi(pi, run_id: str, flash, log=print,
               now=time.time, sleep=time.sleep) -> dict:
    """Stop this run's daemon and leave the Pi up for the next run. See the docstring."""
    d = f"{pi.repo}/data/{run_id}"
    info = {"released": False, "wake_flashes": 0, "post_run_boot_ids": [],
            "exit_dormancy_acked": None, "note": ""}
    deadline = now() + RELEASE_DEADLINE_S
    down_since = last_flash = None

    while now() < deadline:
        if not pi.reachable():
            down_since = down_since if down_since is not None else now()
            since = now() - (last_flash if last_flash is not None else down_since)
            if since >= (BOOT_WAIT_S if last_flash is not None else DOWN_BEFORE_FLASH_S):
                if info["wake_flashes"] >= MAX_WAKE_FLASHES:
                    info["note"] = (f"Pi still unreachable after {MAX_WAKE_FLASHES} wake "
                                    f"flashes -- did Tier 1 see them?")
                    break
                log(f"  Pi unreachable for {since:.0f} s: flashing the trigger patch to wake it")
                flash()
                info["wake_flashes"] += 1
                last_flash = now()
            sleep(POLL_S)
            continue
        down_since = None

        if last_flash is not None:
            boot_id = pi.run("cat /proc/sys/kernel/random/boot_id").stdout.strip()
            if boot_id and boot_id not in info["post_run_boot_ids"]:
                info["post_run_boot_ids"].append(boot_id)
                log("  Pi is back; waiting for the daemon to drain the woken event")
                if not wait_drain(pi, d, boot_id, DRAIN_WAIT_S, now, sleep):
                    log("  WARNING: the post-run boot never logged an event. Tier 2 may "
                        "still hold one, and the next run could receive it first.")

        r = pi.run(f"a=$(grep -c exit-dormancy {d}/daemon.log 2>/dev/null); "
                   f"sudo systemctl stop {UNIT}; "
                   f"b=$(grep -c exit-dormancy {d}/daemon.log 2>/dev/null); "
                   f"echo \"${{a:-0}} ${{b:-0}}\"; "
                   f"grep exit-dormancy {d}/daemon.log 2>/dev/null | tail -n 1", timeout=40)
        lines = r.stdout.strip().splitlines()
        try:
            before, after = (int(x) for x in lines[0].split())
        except (IndexError, ValueError):
            before = after = 0
        if after > before:
            info["exit_dormancy_acked"] = lines[-1].startswith("# exit-dormancy DORMANCY=-1")
            if not info["exit_dormancy_acked"]:
                info["note"] = "Tier 2 did not acknowledge SET,DORMANCY,-1; it may still halt the Pi"
        else:
            # No exit line: either the daemon had just taken a HALT, or it was not
            # running. A Pi that is halting drops off the network within seconds.
            sleep(MID_HALT_CHECK_S)
            if not pi.reachable():
                log("  the Pi was halting as the run ended; waiting for it to go down")
                continue
            info["note"] = "the daemon was not running when the run ended"
        pi.run(f"rm -f {pi.repo}/data/current_run.env")
        info["released"] = True
        break
    return info


def split_post_run(run_dir: Path, boot_ids: list[str]) -> int:
    """Move the events a post-run wake produced from events.csv to events_post_run.csv."""
    boots_path, events_path = run_dir / "boots.csv", run_dir / "events.csv"
    if not boots_path.exists() or not events_path.exists():
        return 0
    with open(boots_path, newline="") as fh:
        firsts = [int(r["first_event_idx"]) for r in csv.DictReader(fh)
                  if r.get("boot_id") in boot_ids]
    if not firsts:
        return 0
    cut = min(firsts)
    with open(events_path, newline="") as fh:
        rows = list(csv.reader(fh))
    header, body = rows[0], rows[1:]
    idx = header.index("event_idx")
    keep = [r for r in body if int(r[idx]) < cut]
    post = [r for r in body if int(r[idx]) >= cut]
    for path, part in ((events_path, keep), (run_dir / "events_post_run.csv", post)):
        with open(path, "w", newline="") as fh:
            csv.writer(fh).writerows([header] + part)
    return len(post)


def wake_flash(args) -> None:
    subprocess.call([sys.executable, str(REPO / "tools" / "trigger_patch.py"), "flash",
                     "--count", "1", "--contrast", "1", "--flash-ms", "2000",
                     "--lead-in", "1", "--gap-s", "0.5", "--jitter-s", "0",
                     "--display", str(args.display)], cwd=REPO)


# ------------------------------------------------------------------- self-test

def self_test() -> int:
    """release_pi() and split_post_run() against a simulated Pi and clock."""
    class Clock:
        def __init__(self):
            self.t = 0.0

        def now(self):
            return self.t

        def sleep(self, s):
            self.t += s

    class FakePi:
        def __init__(self, clock, halted=False, flash_wakes=True, halting_on_stop=False):
            self.repo, self.c = "~/repo", clock
            self.state = "halted" if halted else "up"
            self.flash_wakes, self.halting_on_stop = flash_wakes, halting_on_stop
            self.boot_id, self.boots, self.events = "boot-A", [("boot-A", 0)], 40
            self.unit_active, self.exit_lines, self.pointer = not halted, 0, True
            self.boot_at = self.drain_at = None
            self.stops, self.flash_times = 0, []

        def _tick(self):
            if self.state == "booting" and self.c.t >= self.boot_at + 30:
                self.state, self.boot_id = "up", f"boot-{len(self.boots)}"
                self.boots.append((self.boot_id, self.events))
                self.unit_active, self.drain_at = True, self.c.t + 4
            if self.drain_at is not None and self.c.t >= self.drain_at:
                self.events, self.drain_at = self.events + 1, None

        def reachable(self):
            self._tick()
            return self.state == "up"

        def flash(self):
            self.flash_times.append(self.c.t)
            if self.state == "halted" and self.flash_wakes:
                self.state, self.boot_at = "booting", self.c.t

        def run(self, cmd, timeout=20.0, input=None):
            self._tick()
            if self.state != "up":
                return subprocess.CompletedProcess(cmd, 255, "", "unreachable")
            out = ""
            if "random/boot_id" in cmd:
                out = self.boot_id + "\n"
            elif "boots.csv" in cmd:
                bid, first = self.boots[-1]
                out = f"1.0,{bid},5.0,{first},99\n{self.events}\n"
            elif "systemctl stop" in cmd:
                a = self.exit_lines
                self.stops += 1
                if self.halting_on_stop and self.stops == 1:
                    self.unit_active, self.state = False, "halted"
                    out = f"{a} {a}\n"
                else:
                    if self.unit_active:
                        self.exit_lines += 1
                        self.unit_active = False
                    out = f"{a} {self.exit_lines}\n# exit-dormancy DORMANCY=-1\n"
            elif "rm -f" in cmd:
                self.pointer = False
            return subprocess.CompletedProcess(cmd, 0, out, "")

    failures = []

    def check(name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    def release(**kw):
        c = Clock()
        p = FakePi(c, **kw)
        info = release_pi(p, "run", flash=p.flash, log=lambda *a: None,
                          now=c.now, sleep=c.sleep)
        return p, info

    p, info = release()
    check("Pi up at the end: one stop, no flash, pointer removed",
          info["released"] and info["wake_flashes"] == 0 and p.stops == 1
          and not p.pointer and info["exit_dormancy_acked"], str(info))

    p, info = release(halted=True)
    check("Pi halted at the end: one flash, drained, then stopped",
          info["released"] and info["wake_flashes"] == 1 and p.events == 41
          and info["post_run_boot_ids"] == ["boot-1"] and p.stops == 1 and not p.pointer,
          str(info))
    check("the first flash waits out Tier 2's HALT settle",
          p.flash_times and p.flash_times[0] >= DOWN_BEFORE_FLASH_S, str(p.flash_times))

    p, info = release(halting_on_stop=True)
    check("halting as the run ended: waits, wakes it, drains, stops",
          info["released"] and info["wake_flashes"] == 1 and p.stops == 2
          and p.events == 41, str(info))

    p, info = release(halted=True, flash_wakes=False)
    check("a flash Tier 1 never sees: gives up after three and says so",
          not info["released"] and info["wake_flashes"] == MAX_WAKE_FLASHES
          and "unreachable" in info["note"] and p.pointer, str(info))

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "events.csv").write_text("t_pi,event_idx,class_id\n1.0,0,5\n2.0,1,5\n3.0,2,5\n")
        (d / "boots.csv").write_text("t_pi,boot_id,uptime_s,first_event_idx,pid\n"
                                     "0.0,A,9.0,0,1\n5.0,B,21.0,2,2\n")
        moved = split_post_run(d, ["B"])
        kept = (d / "events.csv").read_text().splitlines()
        post = (d / "events_post_run.csv").read_text().splitlines()
        check("post-run events move out of events.csv",
              moved == 1 and len(kept) == 3 and post[1:] == ["3.0,2,5"], f"{kept} | {post}")

    print("\nPASS: end-of-run release logic" if not failures
          else f"\nFAIL: {len(failures)} check(s)")
    return 1 if failures else 0


# ------------------------------------------------------------------- main

def main() -> int:
    if "--self-test" in sys.argv[1:]:
        return self_test()

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_argument_group("swept parameters")
    g.add_argument("--mean-interval", type=float, required=True,
                   help="mean black dwell, seconds -- sets the event rate")
    g.add_argument("--duration-ms", type=int, default=15000)
    g.add_argument("--contrast", type=float, default=0.8)
    g.add_argument("--dormancy-ms", type=int, required=True,
                   help="SET on Tier 2 over serial at run start. -1 = never halt")
    g.add_argument("--n-events", type=int, default=40)
    g.add_argument("--model", choices=["int8", "fp32"], default="int8")
    g.add_argument("--dwell-dist", choices=["exponential", "fixed"],
                   default="exponential")
    g.add_argument("--flicker-rate", type=float, default=0.0)
    g.add_argument("--flicker-contrast", type=float, default=0.15)
    g.add_argument("--trimmer", default="",
                   help="free text: Tier 1 trimmer position, for the ROC sweep")

    ap.add_argument("--host", default=os.environ.get("PI_HOST", "pi"),
                    help="ssh target for the Pi (default: $PI_HOST or 'pi')")
    ap.add_argument("--pi-repo", default="~/tiered-wakeup-visual-classification")
    ap.add_argument("--daemon-mode", choices=["unit", "nohup"], default="unit",
                    help="unit: tier3-daemon.service, which survives halts (default). "
                         "nohup: loses every event after the first halt")
    ap.add_argument("--display", type=int, default=0)
    ap.add_argument("--images", type=Path, default=REPO / "images")
    ap.add_argument("--target-class", default="banana")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--data-dir", type=Path, default=REPO / "data")
    ap.add_argument("--note", default="", help="free text into the manifest and log")
    ap.add_argument("--tag", default="", help="short slug in the run id")

    ap.add_argument("--no-power", action="store_true", help="skip the FNB58 logger")
    ap.add_argument("--no-daemon", action="store_true",
                    help="do not start the daemon over ssh; assume it is running")
    ap.add_argument("--no-clock", action="store_true", help="skip the ssh offsets")
    ap.add_argument("--confirm-dormancy", action="store_true",
                    help="stop and ask, for firmware that predates SET/GET")
    ap.add_argument("--dry-run", action="store_true",
                    help="stimulus schedule and manifest only; no hardware touched")
    ap.add_argument("--self-test", action="store_true",
                    help="end-of-run logic against a simulated Pi; no hardware")
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = (f"i{args.mean_interval:g}_d{args.duration_ms}_"
            f"t{args.dormancy_ms}_c{args.contrast:g}_{args.model}")
    run_id = f"{stamp}_{slug}" + (f"_{args.tag}" if args.tag else "")
    run_dir = args.data_dir / run_id
    pi = RemotePi(args.host, args.pi_repo)
    remote_dir = f"{args.pi_repo}/data/{run_id}"
    use_pi = not args.no_daemon and not args.dry_run

    if use_pi:
        if not pi.reachable():
            print(f"cannot reach {args.host} over ssh. If the last cell left the Pi halted, "
                  f"wake it with\n  tools/trigger_patch.py flash --count 1 --contrast 1\n"
                  f"and run this again once `ssh {args.host} true` works.", file=sys.stderr)
            return 1
        if (args.daemon_mode == "unit" and "installed" not in
                pi.run(f"systemctl cat {UNIT} >/dev/null 2>&1 && echo installed").stdout):
            print(f"{UNIT} is not installed on {args.host}. On the Pi, once:\n"
                  f"  bash pi/install_service.sh\n"
                  f"or pass --daemon-mode nohup, which loses every event after the first halt.",
                  file=sys.stderr)
            return 1

    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"run {run_id}\n  -> {run_dir}")

    if args.confirm_dormancy and not args.dry_run:
        want = "never" if args.dormancy_ms < 0 else f"{args.dormancy_ms} ms"
        if input(f"Is DORMANCY_MS on the Arduino set to {want}? [y/N] ").lower() != "y":
            return 1

    manifest = {
        "run_id": run_id,
        "started_utc": stamp,
        "git_sha": git_sha(),
        "params": {k: getattr(args, k) for k in
                   ("mean_interval", "duration_ms", "contrast", "dormancy_ms",
                    "n_events", "model", "dwell_dist", "flicker_rate",
                    "flicker_contrast", "trimmer", "seed", "target_class")},
        "host": args.host,
        "daemon_mode": args.daemon_mode,
        "note": args.note,
        "model_sha256": sha256(REPO / "pi" / "models" /
                               ("mobilenet_v2_1.0_224_quant.tflite" if args.model == "int8"
                                else "mobilenet_v2_1.0_224.tflite")),
        "dry_run": args.dry_run,
    }

    if not args.no_clock and not args.dry_run:
        manifest["clock_start"] = clock_offset(args.host)
        print(f"  clock offset (start): "
              f"{manifest['clock_start']['offset_s']:+.3f} s"
              if manifest.get("clock_start") else
              "  clock offset (start): ssh failed -- clapperboard only")

    procs: list[tuple[str, subprocess.Popen]] = []
    if STOP_FILE.exists():
        STOP_FILE.unlink()

    try:
        if not args.no_power and not args.dry_run:
            cmd = [sys.executable, str(REPO / "tools" / "fnb58_logger.py"),
                   "-o", str(run_dir / "power.csv"),
                   "--stop-file", str(STOP_FILE)]
            print(f"  power: {' '.join(cmd)}")
            procs.append(("power", subprocess.Popen(cmd, cwd=REPO)))
            time.sleep(2.0)     # let the meter stream before the clapperboard

        if use_pi:
            if args.daemon_mode == "unit":
                pointer = "".join(f"{k}={shlex.quote(str(v))}\n" for k, v in (
                    ("RUN_ID", run_id), ("MODEL", args.model),
                    ("TARGET_CLASS", args.target_class), ("DORMANCY_MS", args.dormancy_ms)))
                # Stop any daemon a previous run left behind before re-pointing the unit.
                pi.run(f"sudo systemctl stop {UNIT}; mkdir -p {remote_dir} && "
                       f"cat > {args.pi_repo}/data/current_run.env", input=pointer, timeout=40)
                r = pi.run(f"sudo systemctl start {UNIT}", timeout=30)
                print(f"  daemon: {UNIT} -> {remote_dir}"
                      + ("" if r.returncode == 0 else f"  (start failed: {r.stderr.strip()})"))
            else:
                remote = (f"cd {args.pi_repo} && "
                          f"nohup .venv/bin/python pi/pi_daemon.py "
                          f"--model {args.model} --target-class {shlex.quote(args.target_class)} "
                          f"--dormancy-ms {args.dormancy_ms} "
                          f"--out data/{run_id}/events.csv "
                          f"> data/{run_id}/daemon.log 2>&1 &")
                print(f"  daemon (nohup): ssh {args.host} '{remote}'")
                pi.run(f"mkdir -p {remote_dir}")
                pi.run(remote)
            if wait_ready(pi, remote_dir, READY_TIMEOUT_S):
                print("  daemon ready")
            else:
                print(f"  WARNING: no '# ready' in {remote_dir}/daemon.log after "
                      f"{READY_TIMEOUT_S:.0f} s; the first events may be missed", file=sys.stderr)

        cmd = [sys.executable, str(REPO / "tools" / "event_display.py"),
               "--n-events", str(args.n_events),
               "--mean-interval", str(args.mean_interval),
               "--dwell-dist", args.dwell_dist,
               "--duration-ms", str(args.duration_ms),
               "--contrast", str(args.contrast),
               "--flicker-rate", str(args.flicker_rate),
               "--flicker-contrast", str(args.flicker_contrast),
               "--target-class", args.target_class,
               "--images", str(args.images),
               "--display", str(args.display),
               "--seed", str(args.seed),
               "-o", str(run_dir / "gen.csv")]
        if args.dry_run:
            cmd.append("--dry-run")
        print(f"  stimulus: {' '.join(cmd)}\n")
        rc = subprocess.call(cmd, cwd=REPO)
        if rc != 0:
            print(f"  stimulus exited {rc}", file=sys.stderr)

    finally:
        if any(name == "power" for name, _ in procs):
            STOP_FILE.touch()
            time.sleep(1.5)
        for name, proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.terminate()
        if STOP_FILE.exists():
            STOP_FILE.unlink()

    release = None
    if use_pi:
        if args.daemon_mode == "unit":
            print("  releasing the Pi")
            release = release_pi(pi, run_id, flash=lambda: wake_flash(args))
            manifest["pi_release"] = release
            if not release["released"]:
                print(f"  WARNING: could not release the Pi: {release['note']}\n"
                      f"  By hand, once it is up:  ssh {args.host} 'sudo systemctl stop {UNIT}; "
                      f"rm -f {args.pi_repo}/data/current_run.env'", file=sys.stderr)
            elif release["note"]:
                print(f"  note: {release['note']}")
        else:
            pi.run("pkill -INT -f pi/pi_daemon.py || true")

    if not args.no_clock and not args.dry_run:
        manifest["clock_end"] = clock_offset(args.host)
        if release and release["wake_flashes"]:
            manifest["clock_end_note"] = ("taken after a post-run wake: the Pi has no RTC, "
                                          "so this offset can be wrong until NTP resyncs")
        if manifest.get("clock_start") and manifest.get("clock_end"):
            drift = (manifest["clock_end"]["offset_s"]
                     - manifest["clock_start"]["offset_s"])
            manifest["clock_drift_s"] = drift
            print(f"  clock drift over the run: {drift:+.3f} s")
            if abs(drift) > 0.1:
                print("  NOTE: >100 ms of drift. Interpolate linearly between the"
                      "\n        two offsets, and trust the clapperboard over both.")

    if use_pi:
        src = f"{args.host}:{remote_dir}/"
        names = ["events.csv", "daemon.log"] + (["boots.csv"] if args.daemon_mode == "unit" else [])
        print(f"  fetching {src}")
        r = sh(["scp", "-q", *[src + n for n in names], str(run_dir)])
        if r.returncode != 0:
            print(f"  scp failed: {r.stderr.strip()}\n"
                  f"  fetch by hand:  scp '{src}*' {run_dir}/", file=sys.stderr)
        if release and release["post_run_boot_ids"]:
            moved = split_post_run(run_dir, release["post_run_boot_ids"])
            manifest["post_run_events"] = moved
            if moved:
                print(f"  moved {moved} event(s) from the post-run wake to events_post_run.csv")

    # The value Tier 2 says is IN EFFECT, read back from its CFG reply. This is
    # the number that goes in the record -- --dormancy-ms is only what we asked
    # for, and a clamped or unacknowledged SET would otherwise be invisible.
    log = run_dir / "daemon.log"
    if log.exists():
        for line in log.read_text().splitlines():
            if line.startswith("# param DORMANCY="):
                try:
                    verified = int(line.split("=")[1].split()[0])
                except (IndexError, ValueError):
                    break
                manifest["dormancy_ms_verified"] = verified
                if verified != args.dormancy_ms:
                    print(f"  WARNING: asked for dormancy {args.dormancy_ms} ms but "
                          f"Tier 2 reports {verified} ms in effect. The manifest "
                          f"records {verified}.")
                break
            if line.startswith("# WARN DORMANCY not acknowledged"):
                manifest["dormancy_ms_verified"] = None
                print("  WARNING: Tier 2 did not acknowledge SET,DORMANCY. The "
                      "firmware may predate SET/GET, in which case the dormancy in "
                      "effect is whatever was last flashed and this run's parameter "
                      "is UNVERIFIED.")
                break

    manifest["finished_utc"] = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest["files"] = sorted(p.name for p in run_dir.iterdir())
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\n  manifest: {run_dir / 'manifest.json'}")
    print(f"  files: {', '.join(manifest['files'])}")

    log = REPO / "docs" / "EXPERIMENTS.md"
    if log.exists():
        row = (f"| `{run_id}` | {stamp[:8]} | | {args.dormancy_ms} | "
               f"{args.mean_interval:g} s | {args.duration_ms} | {args.contrast:g} | "
               f"{args.model} | {args.n_events} | | | {args.note} |")
        if _insert_run_row(log, row):
            print(f"  logged a row in {log.relative_to(REPO)} -- fill in the results")
        else:
            print(f"  WARNING: no run-log table found in {log.relative_to(REPO)}; "
                  f"row not written. Add it by hand:\n    {row}")

    print(f"\nnext:  analysis/energy_analysis.py {run_dir}"
          f"\n       analysis/accuracy.py {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
