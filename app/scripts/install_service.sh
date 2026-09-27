#!/usr/bin/env bash
#
# Install (or update) the systemd service so the station starts at boot.
# Run on the Pi from the repo root:  ./scripts/install_service.sh
# Logs:  journalctl -u sleepradiopi -f

set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
sudo install -m 644 "$here/sleepradiopi.service" /etc/systemd/system/sleepradiopi.service
sudo systemctl daemon-reload
sudo systemctl enable sleepradiopi.service
sudo systemctl restart sleepradiopi.service
systemctl --no-pager --lines=5 status sleepradiopi.service
