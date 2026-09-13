#!/usr/bin/env python3
"""Stand-in for Tier 3, so the Tier 2 firmware can be exercised with no Pi.

    tools/mock_pi.py --port /dev/cu.usbserial-10 --conformance   # automated contract check
    tools/mock_pi.py --port /dev/cu.usbserial-10 --serve         # behave like the Pi

The Uno's hardware UART is also its USB serial (docs/INTERFACE.md section 1), so
with the Arduino on USB and NOTHING wired to the Pi, this speaks the exact
protocol the Pi does, over the exact code path the firmware uses when
integrated. That is the point of the hardware-UART choice.

  --conformance  Drives the firmware through every part of the contract that does
                 not need a Tier 1 trigger, and fails loudly on any deviation:
                 the boot banner, GET defaults, SET round trips, clamping at both
                 ends, DORMANCY -1, an unknown key (a comment and NO CFG), SYNC,
                 CRLF, a comment, a malformed line and an over-length line (both
                 must be dropped without wedging the parser), and finally the
                 dormancy -> HALT -> ACK -> HALT_SETTLE -> sleep path.

  --serve        Answers like the Pi: ACK on EVT, then RES after --latency-ms.
                 --halted-for S goes silent for S seconds after a HALT and then
                 prints "# ready", so the wake path (ACK timeout -> wake assert ->
                 wait for "# ready" -> send the pending EVT) can be watched by
                 grounding D2 by hand.

Opening the port toggles DTR, which resets an Uno. Every run therefore starts
from a known power-on state, and the banner is how that is confirmed.

Needs pyserial.
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial is required: pip install pyserial")

HALT_SETTLE_S = 20.0
BANNER = "# t2: tier2_firmware up"


class Link:
    """Background reader. EVTs are answered here so a floating D2 cannot stall a
    test by leaving the firmware waiting out ACK_TIMEOUT_MS; every other line is
    queued for whoever is asking."""

    def __init__(self, port: str, baud: int, latency_ms: int, verbose: bool):
        self.ser = serial.Serial(port, baud, timeout=0.05)
        self.lines: queue.Queue[str] = queue.Queue()
        self.latency_ms = latency_ms
        self.verbose = verbose
        self.silent_until = 0.0
        self.evts = 0
        self.halts = 0
        self._stop = False
        self._buf = b""
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def send(self, s: str, raw: bytes | None = None) -> None:
        data = raw if raw is not None else (s + "\n").encode()
        if self.verbose:
            print(f"  -> {data!r}")
        self.ser.write(data)

    def _reader(self) -> None:
        while not self._stop:
            chunk = self.ser.read(256)
            if not chunk:
                continue
            self._buf += chunk
            while b"\n" in self._buf:
                raw, self._buf = self._buf.split(b"\n", 1)
                line = raw.decode(errors="replace").rstrip("\r")
                if self.verbose:
                    print(f"  <- {line}")
                self._on_line(line)

    def _on_line(self, line: str) -> None:
        if line.startswith("EVT,"):
            self.evts += 1
            if time.monotonic() < self.silent_until:
                return                          # halted: nobody is listening
            self.send("ACK")
            time.sleep(self.latency_ms / 1000)
            self.send(f"RES,954,0.912,{self.latency_ms}")
            return
        if line == "HALT":
            self.halts += 1
        self.lines.put(line)

    def expect(self, pred, timeout: float, what: str) -> str:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"timed out after {timeout:.1f}s waiting for {what}")
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                continue
            if pred(line):
                return line

    def drain(self, secs: float) -> list[str]:
        got, end = [], time.monotonic() + secs
        while time.monotonic() < end:
            try:
                got.append(self.lines.get(timeout=max(0.0, end - time.monotonic())))
            except queue.Empty:
                break
        return got

    def close(self) -> None:
        self._stop = True
        self._thread.join(timeout=1)            # read() times out at 50 ms
        self.ser.close()


# ------------------------------------------------------------ conformance

def conformance(link: Link, skip_halt: bool) -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, fn) -> None:
        try:
            detail = fn() or ""
            results.append((name, True, detail))
            print(f"  ok    {name}{'  ' + detail if detail else ''}")
        except AssertionError as e:
            results.append((name, False, str(e)))
            print(f"  FAIL  {name}  {e}")

    def cfg(key: str, value: int, timeout: float = 2.0):
        want = f"CFG,{key},{value}"
        link.expect(lambda l: l.startswith(f"CFG,{key},"), timeout, want)

    def cfg_is(key: str, value: int):
        got = link.expect(lambda l: l.startswith(f"CFG,{key},"), 2.0, f"CFG,{key}")
        assert got == f"CFG,{key},{value}", f"got {got!r}, want CFG,{key},{value}"

    print("waiting for reset + banner ...")
    check("boot banner after DTR reset",
          lambda: link.expect(lambda l: l == BANNER, 6.0, repr(BANNER)) and None)

    # -- GET defaults are the section 6 values --------------------------------
    for key, val in (("DORMANCY", 30000), ("PERSIST", 40), ("REFRACTORY", 500)):
        link.send(f"GET,{key}")
        check(f"GET {key} default = {val}", lambda k=key, v=val: cfg_is(k, v))

    # -- SET round trips --------------------------------------------------------
    link.send("SET,DORMANCY,12345")
    check("SET DORMANCY 12345 -> CFG 12345", lambda: cfg_is("DORMANCY", 12345))
    link.send("GET,DORMANCY")
    check("GET reads the SET value back", lambda: cfg_is("DORMANCY", 12345))

    # -- clamping: CFG reports the value in effect, not the value asked for ----
    link.send("SET,DORMANCY,9999999")
    check("DORMANCY clamps high to 3600000", lambda: cfg_is("DORMANCY", 3600000))
    link.send("SET,DORMANCY,-5")
    check("negative DORMANCY -> -1 (never halt)", lambda: cfg_is("DORMANCY", -1))
    link.send("SET,PERSIST,99999")
    check("PERSIST clamps high to 5000", lambda: cfg_is("PERSIST", 5000))
    link.send("SET,PERSIST,-3")
    check("PERSIST clamps low to 0", lambda: cfg_is("PERSIST", 0))
    link.send("SET,REFRACTORY,70000")
    check("REFRACTORY clamps high to 60000", lambda: cfg_is("REFRACTORY", 60000))
    link.send("SET,PERSIST,40"); link.expect(lambda l: l.startswith("CFG,PERSIST"), 2, "restore")
    link.send("SET,REFRACTORY,500"); link.expect(lambda l: l.startswith("CFG,REFRACTORY"), 2, "restore")

    # -- unknown key: a comment and NO CFG --------------------------------------
    def unknown_key():
        link.send("SET,BOGUS,1")
        link.expect(lambda l: l == "# ERR unknown key BOGUS", 2.0, "# ERR unknown key BOGUS")
        stray = [l for l in link.drain(1.0) if l.startswith("CFG,BOGUS")]
        assert not stray, f"sent a CFG for an unknown key: {stray}"
    check("unknown key -> '# ERR' and no CFG", unknown_key)

    link.send("GET,NOPE")
    check("GET unknown key -> '# ERR'",
          lambda: link.expect(lambda l: l == "# ERR unknown key NOPE", 2.0, "# ERR") and None)

    # -- SYNC -------------------------------------------------------------------
    def sync():
        link.send("SYNC,123")
        got = link.expect(lambda l: l.startswith("SYNC,"), 2.0, "SYNC,<t_ms>")
        int(got.split(",")[1])
        return got
    check("SYNC answered with SYNC,<millis>", sync)

    # -- parser robustness: none of these may wedge it ---------------------------
    def still_alive(label: str):
        link.send("GET,PERSIST")
        cfg_is("PERSIST", 40)
        return label

    link.send("", raw=b"GET,PERSIST\r\n")
    check("CRLF accepted and stripped", lambda: cfg_is("PERSIST", 40))
    link.send("# a comment, ignore me")
    check("comment ignored, parser alive", lambda: still_alive(""))
    link.send("", raw=b"\x00\xff,,,garbage,,,\n")
    check("malformed line dropped, parser alive", lambda: still_alive(""))
    link.send("", raw=b"GET,PERSIST" + b"X" * 80 + b"\n")
    def overlong():
        stray = [l for l in link.drain(1.0) if l.startswith("CFG,")]
        assert not stray, f"acted on an over-length line: {stray}"
        return still_alive("")
    check("over-length (>64 byte) line dropped, parser alive", overlong)
    link.send("ACK")
    link.send("RES,1,0.5,20")
    check("stray ACK/RES tolerated, parser alive", lambda: still_alive(""))

    # -- DORMANCY -1 must never halt ---------------------------------------------
    def never_halt():
        link.send("SET,DORMANCY,-1"); cfg_is("DORMANCY", -1)
        before = link.halts
        link.drain(4.0)
        assert link.halts == before, "sent HALT with DORMANCY -1"
        return "no HALT in 4 s"
    check("DORMANCY -1 never halts", never_halt)

    # -- dormancy -> HALT -> settle -> sleep --------------------------------------
    if not skip_halt:
        def halt_path():
            link.send("SET,DORMANCY,1500"); cfg_is("DORMANCY", 1500)
            t0 = time.monotonic()
            link.expect(lambda l: l == "HALT", 8.0, "HALT")
            waited = time.monotonic() - t0
            link.send("ACK")
            t1 = time.monotonic()
            link.expect(lambda l: l == "# t2: pi halted, sleeping",
                        HALT_SETTLE_S + 6, "'# t2: pi halted, sleeping'")
            settle = time.monotonic() - t1
            assert settle >= HALT_SETTLE_S - 1.0, (
                f"declared the Pi halted after {settle:.1f}s; HALT_SETTLE_MS is 20 s")
            # Asleep in PWR_DOWN: the USART is off, so a GET must go unanswered.
            link.send("GET,PERSIST")
            stray = [l for l in link.drain(2.0) if l.startswith("CFG,")]
            assert not stray, f"answered while supposedly powered down: {stray}"
            return f"HALT after {waited:.1f}s, settle {settle:.1f}s, then deaf (asleep)"
        check("dormancy -> HALT -> 20 s settle -> deep sleep", halt_path)

    # ---------------------------------------------------------------- summary
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if link.evts:
        print(f"\nWARNING: the firmware sent {link.evts} EVT(s) with no Tier 1 attached.\n"
              f"  D2 is floating. It needs the LM339N's 10 kOhm pull-up to 5 V\n"
              f"  (section 2.3), or for bench testing a 10 kOhm from D2 to 5 V.")
    return 1 if failed else 0


# ------------------------------------------------------------------ serve

def serve(link: Link, halted_for: float, dormancy_ms: int | None = None) -> int:
    print("serving as the Pi. Ground D2 (through a resistor) to fire a trigger. Ctrl-C to stop.")
    if dormancy_ms is not None:
        link.expect(lambda l: "tier2_firmware up" in l, 5.0, "boot banner")
        link.send(f"SET,DORMANCY,{dormancy_ms}")
    try:
        while True:
            try:
                line = link.lines.get(timeout=0.5)
            except queue.Empty:
                continue
            print(f"  <- {line}")
            if line == "HALT":
                link.send("ACK")
                link.send("# halting")
                if halted_for > 0:
                    link.silent_until = time.monotonic() + halted_for
                    print(f"  (simulating a halted Pi for {halted_for:.0f} s)")
                    threading.Timer(halted_for, lambda: link.send(
                        f"# ready {time.time():.3f} uptime=16.2 model=int8")).start()
            elif line.startswith("SYNC,"):
                pass
    except KeyboardInterrupt:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True, help="the Uno, e.g. /dev/cu.usbserial-10")
    ap.add_argument("--baud", type=int, default=9600)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--conformance", action="store_true")
    mode.add_argument("--serve", action="store_true")
    ap.add_argument("--skip-halt", action="store_true",
                    help="skip the ~30 s dormancy/HALT/sleep check")
    ap.add_argument("--latency-ms", type=int, default=21,
                    help="RES delay; 21 ms is the measured INT8 end-to-end p50")
    ap.add_argument("--halted-for", type=float, default=30.0)
    ap.add_argument("--dormancy-ms", type=int,
                    help="--serve: SET DORMANCY once the Uno's banner arrives")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    link = Link(args.port, args.baud, args.latency_ms, args.verbose)
    try:
        return conformance(link, args.skip_halt) if args.conformance else serve(link, args.halted_for, args.dormancy_ms)
    finally:
        link.close()


if __name__ == "__main__":
    sys.exit(main())
