#!/usr/bin/env python3
"""Tier 3 daemon: serial listener -> capture -> classify -> RES, and halt on demand.

    pi/pi_daemon.py --port /dev/serial0 --model int8 --out data/<run_id>/events.csv
    pi/pi_daemon.py --port /dev/ttys00X --no-camera --no-halt   # against a mock

Speaks the protocol frozen in docs/INTERFACE.md. Summary of this side's duties:

  EVT,<t_ms>,<peak>,<duration_ms>  ->  ACK immediately (before capture), then
                                       RES,<class_id>,<confidence>,<latency_ms>
  HALT                             ->  ACK, "# halting", then `sudo halt`
  SYNC,<t_ms>                      ->  reply SYNC,<our t_ms>
  # anything                       ->  ignore
  malformed                        ->  drop silently, never block

On start it burns 2 s of 100% CPU on every core -- the "clapperboard". That leaves
an unmistakable square step in the FNB58 trace, which aligns the Mac's power log
to Pi time to within one sample without trusting NTP at all. The burn timestamp
goes into the log; analysis/energy_analysis.py looks for the step.

Pi serial setup, once, and it matters
-------------------------------------
    sudo raspi-config  ->  Interface Options  ->  Serial Port
        login shell over serial: NO      (or getty eats every byte)
        serial hardware enabled: YES

Then add to /boot/firmware/config.txt:

    dtoverlay=disable-bt

**Do not skip that line.** By default /dev/serial0 on a Pi 4 is the *mini-UART*,
whose baud rate is derived from the VPU core clock -- so it drifts when the clock
scales. This daemon deliberately drives the CPU to 100% (clapperboard, then every
inference), which is exactly the condition that shifts the core clock and corrupts
the link. `disable-bt` moves the proper PL011 onto GPIO14/15 and makes the baud
rate independent of CPU load. Alternatively pin `core_freq_min=500`, but moving
the PL011 is the fix that cannot come undone under load.

Wake from halt, also once:

    sudo rpi-eeprom-config      # want WAKE_ON_GPIO=1 and POWER_OFF_ON_HALT=0

POWER_OFF_ON_HALT=1 saves more power but disables GPIO3 wake entirely, which is
why the halted floor is ~0.5 W. That is an architectural constraint; report it.
"""

from __future__ import annotations

import argparse
import csv
import multiprocessing
import os
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

EVENTS_HEADER = ["t_pi", "event_idx", "arduino_t_ms", "peak", "evt_duration_ms",
                 "state_at_evt", "capture_ms", "infer_ms", "latency_ms",
                 "class_id", "class_name", "confidence", "top5", "fired"]
MAX_LINE = 64
BOOT_WINDOW_S = 120.0    # uptime under this at startup means we just booted

_stop = False


def _on_signal(_sig, _frame):
    global _stop
    _stop = True


def mono_ms() -> int:
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


def uptime_s() -> float:
    try:
        with open("/proc/uptime") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError):
        return float("inf")     # not a Pi; assume we did not just boot


def _spin(until: float) -> None:
    x = 0
    while time.time() < until:
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
    return x


def clapperboard(seconds: float = 2.0) -> float:
    """Burn every core for `seconds`. Returns the start timestamp, in Pi time."""
    t0 = time.time()
    until = t0 + seconds
    procs = [multiprocessing.Process(target=_spin, args=(until,))
             for _ in range(max(1, os.cpu_count() or 1))]
    for p in procs:
        p.start()
    _spin(until)
    for p in procs:
        p.join()
    return t0


class Link:
    """Line-oriented serial, non-blocking, with the protocol's framing rules.

    Falls back to raw file-descriptor I/O when pyserial is absent. That fallback
    cannot set a baud rate, so it is correct only for a pty -- which is exactly
    what tools/mock_arduino.py allocates, letting the whole Pi path be tested
    with no dependencies at all. On real hardware, install pyserial.
    """

    def __init__(self, port: str, baud: int = 9600):
        self.ser = None
        self.fd = None
        self.buf = bytearray()
        try:
            import serial
            self.ser = serial.Serial(port, baud, timeout=0)
        except ImportError:
            if not os.path.exists(port):
                raise
            print("# pyserial missing -- raw fd fallback, baud NOT set. "
                  "Correct for a pty only.", flush=True)
            self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
            os.set_blocking(self.fd, False)
            if os.isatty(self.fd):
                import tty
                tty.setraw(self.fd)      # no echo, no line discipline

    def _write(self, data: bytes) -> None:
        if self.ser is not None:
            self.ser.write(data)
            self.ser.flush()
        else:
            os.write(self.fd, data)

    def _read(self, n: int) -> bytes:
        if self.ser is not None:
            return self.ser.read(n)
        try:
            return os.read(self.fd, n)
        except (BlockingIOError, OSError):
            return b""

    def send(self, line: str) -> None:
        self._write((line + "\n").encode("ascii", "replace"))

    def readline(self) -> str | None:
        """One complete line, or None. Never blocks."""
        data = self._read(256)
        if data:
            self.buf.extend(data)
        i = self.buf.find(b"\n")
        if i < 0:
            if len(self.buf) > 4 * MAX_LINE:
                del self.buf[:-MAX_LINE]     # runaway garbage; keep the tail
            return None
        raw = bytes(self.buf[:i])
        del self.buf[:i + 1]
        if len(raw) > MAX_LINE:
            return None                       # over-length: drop, per the contract
        return raw.decode("ascii", "replace").rstrip("\r")

    def close(self) -> None:
        try:
            if self.ser is not None:
                self.ser.close()
            elif self.fd is not None:
                os.close(self.fd)
        except Exception:
            pass


