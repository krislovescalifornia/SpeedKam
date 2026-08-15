#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# SpeedKam auto-update: pull the latest code when the node is online, and restart
# the service only if something actually changed. Triggered by
# speedkam-update.timer (on boot + daily).
#
# Safe by design for an unattended fleet:
#   * never fails the boot (always exits 0)
#   * never leaves the dashboard down (only restarts on a successful fast-forward)
#   * a no-network / offline boot is a clean no-op -- the baked-in code keeps running
#
# provision.sh renders __DIR__/__USER__ and installs this on the golden master.
set -uo pipefail

DIR="__DIR__"
RUN_USER="__USER__"
LOG="/var/log/speedkam-update.log"

log() { echo "[speedkam-update] $(date -u '+%Y-%m-%dT%H:%M:%SZ') $*" | tee -a "${LOG}"; }

command -v git >/dev/null 2>&1 || { log "git not installed; skipping."; exit 0; }
[ -d "${DIR}/.git" ]          || { log "no git checkout at ${DIR}; skipping.";  exit 0; }

# Offline boot? Clean no-op -- the image already carries a working copy.
if ! getent hosts github.com >/dev/null 2>&1; then
  log "no network/DNS; keeping the baked-in version this boot."
  exit 0
fi

before="$(sudo -u "${RUN_USER}" git -C "${DIR}" rev-parse HEAD 2>/dev/null || echo none)"
if ! sudo -u "${RUN_USER}" git -C "${DIR}" pull --ff-only >>"${LOG}" 2>&1; then
  log "git pull failed (diverged/offline/repo error); keeping current version."
  exit 0
fi
after="$(sudo -u "${RUN_USER}" git -C "${DIR}" rev-parse HEAD 2>/dev/null || echo none)"

if [ "${before}" != "${after}" ]; then
  log "updated ${before} -> ${after}; restarting speedkam."
  systemctl restart speedkam.service || log "restart failed; will retry next cycle."
else
  log "already up to date (${after})."
fi
exit 0
