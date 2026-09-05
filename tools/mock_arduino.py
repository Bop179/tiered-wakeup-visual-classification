#!/usr/bin/env python3
"""Stand-in for Tier 2, so the whole Pi path can be exercised with no Arduino.

    tools/mock_arduino.py --self-test                  # no deps, no hardware at all
    tools/mock_arduino.py --n-events 10 --mean-interval 3   # spawns pi_daemon on a pty
    tools/mock_arduino.py --port /dev/ttyUSB0          # real wire, USB-TTL adapter

Three modes, in increasing order of how much has to exist:

  --self-test   Both halves run in this process, wired by in-memory queues. Needs
                no pyserial, no camera, no model, no hardware. It verifies the
                protocol parser -- every message, plus malformed lines, an
                over-length line, and a comment -- and is the thing to run first
                when anything about the contract changes.

  pty (default) Allocates a pseudo-terminal, spawns pi/pi_daemon.py against it,
                and drives the real daemon over a real serial-shaped file. This
                is the Sep 9 verification: 10 synthetic events, every one coming
                back as a well-formed RES.

  --port        Talks to a real port, for driving the Pi over a USB-TTL adapter
                before the Arduino is finished.

Emulates Tier 2's actual behaviour: exponential events, ACK/RES timeouts, the
dormancy timer, HALT, and answering SYNC. It deliberately does NOT retransmit,
because the real firmware does not either -- a lost event is data.
"""

from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
import threading
import time
import tty
from collections import deque
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ACK_TIMEOUT_S = 2.0
RES_TIMEOUT_S = 5.0
MAX_LINE = 64


def mono_ms() -> int:
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


# ---------------------------------------------------------------- transports

class QueueLink:
    """In-memory link. Duck-types pi_daemon.Link for --self-test."""

    def __init__(self, inbox: deque, outbox: deque):
        self.inbox, self.outbox = inbox, outbox

    def send(self, line: str) -> None:
        self.outbox.append(line)

    def readline(self) -> str | None:
        return self.inbox.popleft() if self.inbox else None

    def close(self) -> None:
        pass


class FdLink:
    """Raw file-descriptor link, for the pty master side. No pyserial needed."""

    def __init__(self, fd: int):
        self.fd = fd
        os.set_blocking(fd, False)
        self.buf = bytearray()

    def send(self, line: str) -> None:
        os.write(self.fd, (line + "\n").encode("ascii", "replace"))

    def readline(self) -> str | None:
        try:
            data = os.read(self.fd, 256)
            if data:
                self.buf.extend(data)
        except (BlockingIOError, OSError):
            pass
        i = self.buf.find(b"\n")
        if i < 0:
            return None
        raw = bytes(self.buf[:i])
        del self.buf[:i + 1]
        return raw.decode("ascii", "replace").rstrip("\r")

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


class SerialLink(FdLink):
    def __init__(self, port: str, baud: int):
        import serial
        self.ser = serial.Serial(port, baud, timeout=0)
        self.buf = bytearray()

    def send(self, line: str) -> None:
        self.ser.write((line + "\n").encode("ascii", "replace"))
        self.ser.flush()

    def readline(self) -> str | None:
        data = self.ser.read(256)
        if data:
            self.buf.extend(data)
        i = self.buf.find(b"\n")
        if i < 0:
            return None
        raw = bytes(self.buf[:i])
        del self.buf[:i + 1]
        return raw.decode("ascii", "replace").rstrip("\r")

    def close(self) -> None:
        self.ser.close()


# ------------------------------------------------------------------ the mock

