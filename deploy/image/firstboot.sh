#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later
# Runs ONCE on the first boot of every cloned SD card, then disables itself.
#
# A raw disk clone copies the master's identity onto every card. That's fine for
# the SpeedKam code, but three things MUST be unique per node or a fleet breaks:
#   1. SSH host keys   -- identical keys let one stolen key impersonate all nodes
#   2. machine-id      -- collides in DHCP leases, journald, avahi, etc.
#   3. hostname        -- so you can tell nodes apart on the network
#
# This is installed + enabled by provision.sh via speedkam-firstboot.service.
set -euo pipefail

STAMP="/var/lib/speedkam/firstboot.done"
mkdir -p "$(dirname "${STAMP}")"

# Idempotent: if we've already personalised this card, do nothing.
if [[ -f "${STAMP}" ]]; then
  exit 0
fi

echo "[speedkam-firstboot] personalising this card..."

# --- 1. fresh SSH host keys -------------------------------------------------
if command -v ssh-keygen >/dev/null 2>&1; then
  rm -f /etc/ssh/ssh_host_*
  # Regenerate the standard set. dpkg-reconfigure is the canonical way on Debian.
  if command -v dpkg-reconfigure >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive dpkg-reconfigure openssh-server >/dev/null 2>&1 || \
      ssh-keygen -A
  else
    ssh-keygen -A
  fi
  echo "[speedkam-firstboot] regenerated SSH host keys"
fi

# --- 2. fresh machine-id ----------------------------------------------------
rm -f /etc/machine-id /var/lib/dbus/machine-id
systemd-machine-id-setup >/dev/null 2>&1 || true
# Keep the dbus copy in sync (symlink on modern Pi OS; copy as a fallback).
if [[ -e /etc/machine-id && ! -e /var/lib/dbus/machine-id ]]; then
  ln -sf /etc/machine-id /var/lib/dbus/machine-id 2>/dev/null || \
    cp /etc/machine-id /var/lib/dbus/machine-id
fi
echo "[speedkam-firstboot] regenerated machine-id"

# --- 3. unique hostname from the Pi serial ----------------------------------
# The last 6 hex of the CPU serial is stable per board and human-distinguishable.
SERIAL="$(grep -m1 -i '^Serial' /proc/cpuinfo 2>/dev/null | awk '{print $3}')"
if [[ -n "${SERIAL:-}" ]]; then
  SUFFIX="${SERIAL: -6}"
else
  # Fallback: last 6 hex of the first ethernet MAC.
  SUFFIX="$(cat /sys/class/net/*/address 2>/dev/null | head -n1 | tr -d ':' | tail -c 6)"
fi
NEWHOST="speedkam-${SUFFIX:-unknown}"

OLDHOST="$(hostnamectl --static 2>/dev/null || cat /etc/hostname)"
if command -v hostnamectl >/dev/null 2>&1; then
  hostnamectl set-hostname "${NEWHOST}"
else
  echo "${NEWHOST}" > /etc/hostname
fi
# Keep /etc/hosts consistent so sudo doesn't complain about name resolution.
if grep -q "127.0.1.1" /etc/hosts 2>/dev/null; then
  sed -i "s/127.0.1.1.*/127.0.1.1\t${NEWHOST}/" /etc/hosts
else
  printf '127.0.1.1\t%s\n' "${NEWHOST}" >> /etc/hosts
fi
echo "[speedkam-firstboot] hostname ${OLDHOST} -> ${NEWHOST}"

# --- done: stamp + disable self so this never runs again --------------------
date -u '+%Y-%m-%dT%H:%M:%SZ' > "${STAMP}"
systemctl disable speedkam-firstboot.service >/dev/null 2>&1 || true

echo "[speedkam-firstboot] done. SpeedKam dashboard will come up on :8080."
