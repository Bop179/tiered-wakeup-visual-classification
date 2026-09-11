#!/usr/bin/env bash
# Install pi/tier3-daemon.service for this checkout. Run once, on the Pi:
#
#     bash pi/install_service.sh
#
# The unit starts pi/run_current.sh at every boot, but only while
# data/current_run.env exists -- which is only during a run.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SUDO_USER:-$(id -un)}"
UNIT=/etc/systemd/system/tier3-daemon.service

chmod +x "$REPO/pi/run_current.sh"

if ! id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx dialout; then
  echo "WARNING: $USER_NAME is not in the dialout group, so the daemon cannot open the UART." >&2
  echo "         Fix: sudo usermod -aG dialout $USER_NAME   (then reboot)" >&2
fi
if [ ! -x "$REPO/.venv/bin/python" ]; then
  echo "WARNING: $REPO/.venv/bin/python is missing -- build the venv first:" >&2
  echo "         python3 -m venv --system-site-packages .venv && .venv/bin/pip install --no-cache-dir -r pi/requirements.txt" >&2
fi
if ! sudo -n true 2>/dev/null; then
  echo "WARNING: sudo asks for a password, so the daemon's 'sudo halt' will hang." >&2
fi

sed -e "s|@REPO@|$REPO|g" -e "s|@USER@|$USER_NAME|g" "$REPO/pi/tier3-daemon.service" \
  | sudo tee "$UNIT" >/dev/null
sudo systemd-analyze verify "$UNIT"
sudo systemctl daemon-reload
sudo systemctl enable tier3-daemon.service

echo "installed $UNIT"
echo "  enabled at boot; it only starts while $REPO/data/current_run.env exists"
echo "  logs: data/<run_id>/daemon.log   status: systemctl status tier3-daemon"