class MockArduino:
    """Tier 2's state machine, minus the sleeping and the analog front end."""

    # Defaults match docs/INTERFACE.md section 6; ranges match section 1.1.
    PARAMS = {"DORMANCY": (30000, -1, 3600000),
              "PERSIST": (40, 0, 5000),
              "REFRACTORY": (500, 0, 60000)}

    def __init__(self, link, args):
        self.link, self.args = link, args
        self.rng = random.Random(args.seed)
        self.results: list[dict] = []
        self.acks = self.timeouts = self.malformed = 0
        self.params = {k: v[0] for k, v in self.PARAMS.items()}

    def handle_config(self, line: str) -> bool:
        """SET/GET, answering CFG with the value actually in effect. -> handled?"""
        parts = line.split(",")
        if parts[0] not in ("SET", "GET") or len(parts) < 2:
            return False
        key = parts[1]
        if key not in self.PARAMS:
            self.link.send(f"# ERR unknown key {key}")
            return True
        if parts[0] == "SET":
            try:
                want = int(parts[2])
            except (IndexError, ValueError):
                self.link.send(f"# ERR bad value for {key}")
                return True
            default, lo, hi = self.PARAMS[key]
            # Clamp, then report what is in effect -- never what was asked for.
            self.params[key] = lo if want < lo else hi if want > hi else want
        self.link.send(f"CFG,{key},{self.params[key]}")
        return True

    def _wait_for(self, prefix: str, timeout: float,
                  answer_sync: bool = True) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.link.readline()
            if line is None:
                time.sleep(0.002)
                continue
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                if self.args.verbose:
                    print(f"  pi: {line}", file=sys.stderr)
                continue
            if answer_sync and line.startswith("SYNC,"):
                self.link.send(f"SYNC,{mono_ms()}")
                continue
            if self.handle_config(line):
                continue
            if line.startswith(prefix):
                return line
            self.malformed += 1
            if self.args.verbose:
                print(f"  unexpected: {line!r}", file=sys.stderr)
        return None

    def wait_ready(self, timeout: float = 60.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.link.readline()
            if line is None:
                time.sleep(0.005)
                continue
            if self.args.verbose:
                print(f"  pi: {line}", file=sys.stderr)
            line = line.strip()
            if self.handle_config(line):
                continue
            if line.startswith("# ready"):
                return True
        return False

    def one_event(self, idx: int) -> dict:
        peak = self.rng.randint(520, 1000)
        duration = self.rng.randint(80, 400)
        t0 = time.monotonic()
        self.link.send(f"EVT,{mono_ms()},{peak},{duration}")

        ack = self._wait_for("ACK", ACK_TIMEOUT_S)
        if ack is None:
            self.timeouts += 1
            return {"idx": idx, "ok": False, "why": "no ACK"}
        self.acks += 1
        ack_ms = (time.monotonic() - t0) * 1000

        res = self._wait_for("RES,", RES_TIMEOUT_S)
        if res is None:
            self.timeouts += 1
            return {"idx": idx, "ok": False, "why": "no RES"}

        parts = res.split(",")
        try:
            cid, conf, lat = int(parts[1]), float(parts[2]), int(parts[3])
        except (IndexError, ValueError):
            self.malformed += 1
            return {"idx": idx, "ok": False, "why": f"bad RES {res!r}"}
        return {"idx": idx, "ok": True, "class_id": cid, "conf": conf,
                "latency_ms": lat, "ack_ms": ack_ms,
                "round_trip_ms": (time.monotonic() - t0) * 1000}

    def run(self) -> int:
        if not self.wait_ready(self.args.ready_timeout):
            print("FAIL: daemon never sent '# ready'", file=sys.stderr)
            return 1

        for i in range(self.args.n_events):
            if i:
                gap = (self.args.mean_interval if self.args.dwell_dist == "fixed"
                       else self.rng.expovariate(1.0 / self.args.mean_interval))
                time.sleep(min(gap, self.args.max_gap))
            r = self.one_event(i)
            self.results.append(r)
            if r["ok"]:
                print(f"[{i:3d}] ACK {r['ack_ms']:6.1f}ms  RES class={r['class_id']:4d} "
                      f"conf={r['conf']:.3f} lat={r['latency_ms']}ms")
            else:
                print(f"[{i:3d}] MISS -- {r['why']}")

        if self.args.send_halt:
            print("sending HALT")
            self.link.send("HALT")
            ack = self._wait_for("ACK", ACK_TIMEOUT_S)
            print("  HALT acked" if ack else "  HALT not acked")

        ok = [r for r in self.results if r["ok"]]
        print(f"\n{len(ok)}/{len(self.results)} events completed, "
              f"{self.timeouts} timeouts, {self.malformed} malformed")
        if ok:
            lat = sorted(r["round_trip_ms"] for r in ok)
            print(f"round trip ms: min {lat[0]:.1f}  p50 {lat[len(lat) // 2]:.1f}  "
                  f"max {lat[-1]:.1f}")
        return 0 if len(ok) == len(self.results) and not self.malformed else 1


# ----------------------------------------------------------------- self-test

def self_test(args) -> int:
    """Both halves in one process. No pyserial, no camera, no model, no hardware."""
    sys.path.insert(0, str(REPO / "pi"))
    import pi_daemon

    to_pi, to_mock = deque(), deque()
    pi_link = QueueLink(to_pi, to_mock)
    mock_link = QueueLink(to_mock, to_pi)

    class A:                                   # pi_daemon's args, minimally
        port, baud = "queue", 9600
        model, models_dir, threads = "int8", REPO / "pi" / "models", 4
        target_class, conf_threshold = "banana", 0.30
        out = str(args.events_out)
        clapperboard, sync_n, max_runtime = 0.0, 1, 30.0
        dormancy_ms = args.dormancy_ms if args.dormancy_ms is not None else 30000
        persist_ms = refractory_ms = None
        no_camera = fake_infer = no_halt = True
        fake_frame, swap_rgb = False, True
        fake_latency = 5.0

    daemon = pi_daemon.Daemon(A())
    thread = threading.Thread(target=daemon.run, args=(pi_link,), daemon=True)
    thread.start()

    mock = MockArduino(mock_link, args)
    if not mock.wait_ready(5.0):
        print("FAIL: no '# ready'", file=sys.stderr)
        return 1

    failures = []
    for i in range(args.n_events):
        r = mock.one_event(i)
        if not r["ok"]:
            failures.append(f"event {i}: {r['why']}")
        elif not 0.0 <= r["conf"] <= 1.0:
            failures.append(f"event {i}: confidence {r['conf']} out of range")
    print(f"  {args.n_events} events: "
          f"{args.n_events - len(failures)} well-formed RES")

    # The contract's edge cases. None of these may wedge the daemon.
    checks = [
        ("comment ignored", "# hello from the mock"),
        ("malformed EVT", "EVT,not_a_number,x"),
        ("unknown verb", "WAT,1,2,3"),
        ("empty line", ""),
        ("over-length line", "EVT," + "9" * (MAX_LINE * 2)),
        ("bare comma", ","),
    ]
    for name, payload in checks:
        mock_link.send(payload)
        time.sleep(0.05)
        while mock_link.readline() is not None:
            pass
        r = mock.one_event(999)
        print(f"  after {name:<18} -> {'alive' if r['ok'] else 'WEDGED'}")
        if not r["ok"]:
            failures.append(f"daemon wedged after {name}")

    # The daemon SETs the swept parameters at startup; wait_ready above answered
    # them. If this landed, the Pi -> Tier 2 configuration path works end to end
    # and the run manifest can record the value actually in effect.
    want_dorm = A.dormancy_ms
    if want_dorm is not None:
        got = mock.params["DORMANCY"]
        print(f"  daemon SET DORMANCY -> {got} "
              f"{'ok' if got == want_dorm else 'FAIL'}")
        if got != want_dorm:
            failures.append(f"daemon failed to SET DORMANCY: {got} != {want_dorm}")

    # Clamping is a unit test of Tier 2's side, on its own queues so the replies
    # do not land in the daemon's inbox. This is the behaviour Juan's firmware
    # must reproduce: CFG reports what is IN EFFECT, never what was requested.
    probe = MockArduino(QueueLink(deque(), deque()), args)
    for key, want, expect in (("DORMANCY", 12000, 12000),
                              ("DORMANCY", 9_999_999, 3600000),   # clamp high
                              ("DORMANCY", -50, -1),              # clamp low
                              ("PERSIST", 40, 40),
                              ("REFRACTORY", 99_999, 60000)):     # clamp high
        probe.link.outbox.clear()
        probe.handle_config(f"SET,{key},{want}")
        replies = list(probe.link.outbox)
        got = (int(replies[-1].split(",")[2])
               if replies and replies[-1].startswith(f"CFG,{key},") else None)
        print(f"  SET {key}={str(want):<9} -> CFG {str(expect):<9} "
              f"{'ok' if got == expect else f'FAIL (got {got})'}")
        if got != expect:
            failures.append(f"SET {key},{want} -> {got}, expected {expect}")

    probe.link.outbox.clear()
    probe.handle_config("GET,DORMANCY")
    replies = list(probe.link.outbox)
    ok_get = bool(replies) and replies[-1] == "CFG,DORMANCY,-1"
    print(f"  GET DORMANCY reads back  {'ok' if ok_get else f'FAIL ({replies})'}")
    if not ok_get:
        failures.append(f"GET,DORMANCY returned {replies}")

    probe.link.outbox.clear()
    probe.handle_config("SET,NOSUCHKEY,1")
    stray = [r for r in probe.link.outbox if r.startswith("CFG,")]
    print(f"  unknown key -> no CFG    {'ok' if not stray else 'FAIL'}")
    if stray:
        failures.append("unknown SET key produced a CFG reply")

    # A malformed EVT must not be acked: an ACK promises a RES.
    mock_link.send("EVT,junk,junk,junk")
    stray = mock._wait_for("ACK", 0.5)
    print(f"  malformed EVT unacked -> {'yes' if stray is None else 'NO'}")
    if stray is not None:
        failures.append("malformed EVT was acked; ACK no longer implies RES")

    # SYNC must be answered in both directions.
    mock_link.send(f"SYNC,{mono_ms()}")
    reply = mock._wait_for("SYNC,", 1.0, answer_sync=False)
    print(f"  SYNC answered      -> {'yes' if reply else 'NO'}")
    if not reply:
        failures.append("SYNC unanswered")

    # HALT must be acked and must not actually halt under --no-halt.
    mock_link.send("HALT")
    print(f"  HALT acked         -> "
          f"{'yes' if mock._wait_for('ACK', 2.0) else 'NO'}")

    thread.join(timeout=3.0)
    print()
    if failures:
        print(f"FAIL ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: protocol parser handles every message and every malformed input")
    return 0


# --------------------------------------------------------------------- entry

def run_pty(args) -> int:
    master, slave = os.openpty()
    # A pty defaults to canonical mode with ECHO on, so every line written to the
    # master comes straight back and reads as a malformed reply. Raw mode on both
    # ends makes the pty behave like the wire it is standing in for.
    for fd in (master, slave):
        tty.setraw(fd)
    slave_path = os.ttyname(slave)
    cmd = [sys.executable, str(REPO / "pi" / "pi_daemon.py"),
           "--port", slave_path, "--out", str(args.events_out),
           "--clapperboard", str(args.clapperboard), "--sync-n", "1",
           "--max-runtime", str(args.max_runtime), "--no-halt"]
    if args.dormancy_ms is not None:
        cmd += ["--dormancy-ms", str(args.dormancy_ms)]
    if args.no_camera:
        cmd.append("--no-camera")
    if args.fake_infer:
        cmd.append("--fake-infer")
    print(f"spawning: {' '.join(cmd)}", file=sys.stderr)
    proc = subprocess.Popen(cmd)
    os.close(slave)
    link = FdLink(master)
    try:
        return MockArduino(link, args).run()
    finally:
        link.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true",
                    help="both halves in-process; no dependencies, no hardware")
    ap.add_argument("--port", help="real serial port instead of a pty")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--n-events", type=int, default=10)
    ap.add_argument("--mean-interval", type=float, default=3.0)
    ap.add_argument("--dwell-dist", choices=["exponential", "fixed"],
                    default="exponential")
    ap.add_argument("--max-gap", type=float, default=30.0,
                    help="cap on a sampled gap, so a long tail cannot stall a test")
    ap.add_argument("--send-halt", action="store_true",
                    help="send HALT after the last event")
    ap.add_argument("--events-out", type=Path, default=Path("mock_events.csv"))
    ap.add_argument("--clapperboard", type=float, default=0.0)
    ap.add_argument("--max-runtime", type=float, default=300.0)
    ap.add_argument("--ready-timeout", type=float, default=60.0)
    ap.add_argument("--no-camera", action="store_true", default=True)
    ap.add_argument("--fake-infer", action="store_true", default=True)
    ap.add_argument("--camera", dest="no_camera", action="store_false",
                    help="let the spawned daemon use the real camera and model")
    ap.add_argument("--real-infer", dest="fake_infer", action="store_false")
    ap.add_argument("--dormancy-ms", type=int,
                    help="have the daemon SET this on the mock at startup")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test(args)
    if args.port:
        return MockArduino(SerialLink(args.port, args.baud), args).run()
    return run_pty(args)


if __name__ == "__main__":
    sys.exit(main())
