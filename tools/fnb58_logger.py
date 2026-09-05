#!/usr/bin/env python3
"""100 Hz power logger for the FNIRSI FNB58, macOS friendly.

    brew install hidapi && pip install hidapi
    tools/fnb58_logger.py -o data/<run_id>/power.csv        # no sudo needed

Why hidapi and not libusb
-------------------------
The FNB58's PC port enumerates as USB HID. macOS's IOHIDFamily claims every HID
device automatically and libusb cannot ask it to detach -- that ability is Linux
only. So pyusb/libusb tools (including baryluk's widely recommended
fnirsi_logger.py) fail here with a detach-kernel-driver error, and **sudo does
not help**; it is architectural, not permissions. hidapi goes *through*
IOHIDManager instead of fighting it, and needs no root at all, which is what lets
an unattended overnight sweep run from a normal shell.

Sample rate
-----------
The meter streams at a fixed 100 Hz. Each 64-byte HID report carries FOUR 15-byte
samples and reports arrive ~25/s. The 1 s cadence in baryluk's logger is the
poll/keepalive interval, not a data-rate cap -- a common misreading that leads
people to believe the FNB58 logs at 1 Hz.

Per-sample timestamps are interpolated backwards across the report interval, so
the CSV really is 100 Hz and energy segmentation lands on the right samples
rather than on four identical timestamps 40 ms apart.

Physical setup
--------------
    brick -> meter IN -> meter OUT -> Pi          (device on its own mains brick)
    meter PC port (micro-USB, top long edge) -> Mac      (must be a DATA cable)

Never power the Pi through the Mac's USB port; a Pi 4 browns out and the whole
measurement is worthless. A "NO NAME" disk mounting on plug-in is normal -- the
meter is mass storage and HID at once. Unmount it so macOS stops poking at it:

    diskutil unmount "/Volumes/NO NAME"

"HID interface not found" after it was working is a physical fault, not a code
fault: re-seat the micro-USB PC cable. Check the bus before touching this script:

    system_profiler SPUSBDataType | grep -i 2e3c

Stops on Ctrl-C, on --duration, or when a file named `fnirsi_stop` appears in the
working directory, so run_experiment.py can end a log without kill -9 truncating
a line mid-write.
"""

from __future__ import annotations

import argparse
import csv
import os
import signal
import struct
import sys
import time
from pathlib import Path

VID, PID, IFACE = 0x2E3C, 0x5558, 3
INIT1 = b"\xaa\x81" + b"\x00" * 61 + b"\x8e"
INIT2 = b"\xaa\x82" + b"\x00" * 61 + b"\x96"
POLL = b"\xaa\x83" + b"\x00" * 61 + b"\x9e"
POLL_INTERVAL_S = 1.0
SAMPLES_PER_REPORT = 4
STOP_FILE = "fnirsi_stop"

HEADER = ["t_mac", "sample_idx", "voltage_V", "current_A", "power_W",
          "dp_V", "dn_V", "temp_C", "energy_J", "charge_C"]

_stop = False


def _on_signal(_sig, _frame):
    global _stop
    _stop = True


def open_meter():
    try:
        import hid
    except ImportError:
        sys.exit("pip install hidapi  (and: brew install hidapi)")
    path = next((d["path"] for d in hid.enumerate(VID, PID)
                 if d.get("interface_number") == IFACE), None)
    if path is None:
        sys.exit(f"FNB58 HID interface #{IFACE} not found.\n"
                 "  - is the micro-USB cable in the PC port on the top long edge?\n"
                 "  - is it a DATA cable, not charge-only?\n"
                 "  - check the bus: system_profiler SPUSBDataType | grep -i 2e3c")
    dev = hid.device()
    dev.open_path(path)
    return dev


def write_cmd(dev, pkt: bytes) -> None:
    # macOS hidapi wants a report-ID byte prefixed; fall back if it is rejected.
    if dev.write(b"\x00" + pkt) < 0:
        dev.write(pkt)


