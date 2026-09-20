#!/usr/bin/env bash
# Started by pi/tier3-daemon.service at every boot -- including every wake from halt.
#
# Reads the run pointer that tools/run_experiment.py writes, data/current_run.env,
# and starts pi/pi_daemon.py for that run, appending to its events.csv. No pointer,
# no daemon: run_experiment.py deletes the file when the run is over, so an
# ordinary reboot starts nothing.
#
# The first start of a run burns the 2 s clapperboard. Every later start is a
# restart after the Arduino woke the Pi, and passes --clapperboard 0: the burn runs
# before "# ready", so repeating it would add 2 s and several joules to every boot
# the experiment measures.
#
# current_run.env is shell syntax, values quoted:
#     RUN_ID=20260914T101500Z_i10_d25000_t15000_c0.8_int8
#     MODEL=int8
#     TARGET_CLASS=banana
#     DORMANCY_MS=15000          # optional; empty = do not SET
#     PORT=/dev/serial0          # optional
#     EXTRA_ARGS=                # optional, appended to the daemon command
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO/data/current_run.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "tier3: no $ENV_FILE -- no run is live, not starting" >&2
  exit 0
fi

RUN_ID="" MODEL="int8" TARGET_CLASS="banana" DORMANCY_MS="" PORT="/dev/serial0" EXTRA_ARGS=""
# shellcheck disable=SC1090
. "$ENV_FILE"
if [ -z "$RUN_ID" ]; then
  echo "tier3: RUN_ID missing from $ENV_FILE" >&2
  exit 1
fi

RUN_DIR="$REPO/data/$RUN_ID"
mkdir -p "$RUN_DIR"
LOG="$RUN_DIR/daemon.log"
PY="${TIER3_PYTHON:-$REPO/.venv/bin/python}"

# The UART can appear a moment after this unit starts. Waiting for the path is
# simpler and sturdier than ordering on a device unit whose name depends on the
# disable-bt overlay.
for _ in $(seq 1 50); do
  [ -e "$PORT" ] && break
  sleep 0.2
done
if [ ! -e "$PORT" ]; then
  echo "# tier3 wrapper: $PORT never appeared -- UART not configured?" >> "$LOG"
  exit 1
fi

if [ -e "$RUN_DIR/.started" ]; then
  CLAP=0
else
  CLAP=2
  : > "$RUN_DIR/.started"
fi

args=(--port "$PORT" --model "$MODEL" --target-class "$TARGET_CLASS"
      --out "$RUN_DIR/events.csv" --boots-out "$RUN_DIR/boots.csv"
      --clapperboard "$CLAP" --exit-dormancy -1)
if [ -n "$DORMANCY_MS" ]; then
  args+=(--dormancy-ms "$DORMANCY_MS")
fi

echo "# tier3 wrapper start $(date -u +%Y-%m-%dT%H:%M:%SZ) uptime=$(cut -d' ' -f1 /proc/uptime 2>/dev/null || echo ?) clapperboard=${CLAP}s" >> "$LOG"
cd "$REPO"
# EXTRA_ARGS is split on purpose.
# shellcheck disable=SC2086
exec "$PY" pi/pi_daemon.py "${args[@]}" $EXTRA_ARGS >> "$LOG" 2>&1
