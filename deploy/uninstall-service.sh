#!/usr/bin/env bash
# Remove the SpeedKam systemd service.
#     sudo bash deploy/uninstall-service.sh
set -euo pipefail

SERVICE_NAME="speedkam"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo:  sudo bash deploy/uninstall-service.sh" >&2
  exit 1
fi

systemctl stop "${SERVICE_NAME}.service" 2>/dev/null || true
systemctl disable "${SERVICE_NAME}.service" 2>/dev/null || true
rm -f "${UNIT_PATH}"
systemctl daemon-reload
echo "Removed ${SERVICE_NAME}. (Captures and config are left untouched.)"