def decode(report: bytes):
    """Four (V, I, D+, D-, temp) tuples from one 64-byte data report."""
    out = []
    for k in range(SAMPLES_PER_REPORT):
        b = 2 + k * 15
        out.append((
            struct.unpack_from("<I", report, b)[0] / 100000.0,      # volts
            struct.unpack_from("<I", report, b + 4)[0] / 100000.0,  # amps
            struct.unpack_from("<H", report, b + 8)[0] / 1000.0,    # D+
            struct.unpack_from("<H", report, b + 10)[0] / 1000.0,   # D-
            struct.unpack_from("<H", report, b + 12)[0] / 10.0,     # temp C
        ))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", help="CSV path (default: stdout)")
    ap.add_argument("--duration", type=float, help="stop after this many seconds")
    ap.add_argument("--stop-file", default=STOP_FILE,
                    help=f"stop when this path appears (default: {STOP_FILE})")
    ap.add_argument("--quiet", action="store_true",
                    help="no live status line on stderr")
    ap.add_argument("--check", action="store_true",
                    help="open the meter, print one sample, exit")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    dev = open_meter()
    for pkt in (INIT1, INIT2, POLL):
        write_cmd(dev, pkt)
        time.sleep(0.05)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        sink = open(args.out, "w", newline="")
    else:
        sink = sys.stdout
    writer = csv.writer(sink)
    writer.writerow(HEADER)

    energy = 0.0      # J, running integral from logger start
    charge = 0.0      # C
    idx = 0
    t_start = time.time()
    t_last = t_start
    next_poll = t_start + POLL_INTERVAL_S
    n_reports = 0

    try:
        while not _stop:
            if os.path.exists(args.stop_file):
                break
            if args.duration and time.time() - t_start >= args.duration:
                break

            data = dev.read(64, timeout_ms=200)
            now = time.time()
            if now >= next_poll:
                write_cmd(dev, POLL)
                next_poll = now + POLL_INTERVAL_S
            if not data or bytes(data[0:2]) != b"\xaa\x04":
                continue

            samples = decode(bytes(data))
            dt = now - t_last
            t_last = now
            # Guard against a stall (USB hiccup, laptop sleep) silently inflating
            # the energy integral across a gap where nothing was measured.
            if dt > 1.0:
                print(f"# gap of {dt:.2f}s at {now:.3f} -- energy across it is "
                      f"extrapolated, not measured", file=sys.stderr)
            step = dt / SAMPLES_PER_REPORT

            for k, (v, i, dp, dn, temp) in enumerate(samples):
                # Spread the four samples backwards across the report interval.
                t = now - (SAMPLES_PER_REPORT - 1 - k) * step
                p = v * i
                energy += p * step
                charge += i * step
                writer.writerow([f"{t:.3f}", idx, f"{v:.5f}", f"{i:.5f}",
                                 f"{p:.5f}", f"{dp:.3f}", f"{dn:.3f}",
                                 f"{temp:.1f}", f"{energy:.5f}", f"{charge:.5f}"])
                idx += 1

            sink.flush()
            n_reports += 1
            if args.check:
                v, i, *_ = samples[-1]
                print(f"{v:.3f} V  {i:.4f} A  {v * i:.3f} W", file=sys.stderr)
                break
            if not args.quiet and n_reports % 25 == 0:
                v, i, *_ = samples[-1]
                elapsed = now - t_start
                print(f"\r{elapsed:7.1f}s  {v:6.3f} V  {i:6.4f} A  {v * i:6.3f} W  "
                      f"{energy:9.2f} J  {idx} samples "
                      f"({idx / max(elapsed, 1e-9):5.1f}/s)",
                      end="", file=sys.stderr, flush=True)
    finally:
        dev.close()
        if sink is not sys.stdout:
            sink.close()
        if not args.quiet:
            print(file=sys.stderr)

    elapsed = time.time() - t_start
    rate = idx / elapsed if elapsed else 0.0
    print(f"# {idx} samples in {elapsed:.1f}s ({rate:.1f}/s), {energy:.2f} J",
          file=sys.stderr)
    if idx and not 80 <= rate <= 120:
        print(f"# WARNING: {rate:.1f} samples/s -- expected ~100. Dropped USB "
              f"reports make energy integration unreliable.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
