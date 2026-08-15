#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Stage the SpeedKam zero-touch first-boot provisioner onto a freshly-flashed SD
# card's boot partition. Run this on the machine that flashed the card (Linux,
# macOS, a Raspberry Pi, or Windows via Git Bash / WSL) AFTER Raspberry Pi Imager
# has written the card and the boot partition is mounted.
#
#   Recommended flow:
#     1. Flash "Raspberry Pi OS (64-bit) Lite" with Raspberry Pi Imager and use
#        its OS customisation to set user / Wi-Fi / SSH / hostname / locale.
#     2. Run this script pointed at the mounted boot partition:
#
#          sudo bash deploy/image/prepare-boot.sh \
#            --boot /media/$USER/bootfs \
#            --config-local ./config.local.yaml
#
#        (Windows Git Bash example:  --boot /e/   where E: is the "bootfs" drive.)
#     3. Eject, insert into the Pi, power on. ~5 min later the dashboard is live
#        on :8080. Then calibrate on-site once. That's it.
#
# What it does:
#   * copies firstrun.sh + provision-node.sh onto the boot partition
#   * writes speedkam-provision.conf (repo URL / ref / user)
#   * copies your config.local.yaml (secrets) so the node comes up backing up
#   * wires the first-boot hook WITHOUT clobbering Imager's customisation:
#       - if Imager wrote a firstrun.sh, it's moved aside and chained
#       - otherwise we add the systemd.run hook to cmdline.txt ourselves and
#         drop an empty `ssh` file so you can still log in
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- defaults + args --------------------------------------------------------
BOOT=""
CONFIG_LOCAL=""
REPO_URL="https://github.com/krislovescalifornia/SpeedKam.git"
REPO_REF="main"
TARGET_USER=""

usage() {
  sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  echo
  echo "Options:"
  echo "  --boot <path>          Mounted boot partition (contains cmdline.txt). REQUIRED."
  echo "  --config-local <file>  config.local.yaml to ship (secrets). Optional but recommended."
  echo "  --repo <url>           Git URL to clone on the node. Default: ${REPO_URL}"
  echo "  --ref <branch|tag>     Git ref to check out. Default: ${REPO_REF}"
  echo "  --user <name>          Pin the node's login user. Default: auto-detect on the Pi."
  echo "  -h, --help             This help."
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --boot)         BOOT="${2:-}"; shift 2 ;;
    --config-local) CONFIG_LOCAL="${2:-}"; shift 2 ;;
    --repo)         REPO_URL="${2:-}"; shift 2 ;;
    --ref)          REPO_REF="${2:-}"; shift 2 ;;
    --user)         TARGET_USER="${2:-}"; shift 2 ;;
    -h|--help)      usage 0 ;;
    *) echo "Unknown option: $1" >&2; usage 1 ;;
  esac
done

[[ -n "${BOOT}" ]] || { echo "ERROR: --boot is required." >&2; usage 1; }
[[ -d "${BOOT}" ]] || { echo "ERROR: boot path not found: ${BOOT}" >&2; exit 1; }
if [[ ! -f "${BOOT}/cmdline.txt" ]]; then
  echo "ERROR: ${BOOT} has no cmdline.txt -- that isn't a Raspberry Pi boot partition." >&2
  echo "       Point --boot at the small FAT 'bootfs' volume, not the big root one." >&2
  exit 1
fi
if [[ -n "${CONFIG_LOCAL}" && ! -f "${CONFIG_LOCAL}" ]]; then
  echo "ERROR: --config-local file not found: ${CONFIG_LOCAL}" >&2
  exit 1
fi

echo "=============================================================="
echo " Staging SpeedKam zero-touch provisioner"
echo "   boot partition : ${BOOT}"
echo "   repo / ref     : ${REPO_URL} @ ${REPO_REF}"
echo "   user           : ${TARGET_USER:-<auto-detect on node>}"
echo "   config.local   : ${CONFIG_LOCAL:-<none — node runs without off-site backup>}"
echo "=============================================================="

