#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later
# Provision a fresh Raspberry Pi OS into a ready-to-clone SpeedKam node.
#
# Run this ONCE, on the Raspberry Pi you will turn into the golden master,
# from inside the project folder:
#
#     sudo bash deploy/image/provision.sh
#
# It installs every runtime dependency from apt (no pip/venv needed on the Pi),
# installs the autostart service, and arms a first-boot step that gives every
# cloned SD card a unique identity. After it finishes, follow
# deploy/image/README.md to capture and clone the card.
set -euo pipefail

# --- resolve paths ----------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo:  sudo bash deploy/image/provision.sh" >&2
  exit 1
fi

if [[ ! -f "${PROJECT_DIR}/serve.py" ]]; then
  echo "Could not find serve.py in ${PROJECT_DIR}. Run this from inside the project." >&2
  exit 1
fi

# Who will own the app + run the service. Prefer sudo's invoking user, then $USER,
# then fall back to the first normal login account (uid 1000) -- so this also works
# when launched non-interactively (systemd-run/cron), where neither var is set.
RUN_USER="${SUDO_USER:-${USER:-}}"
if [[ -z "${RUN_USER}" || "${RUN_USER}" == "root" ]]; then
  RUN_USER="$(getent passwd 1000 | cut -d: -f1)"
fi
if [[ -z "${RUN_USER}" || "${RUN_USER}" == "root" ]]; then
  echo "Refusing to provision as root and could not find a normal login user." >&2
  echo "Create a non-root user (e.g. 'pi') first, or pass SUDO_USER=<name>." >&2
  exit 1
fi

echo "=============================================================="
echo " SpeedKam golden-image provisioning"
echo "   project : ${PROJECT_DIR}"
echo "   user    : ${RUN_USER}"
echo "=============================================================="

# --- system dependencies (apt only -- no pip on the Pi) ---------------------
# These cover: import cv2, numpy, yaml, flask, requests  + the native CSI camera
# (picamera2). Everything the systemd unit checks for is here.
echo
echo ">> Installing system packages (this is the slow step)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  git \
  python3 \
  python3-opencv \
  python3-numpy \
  python3-yaml \
  python3-flask \
  python3-requests \
  python3-picamera2

# Sanity-check the interpreter can import the whole stack.
if ! /usr/bin/python3 -c "import cv2, numpy, yaml, flask, requests" 2>/dev/null; then
  echo "ERROR: system python3 still can't import the core stack after install." >&2
  echo "Fix the apt errors above and re-run before capturing an image." >&2
  exit 1
fi
echo "   core stack imports OK."

# --- guard: don't ship a fleet with a placeholder backup secret -------------
# The tracked config.yaml only ever holds placeholders; the REAL url/secret live
# in the untracked config.local.yaml overlay, which rides along on every clone.
# If backup is enabled but that overlay is missing or still has placeholders, all
# nodes would queue failing uploads -- catch it here, before the image is taken.
CONFIG="${PROJECT_DIR}/config.yaml"
LOCAL="${PROJECT_DIR}/config.local.yaml"
if grep -Eq '^[[:space:]]*enabled:[[:space:]]*true' "${CONFIG}" 2>/dev/null && \
   grep -q 'backup:' "${CONFIG}" 2>/dev/null; then
  if [[ ! -f "${LOCAL}" ]] \
     || ! grep -q 'secret:' "${LOCAL}" \
     || grep -q 'CHANGE-ME-to-a-long-random-string' "${LOCAL}" \
     || grep -q 'yourdomain.example' "${LOCAL}"; then
    echo >&2
    echo "ERROR: backup is enabled but config.local.yaml has no real url/secret." >&2
    echo "       Every cloned node shares this overlay, so set the REAL values" >&2
    echo "       on this master before imaging. Create config.local.yaml from" >&2
    echo "       config.local.example.yaml and fill in:" >&2
    echo "         - backup.url    -> your receiver URL" >&2
    echo "         - backup.secret -> the same long random string as the PHP host" >&2
    echo "       Then re-run this script. (Or set backup.enabled: false in" >&2
    echo "       config.yaml to ship without off-site backup.)" >&2
    exit 1
  fi
