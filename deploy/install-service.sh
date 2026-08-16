#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later
# Install SpeedKam as a systemd service so it autoruns on boot.
#
# Usage (on the Raspberry Pi, from anywhere):
#     sudo bash deploy/install-service.sh
#
# Optional: choose which Python to run with (defaults to the project .venv if it
# exists, otherwise the system python3 -- which is what you want when using the
# apt-installed python3-opencv / python3-picamera2 packages):
#     sudo PYTHON=/usr/bin/python3 bash deploy/install-service.sh
set -euo pipefail

SERVICE_NAME="speedkam"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

# --- resolve paths ----------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE="${SCRIPT_DIR}/speedkam.service"

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo:  sudo bash deploy/install-service.sh" >&2
  exit 1
fi

if [[ ! -f "${PROJECT_DIR}/run.py" ]]; then
  echo "Could not find run.py in ${PROJECT_DIR}. Run this from inside the project." >&2
  exit 1
fi

# --- who will the service run as -------------------------------------------
# The real login user, not root (systemd would otherwise run as root).
RUN_USER="${SUDO_USER:-${USER}}"
if [[ "${RUN_USER}" == "root" ]]; then
  echo "Refusing to install a root-owned camera service. Log in as a normal" >&2
  echo "user (e.g. 'pi') and run with sudo so SUDO_USER is set." >&2
  exit 1
fi

# --- pick the python interpreter -------------------------------------------
if [[ -n "${PYTHON:-}" ]]; then
  PY="${PYTHON}"
elif [[ -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
  PY="${PROJECT_DIR}/.venv/bin/python"
else
  PY="/usr/bin/python3"
fi

if [[ ! -x "${PY}" ]]; then
  echo "Python interpreter not executable: ${PY}" >&2
  exit 1
fi

# Sanity-check the dependencies are importable with the chosen interpreter.
if ! "${PY}" -c "import cv2, numpy, yaml, flask, requests" 2>/dev/null; then
  echo "WARNING: ${PY} cannot import cv2/numpy/yaml/flask/requests." >&2
  echo "  If using system python:  sudo apt install -y python3-opencv python3-picamera2 python3-yaml python3-flask python3-requests" >&2
  echo "  If using a venv for the Pi camera, create it with --system-site-packages." >&2
  echo "Continuing to install the unit anyway so you can fix and restart." >&2
fi

# --- make sure the user can reach the camera --------------------------------
if ! id -nG "${RUN_USER}" | tr ' ' '\n' | grep -qx video; then
  echo "Adding ${RUN_USER} to the 'video' group for camera access."
  usermod -aG video "${RUN_USER}"
fi

# --- render and install the unit -------------------------------------------
echo "Installing ${UNIT_PATH}"
echo "  user   : ${RUN_USER}"
echo "  dir    : ${PROJECT_DIR}"
echo "  python : ${PY}"

sed -e "s|__USER__|${RUN_USER}|g" \
    -e "s|__DIR__|${PROJECT_DIR}|g" \
    -e "s|__PYTHON__|${PY}|g" \
    "${TEMPLATE}" > "${UNIT_PATH}"

# Make sure the capture output dir exists and is owned by the run user.
mkdir -p "${PROJECT_DIR}/captures"
chown -R "${RUN_USER}" "${PROJECT_DIR}/captures"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
systemctl restart "${SERVICE_NAME}.service"

# --- Wi-Fi onboarding service (AP-mode setup portal when offline) -----------
# Optional but installed by default when NetworkManager + nmcli are present
# (Raspberry Pi OS Bookworm). Lets a headless node be joined to a new network
# from a phone with no re-imaging. Skipped silently on systems without nmcli.
NETCFG_TEMPLATE="${SCRIPT_DIR}/speedkam-netcfg.service"
if command -v nmcli >/dev/null 2>&1 && [[ -f "${NETCFG_TEMPLATE}" ]]; then
  echo
  echo "Installing Wi-Fi onboarding service (speedkam-netcfg)"
  sed -e "s|__DIR__|${PROJECT_DIR}|g" \
      -e "s|__PYTHON__|${PY}|g" \
      "${NETCFG_TEMPLATE}" > /etc/systemd/system/speedkam-netcfg.service
  systemctl daemon-reload
  systemctl enable speedkam-netcfg.service
  # Don't 'restart' here: on the provisioning host that would raise a setup AP
  # if this box happens to be offline. It runs on next boot, where it belongs.
  echo "  enabled; runs on next boot. Offline nodes will show a 'SpeedKam-Setup-*' Wi-Fi."
else
  echo
  echo "NOTE: nmcli not found -- skipping Wi-Fi onboarding service. (It needs"
  echo "      NetworkManager, the default on Raspberry Pi OS Bookworm.)"
fi

echo
echo "Done. SpeedKam will now start automatically on boot."
echo
echo "  Status : sudo systemctl status ${SERVICE_NAME}"
echo "  Logs   : journalctl -u ${SERVICE_NAME} -f"
echo "  Stop   : sudo systemctl stop ${SERVICE_NAME}"
echo "  Disable: sudo systemctl disable ${SERVICE_NAME}"
echo
echo "NOTE: calibrate first (python tools/calibrate.py) so it reports speed,"
echo "and confirm config.yaml points at the right camera. If you were just"
echo "added to the 'video' group, a reboot ensures it takes effect."