# --- 1. copy the provisioner scripts ----------------------------------------
cp "${SCRIPT_DIR}/provision-node.sh" "${BOOT}/provision-node.sh"
echo " + provision-node.sh"

# --- 2. write the node config -----------------------------------------------
cat > "${BOOT}/speedkam-provision.conf" <<EOF
# Consumed by provision-node.sh on the node's first networked boot.
REPO_URL="${REPO_URL}"
REPO_REF="${REPO_REF}"
TARGET_USER="${TARGET_USER}"
EOF
echo " + speedkam-provision.conf"

# --- 3. ship secrets (optional) ---------------------------------------------
if [[ -n "${CONFIG_LOCAL}" ]]; then
  cp "${CONFIG_LOCAL}" "${BOOT}/config.local.yaml"
  echo " + config.local.yaml (will be shredded off the card after first boot)"
  if grep -q 'CHANGE-ME-to-a-long-random-string\|yourdomain.example' "${CONFIG_LOCAL}"; then
    echo "   WARNING: that config.local.yaml still has placeholder backup values." >&2
    echo "            Fill in real backup.url / backup.secret before you rely on backup." >&2
  fi
fi

# --- 4. wire the first-boot hook, coexisting with Imager --------------------
if [[ -f "${BOOT}/firstrun.sh" ]]; then
  # Imager customisation is present. Chain it: run Imager's identity setup first,
  # then ours. cmdline.txt already points systemd.run at firstrun.sh, so we just
  # swap in our wrapper and keep theirs alongside.
  mv "${BOOT}/firstrun.sh" "${BOOT}/speedkam-imager-firstrun.sh"
  cp "${SCRIPT_DIR}/firstrun.sh" "${BOOT}/firstrun.sh"
  echo " + firstrun.sh (chained after Imager's customisation)"
  echo "   (kept Imager's as speedkam-imager-firstrun.sh; cmdline.txt already hooks it)"
else
  # No Imager customisation. We own the hook: add systemd.run to cmdline.txt and
  # drop an empty `ssh` so the node is reachable. NOTE: with no Imager
  # customisation you must provide the login user + Wi-Fi another way (userconf.txt
  # / Ethernet) — see deploy/image/README.md.
  cp "${SCRIPT_DIR}/firstrun.sh" "${BOOT}/firstrun.sh"
  if ! grep -q 'systemd.run=' "${BOOT}/cmdline.txt"; then
    # cmdline.txt must stay a single line; append tokens with a leading space.
    sed -i 's|[[:space:]]*$| systemd.run=/boot/firmware/firstrun.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target|' \
      "${BOOT}/cmdline.txt"
    echo " + firstrun.sh (we own the hook) + appended systemd.run to cmdline.txt"
  else
    echo " + firstrun.sh — cmdline.txt already has a systemd.run hook; left it as-is." >&2
    echo "   If that hook isn't ours, chain manually (see README)." >&2
  fi
  [[ -f "${BOOT}/ssh" ]] || : > "${BOOT}/ssh"
  echo " + ssh (enabled headless login)"
  echo "   NOTE: no Imager customisation detected — make sure the node has a login"
  echo "         user (userconf.txt) and network (Ethernet or pre-seeded Wi-Fi)."
fi

# Best-effort flush to the physical card before you yank it.
sync

echo
echo "=============================================================="
echo " Done. Eject the card, boot the Pi, wait ~5 min, then:"
echo "   http://<hostname>.local:8080   (or the IP from your router)"
echo
echo " Watch it provision (if you have SSH):"
echo "   journalctl -u speedkam-provision -f"
echo "   tail -f /var/log/speedkam-provision.log"
echo
echo " Only manual step left: on-site calibration (dashboard -> Calibrate)."
echo "=============================================================="