fi

# --- make sure the image ships BLANK, not with this Pi's local state --------
# Crossing-time calibration (speed.d_east_m / d_west_m in config.local.yaml) is
# per-physical-install; captures + runtime state are per-node. None belongs in a
# shared image. (They're gitignored, but a copied folder can carry them.)
echo
echo ">> Clearing per-node state so the image starts blank..."
# calibration.json / .ref.jpg are legacy homography artifacts -- removed if a
# copied folder still carries one from before the crossing-time engine.
rm -f  "${PROJECT_DIR}/calibration.json" \
       "${PROJECT_DIR}/calibration.ref.jpg"
rm -rf "${PROJECT_DIR}/captures" "${PROJECT_DIR}/captures_test" \
       "${PROJECT_DIR}/speedkam_data"
find "${PROJECT_DIR}" -type d -name '.sync_queue' -exec rm -rf {} + 2>/dev/null || true
find "${PROJECT_DIR}" -type d -name '__pycache__'  -exec rm -rf {} + 2>/dev/null || true
# A venv built on Windows or another Pi is useless here; we run system python.
rm -rf "${PROJECT_DIR}/.venv"
install -d -o "${RUN_USER}" -g "${RUN_USER}" "${PROJECT_DIR}/captures"

# --- install the autostart service (uses system python3) --------------------
echo
echo ">> Installing the SpeedKam autostart service..."
PYTHON=/usr/bin/python3 bash "${PROJECT_DIR}/deploy/install-service.sh"

# We just captured an image, so don't leave THIS master running/collecting.
systemctl stop speedkam.service 2>/dev/null || true

# --- arm the first-boot identity reset for every clone ----------------------
echo
echo ">> Arming the per-clone first-boot identity reset..."
sed -e "s|__DIR__|${PROJECT_DIR}|g" \
    "${SCRIPT_DIR}/speedkam-firstboot.service" \
    > /etc/systemd/system/speedkam-firstboot.service
systemctl daemon-reload
systemctl enable speedkam-firstboot.service

# --- bake in auto-update-on-network -----------------------------------------
# So the golden image isn't frozen: every card pulls the latest code when it has
# a network and restarts SpeedKam only if something changed. Offline boots are a
# clean no-op. This is what makes "after-image work" happen automatically.
echo
echo ">> Installing auto-update (git pull on boot + daily)..."
sed -e "s|__DIR__|${PROJECT_DIR}|g" -e "s|__USER__|${RUN_USER}|g" \
    "${PROJECT_DIR}/deploy/speedkam-update.sh" \
    > /usr/local/sbin/speedkam-update.sh
chmod 0755 /usr/local/sbin/speedkam-update.sh
# Point the unit at the installed copy (not the in-repo template path).
sed -e "s|/bin/bash __DIR__/deploy/speedkam-update.sh|/bin/bash /usr/local/sbin/speedkam-update.sh|g" \
    "${PROJECT_DIR}/deploy/speedkam-update.service" \
    > /etc/systemd/system/speedkam-update.service
cp "${PROJECT_DIR}/deploy/speedkam-update.timer" /etc/systemd/system/speedkam-update.timer
systemctl daemon-reload
systemctl enable speedkam-update.timer

echo
echo "=============================================================="
echo " Provisioning complete."
echo
echo " This card is now a golden master. Next:"
echo "   1. sudo shutdown -h now"
echo "   2. Move the card to another machine and capture + shrink it"
echo "      (see deploy/image/README.md)."
echo "   3. Clone that .img to as many cards as you like."
echo
echo " Each cloned card will, on its FIRST boot:"
echo "   - regenerate SSH host keys + machine-id (no fleet collisions)"
echo "   - set a unique hostname from the Pi serial (speedkam-XXXXXX)"
echo " then start SpeedKam and serve the dashboard on port 8080."
echo
echo " On each deployed node you still calibrate on-site once: drive past each"
echo " way at a known speed, read the crossing time from the log, and set"
echo " speed.d_east_m / speed.d_west_m in config.local.yaml (the dashboard's"
echo " Calibration page has a distance calculator). Then restart speedkam."
echo "=============================================================="
