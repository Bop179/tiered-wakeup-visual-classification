#!/usr/bin/env bash
# Build the Mac-side virtualenv. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v brew >/dev/null; then
  echo "Homebrew not found. Install it first: https://brew.sh" >&2
  exit 1
fi

# hidapi is a C library; the Python `hidapi` wheel binds to it.
brew list hidapi >/dev/null 2>&1 || brew install hidapi

python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install -r tools/requirements.txt

echo
echo "Done. Use .venv/bin/python for the Mac-side tools:"
echo "  .venv/bin/python tools/fnb58_logger.py --check"
echo "  .venv/bin/python tools/event_display.py --dry-run --n-events 50 -o /tmp/gen.csv"
echo
echo "These need nothing installed and can be run with plain python3:"
echo "  python3 tools/mock_arduino.py --self-test"
echo "  python3 analysis/power_model.py --monte-carlo"
echo "  python3 tools/make_synthetic_run.py -o /tmp/syn && python3 analysis/energy_analysis.py /tmp/syn"
