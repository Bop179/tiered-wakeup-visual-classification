#!/usr/bin/env python3
"""One command = one matrix cell. Run this, not the individual tools.

    tools/run_experiment.py --mean-interval 45 --duration-ms 15000 \
                            --dormancy-ms 30000 --contrast 0.8 --n-events 40

Hand-running 24 conditions in week two will not fit, and a run assembled by hand
is a run whose parameters nobody can reconstruct afterwards. This script is what
makes the matrix affordable:

  1. mkdir data/<run_id>/ and record the git SHA and the model's SHA256
  2. measure the Mac<->Pi clock offset (ssh date), at the START
  3. start tools/fnb58_logger.py       -> power.csv
  4. start pi/pi_daemon.py on the Pi   -> events.csv (over ssh, optional)
  5. run tools/event_display.py        -> gen.csv     [blocks until done]
  6. stop the logger cleanly via the stop-file, so no CSV line is truncated
  7. measure the clock offset again, at the END -- drift over a long run is real
  8. scp events.csv back from the Pi
  9. write manifest.json and append a skeleton row to docs/EXPERIMENTS.md

A run without a manifest is a run that did not happen.

Clock note: the ssh offsets are a CROSS-CHECK, not the alignment. ssh latency is
high and asymmetric, so these are good to maybe tens of ms. The trusted anchor is
the clapperboard -- the daemon's 2 s full-core burn at t=0, which appears in the
power trace as an unmistakable step and pins the two clocks to within one sample.
If the two disagree by more than 100 ms, the clapperboard wins and the
disagreement gets noted.

The dormancy timeout lives in the Arduino firmware, so this script cannot set it.
It is recorded in the manifest from --dormancy-ms and it is YOUR job to have
flashed the matching value. --confirm-dormancy makes the script stop and ask.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STOP_FILE = REPO / "fnirsi_stop"


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_argument_group("swept parameters")
    g.add_argument("--mean-interval", type=float, required=True,
                   help="mean black dwell, seconds -- sets the event rate")
    g.add_argument("--duration-ms", type=int, default=15000)
    g.add_argument("--contrast", type=float, default=0.8)
    g.add_argument("--dormancy-ms", type=int, required=True,
                   help="recorded only; it lives in the firmware. -1 = never halt")
    g.add_argument("--n-events", type=int, default=40)
    g.add_argument("--model", choices=["int8", "fp32"], default="int8")
    g.add_argument("--dwell-dist", choices=["exponential", "fixed"],
                   default="exponential")
    g.add_argument("--flicker-rate", type=float, default=0.0)
    g.add_argument("--flicker-contrast", type=float, default=0.15)
    g.add_argument("--trimmer", default="",
                   help="free text: Tier 0 trimmer position, for the ROC sweep")

    ap.add_argument("--host", default=os.environ.get("PI_HOST", "pi"),
                    help="ssh target for the Pi (default: $PI_HOST or 'pi')")
    ap.add_argument("--pi-repo", default="~/tiered-wakeup-visual-classification")
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
                    help="stop and ask whether the firmware matches --dormancy-ms")
    ap.add_argument("--dry-run", action="store_true",
                    help="stimulus schedule and manifest only; no hardware touched")
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = (f"i{args.mean_interval:g}_d{args.duration_ms}_"
            f"t{args.dormancy_ms}_c{args.contrast:g}_{args.model}")
    run_id = f"{stamp}_{slug}" + (f"_{args.tag}" if args.tag else "")
    run_dir = args.data_dir / run_id
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

        if not args.no_daemon and not args.dry_run:
            remote = (f"cd {args.pi_repo} && "
                      f"nohup .venv/bin/python pi/pi_daemon.py "
                      f"--model {args.model} --target-class {shlex.quote(args.target_class)} "
                      f"--out data/{run_id}/events.csv "
                      f"> data/{run_id}/daemon.log 2>&1 &")
            print(f"  daemon: ssh {args.host} '{remote}'")
            sh(["ssh", args.host, f"mkdir -p {args.pi_repo}/data/{run_id}"])
            sh(["ssh", args.host, remote])
            time.sleep(3.0)

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

    if not args.no_clock and not args.dry_run:
        manifest["clock_end"] = clock_offset(args.host)
        if manifest.get("clock_start") and manifest.get("clock_end"):
            drift = (manifest["clock_end"]["offset_s"]
                     - manifest["clock_start"]["offset_s"])
            manifest["clock_drift_s"] = drift
            print(f"  clock drift over the run: {drift:+.3f} s")
            if abs(drift) > 0.1:
                print("  NOTE: >100 ms of drift. Interpolate linearly between the"
                      "\n        two offsets, and trust the clapperboard over both.")

    if not args.no_daemon and not args.dry_run:
        src = f"{args.host}:{args.pi_repo}/data/{run_id}/"
        print(f"  fetching {src}")
        r = sh(["scp", "-q", f"{src}events.csv", f"{src}daemon.log", str(run_dir)])
        if r.returncode != 0:
            print(f"  scp failed: {r.stderr.strip()}\n"
                  f"  fetch by hand:  scp '{src}*' {run_dir}/", file=sys.stderr)

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

    print(f"\nnext:  analysis/energy_analysis.py {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
