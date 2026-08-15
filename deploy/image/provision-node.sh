#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# SpeedKam zero-touch PROVISIONING (Phase 2 of the online first-boot path).
#
# Installed + enabled by firstrun.sh as `speedkam-provision.service`, this runs
# ONCE on the first *networked* boot of a freshly-flashed stock Raspberry Pi OS
# card and turns it into a running SpeedKam node with no human touch:
#
#     wait for network -> apt install deps -> git clone the project ->
#     drop in config.local.yaml (secrets) -> install the autostart service ->
#     disable + remove itself.
#
# It reads its parameters from `/boot/firmware/speedkam-provision.conf` (staged
# by prepare-boot.sh). Everything is idempotent and guarded by a stamp file, so a
# reboot mid-run just resumes cleanly on the next boot.
#
# Progress is visible live:  journalctl -u speedkam-provision -f
# and is also appended to    /var/log/speedkam-provision.log
set -uo pipefail

STAMP="/var/lib/speedkam/provision.done"
LOG="/var/log/speedkam-provision.log"
BOOT_DIR="/boot/firmware"
[[ -d "${BOOT_DIR}" ]] || BOOT_DIR="/boot"   # pre-Bookworm fallback
CONF="${BOOT_DIR}/speedkam-provision.conf"

mkdir -p "$(dirname "${STAMP}")"
# Tee everything to a persistent log as well as the journal.
exec > >(tee -a "${LOG}") 2>&1

log()  { echo "[speedkam-provision] $*"; }
fail() { echo "[speedkam-provision] ERROR: $*" >&2; exit 1; }

if [[ -f "${STAMP}" ]]; then
  log "already provisioned ($(cat "${STAMP}")); nothing to do."
  exit 0
fi

echo "=============================================================="
log "starting online provisioning at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

# --- parameters -------------------------------------------------------------
REPO_URL="https://github.com/krislovescalifornia/SpeedKam.git"
REPO_REF="main"
TARGET_USER=""
PROJECT_DIR=""
# shellcheck disable=SC1090
[[ -f "${CONF}" ]] && { log "reading ${CONF}"; source "${CONF}"; }

# Auto-detect the login user if not pinned in the conf (first normal account).
if [[ -z "${TARGET_USER}" ]]; then
  TARGET_USER="$(getent passwd | awk -F: '$3>=1000 && $3<65534 {print $1; exit}')"
fi
[[ -n "${TARGET_USER}" ]] || fail "could not determine a target user; set TARGET_USER in ${CONF}"
USER_HOME="$(getent passwd "${TARGET_USER}" | cut -d: -f6)"
[[ -n "${PROJECT_DIR}" ]] || PROJECT_DIR="${USER_HOME}/SpeedKam"

log "user=${TARGET_USER} home=${USER_HOME}"
log "repo=${REPO_URL} ref=${REPO_REF} -> ${PROJECT_DIR}"

# --- 1. wait for the network ------------------------------------------------
log "waiting for network + DNS (up to 5 min)..."
systemctl start network-online.target 2>/dev/null || true
for i in $(seq 1 60); do
  if getent hosts github.com >/dev/null 2>&1; then
    log "network is up (after ~$(( (i-1) * 5 ))s)."
    break
  fi
  sleep 5
  [[ $i -eq 60 ]] && fail "no network after 5 min; check Wi-Fi/Ethernet, then: sudo systemctl restart speedkam-provision"
done

# --- 2. system dependencies (apt only -- no pip/venv on the node) -----------
log ">> apt update + install runtime deps (the slow step)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update || fail "apt-get update failed"
apt-get install -y --no-install-recommends \
  git \
  python3 \
  python3-opencv \
  python3-numpy \
  python3-yaml \
  python3-flask \
  python3-requests \
  python3-picamera2 || fail "apt-get install failed"

if ! /usr/bin/python3 -c "import cv2, numpy, yaml, flask, requests" 2>/dev/null; then
  fail "system python3 still can't import the core stack after install."
fi
log "   core stack imports OK."

# --- 3. fetch the project ---------------------------------------------------
if [[ -d "${PROJECT_DIR}/.git" ]]; then
  log ">> project already present; fetching ${REPO_REF}..."
  sudo -u "${TARGET_USER}" git -C "${PROJECT_DIR}" fetch --depth 1 origin "${REPO_REF}" \
    && sudo -u "${TARGET_USER}" git -C "${PROJECT_DIR}" checkout -f "${REPO_REF}" \
    || log "   (fetch failed; keeping existing checkout)"
else
  log ">> git clone ${REPO_URL} (${REPO_REF})..."
  sudo -u "${TARGET_USER}" git clone --depth 1 --branch "${REPO_REF}" \
    "${REPO_URL}" "${PROJECT_DIR}" \
    || sudo -u "${TARGET_USER}" git clone --depth 1 "${REPO_URL}" "${PROJECT_DIR}" \
    || fail "git clone failed"
fi
chown -R "${TARGET_USER}:${TARGET_USER}" "${PROJECT_DIR}"

# --- 4. drop in secrets (config.local.yaml) ---------------------------------
# Staged on the boot partition by prepare-boot.sh. It holds backup url/secret and
# optional dashboard auth. We copy it in, lock it down, then SHRED it off the FAT
# boot partition so the secret isn't left readable on an easily-removed card.
LOCAL_SRC="${BOOT_DIR}/config.local.yaml"
if [[ -f "${LOCAL_SRC}" ]]; then
  log ">> installing config.local.yaml (secrets) and wiping it off the boot partition..."
  install -m 600 -o "${TARGET_USER}" -g "${TARGET_USER}" \
    "${LOCAL_SRC}" "${PROJECT_DIR}/config.local.yaml"
  shred -u "${LOCAL_SRC}" 2>/dev/null || rm -f "${LOCAL_SRC}"
else
  log ">> no config.local.yaml staged; node will run without off-site backup"
  log "   (set backup later in ${PROJECT_DIR}/config.local.yaml if you want it)."
fi

# --- 5. install the autostart service ---------------------------------------
# Reuse the same installer the manual path uses. We're root under systemd, so
# hand it SUDO_USER explicitly (that's how install-service.sh learns the run user)
# and pin the system interpreter.
log ">> installing the SpeedKam autostart service..."
SUDO_USER="${TARGET_USER}" PYTHON=/usr/bin/python3 \
  bash "${PROJECT_DIR}/deploy/install-service.sh" || fail "install-service.sh failed"

# --- 6. done: stamp, then remove ourselves so this never runs again ---------
date -u '+%Y-%m-%dT%H:%M:%SZ' > "${STAMP}"
systemctl disable speedkam-provision.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/speedkam-provision.service
systemctl daemon-reload 2>/dev/null || true
rm -f "${BOOT_DIR}/provision-node.sh" "${BOOT_DIR}/speedkam-provision.conf"

echo "=============================================================="
log "PROVISIONED. SpeedKam is starting on http://$(hostname -I | awk '{print $1}'):8080"
log "next step is the only manual one: on-site calibration (dashboard -> Calibrate)."
echo "=============================================================="
exit 0