def set_param(link: Link, key: str, value: int, timeout: float = 2.0) -> int | None:
    """SET a Tier 2 parameter and return the value the firmware says is in effect.

    The return value is the point of this, not the setting. It goes into the run
    manifest, so the record cannot disagree with the hardware -- which a
    reflash-and-hope workflow could not guarantee.
    """
    link.send(f"SET,{key},{value}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = link.readline()
        if line and line.startswith(f"CFG,{key},"):
            try:
                return int(line.split(",")[2])
            except (IndexError, ValueError):
                return None
        time.sleep(0.002)
    return None


def sync_offset(link: Link, n: int = 5, timeout: float = 1.0) -> float | None:
    """Median Arduino->Pi clock offset, ms. Add to Arduino time to get Pi time.

    One round trip at 9600 baud is ~10 ms of transmission alone, so a single
    sample is noise. Median of n.
    """
    offsets = []
    for _ in range(n):
        t0 = time.monotonic() * 1000
        link.send(f"SYNC,{mono_ms()}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = link.readline()
            if line and line.startswith("SYNC,"):
                try:
                    t_ard = int(line.split(",")[1])
                except (IndexError, ValueError):
                    break
                t1 = time.monotonic() * 1000
                offsets.append((t0 + t1) / 2 - t_ard)
                break
            time.sleep(0.002)
    return statistics.median(offsets) if offsets else None


class Daemon:
    def __init__(self, args):
        self.args = args
        self.event_idx = 0
        self.fired_count = 0
        self.just_booted = uptime_s() < BOOT_WINDOW_S

        self.clf = None
        self.cam = None
        self.target_id = -1
        if not args.no_camera or not args.fake_infer:
            import classify
            self.clf = classify.Classifier(args.model, args.models_dir, args.threads)
            try:
                self.target_id = self.clf.class_id_for(args.target_class)
            except KeyError as e:
                print(f"# target class not found: {e}", flush=True)
            if not args.no_camera:
                self.cam = classify.Camera(self.clf.width, self.clf.height,
                                           args.swap_rgb)

        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        self.sink = open(args.out, "a", newline="")
        self.writer = csv.writer(self.sink)
        if self.sink.tell() == 0:
            self.writer.writerow(EVENTS_HEADER)
            self.sink.flush()

    # ------------------------------------------------------------- handlers

    def handle_evt(self, link: Link, parts: list[str]) -> None:
        t_recv = time.time()
        try:
            ard_ms, peak, dur_ms = (int(parts[1]), int(parts[2]), int(parts[3]))
        except (IndexError, ValueError):
            # Parse before acking. An ACK promises a RES, so acking a line whose
            # fields turn out to be junk would leave Tier 2 waiting out
            # RES_TIMEOUT_MS for a result that can never come.
            link.send("# ERR bad EVT")
            return
        link.send("ACK")                     # parsed, before capture, per the contract

        state = "booted" if (self.just_booted and self.event_idx == 0) else "awake"
        cap_ms = infer_ms = 0.0
        cid, conf, top = -1, 0.0, []
        try:
            if self.cam is not None:
                frame, cap_ms = self.cam.capture()
                cid, conf, top, infer_ms = self.clf.infer(frame)
            elif self.clf is not None and self.args.fake_frame:
                import numpy as np
                frame = np.zeros((self.clf.height, self.clf.width, 3), dtype=np.uint8)
                cid, conf, top, infer_ms = self.clf.infer(frame)
            else:
                time.sleep(self.args.fake_latency / 1000.0)
                cid, conf = 955, 0.87   # 'banana' in this model's 1001-class space
        except Exception as e:
            # Never stay silent: counts must stay aligned or the detection rate
            # is unrecoverable from the logs.
            print(f"# capture/infer failed: {e}", flush=True)
            cid, conf = -1, 0.0

        latency_ms = (time.time() - t_recv) * 1000.0
        if cid >= 0 and conf < self.args.conf_threshold:
            cid, conf = -1, 0.0
        link.send(f"RES,{cid},{conf:.3f},{int(round(latency_ms))}")

        fired = int(cid == self.target_id and self.target_id >= 0)
        self.fired_count += fired
        name = self.clf.label(cid) if (self.clf and cid >= 0) else "none"
        self.writer.writerow([
            f"{t_recv:.3f}", self.event_idx, ard_ms, peak, dur_ms, state,
            f"{cap_ms:.1f}", f"{infer_ms:.1f}", f"{latency_ms:.1f}",
            cid, name, f"{conf:.3f}",
            ";".join(f"{c}:{p:.3f}" for c, p in top), fired])
        self.sink.flush()
        print(f"# evt {self.event_idx} {state} {latency_ms:.0f}ms -> "
              f"{cid} {name} {conf:.3f}{' FIRED' if fired else ''}", flush=True)
        self.event_idx += 1

    def handle_halt(self, link: Link) -> bool:
        link.send("ACK")
        link.send("# halting")
        time.sleep(0.2)                       # let the bytes leave the UART
        self.close()
        if self.args.no_halt:
            print("# --no-halt: would run 'sudo halt' here", flush=True)
            return False
        print("# halting now", flush=True)
        subprocess.run(["sudo", "halt"], check=False)
        return True

    def close(self) -> None:
        if self.cam is not None:
            self.cam.close()
            self.cam = None
        try:
            self.sink.flush()
            self.sink.close()
        except Exception:
            pass

    # ----------------------------------------------------------------- loop

    def run(self, link: Link) -> int:
        offset = sync_offset(link, self.args.sync_n)
        print(f"# sync offset {offset:.1f} ms" if offset is not None
              else "# sync failed (no Arduino?)", flush=True)

        # Swept parameters, set and verified before the clapperboard so the run
        # record carries what the firmware actually has, not what we asked for.
        for key, want in (("DORMANCY", self.args.dormancy_ms),
                          ("PERSIST", self.args.persist_ms),
                          ("REFRACTORY", self.args.refractory_ms)):
            if want is None:
                continue
            got = set_param(link, key, want)
            if got is None:
                print(f"# WARN {key} not acknowledged -- firmware may predate "
                      f"SET/GET; the value in effect is whatever was flashed",
                      flush=True)
            else:
                print(f"# param {key}={got}"
                      + (f" (requested {want}, CLAMPED)" if got != want else ""),
                      flush=True)

        t_clap = clapperboard(self.args.clapperboard)
        print(f"# clapperboard {t_clap:.3f} {self.args.clapperboard:g}s", flush=True)
        print(f"# ready {time.time():.3f} uptime={uptime_s():.1f} "
              f"model={self.args.model}", flush=True)
        link.send("# ready")

        deadline = time.time() + self.args.max_runtime if self.args.max_runtime else None
        while not _stop:
            if deadline and time.time() > deadline:
                print("# max runtime reached", flush=True)
                break
            line = link.readline()
            if line is None:
                time.sleep(0.002)
                continue
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            tag = parts[0]
            if tag == "EVT":
                self.handle_evt(link, parts)
            elif tag == "HALT":
                if self.handle_halt(link):
                    return 0
                break
            elif tag == "SYNC":
                link.send(f"SYNC,{mono_ms()}")
            elif tag == "ACK":
                pass
            else:
                link.send(f"# ERR {line[:20]}")

        self.close()
        print(f"# stopped after {self.event_idx} events, {self.fired_count} fired",
              flush=True)
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/serial0")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--model", choices=["int8", "fp32"], default="int8")
    ap.add_argument("--models-dir", type=Path,
                    default=Path(__file__).resolve().parent / "models")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--target-class", default="banana")
    ap.add_argument("--conf-threshold", type=float, default=0.30,
                    help="below this, RES reports class_id -1")
    ap.add_argument("-o", "--out", default="events.csv")
    ap.add_argument("--dormancy-ms", type=int,
                    help="SET the Tier 2 dormancy timeout at startup; -1 = never halt")
    ap.add_argument("--persist-ms", type=int,
                    help="SET the Tier 2 persistence window at startup")
    ap.add_argument("--refractory-ms", type=int,
                    help="SET the Tier 2 refractory period at startup")
    ap.add_argument("--clapperboard", type=float, default=2.0,
                    help="seconds of full-core burn at start; 0 disables")
    ap.add_argument("--sync-n", type=int, default=5)
    ap.add_argument("--max-runtime", type=float,
                    help="exit after this many seconds (safety net for sweeps)")
    ap.add_argument("--no-camera", action="store_true",
                    help="no picamera2 -- for running against a mock Arduino")
    ap.add_argument("--fake-infer", action="store_true",
                    help="with --no-camera, skip the model too")
    ap.add_argument("--fake-frame", action="store_true",
                    help="with --no-camera, still run the model on a blank frame")
    ap.add_argument("--fake-latency", type=float, default=100.0,
                    help="ms of pretend work when nothing real is running")
    ap.add_argument("--no-halt", action="store_true",
                    help="log the halt instead of executing it")
    ap.add_argument("--swap-rgb", dest="swap_rgb", action="store_true", default=True)
    ap.add_argument("--no-swap-rgb", dest="swap_rgb", action="store_false")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        link = Link(args.port, args.baud)
    except Exception as e:
        sys.exit(f"cannot open {args.port}: {e}\n"
                 "  - serial console disabled and serial hardware enabled?\n"
                 "  - dtoverlay=disable-bt in /boot/firmware/config.txt?\n"
                 "  - user in the dialout group?")
    daemon = Daemon(args)
    try:
        return daemon.run(link)
    finally:
        link.close()


if __name__ == "__main__":
    sys.exit(main())
