#!/bin/bash
# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# SpeedKam zero-touch FIRST-RUN bootstrap (online path).
#
# This file lives on the SD card's FAT boot partition and is executed ONCE, very
# early on the first boot, by the standard Raspberry Pi mechanism:
#
#     cmdline.txt:  ... systemd.run=/boot/firmware/firstrun.sh \
#                       systemd.run_success_action=reboot \
#                       systemd.unit=kernel-command-line.target
#
# That is the exact same hook Raspberry Pi Imager uses for its OS customisation,
# so the two must not both own it. `prepare-boot.sh` handles that: if Imager
# already wrote a firstrun.sh (to set user / Wi-Fi / SSH / hostname), it is moved
# aside to `speedkam-imager-firstrun.sh` and we run it first, below, before doing
# our own work -- so you keep all of Imager's identity setup.
#
# We run at this stage with NO network (kernel-command-line.target). So all we do
# here is INSTALL a normal systemd service that will do the heavy, network-hungry
# provisioning (apt + git clone + install the SpeedKam service) on the *next*
# boot, once NetworkManager is up. Then the success-action reboot takes us there.
#
# See deploy/image/README.md ("Zero-touch: online first boot") for the recipe.
set +e

BOOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # /boot/firmware (usually)
STAMP="/var/lib/speedkam/firstrun-bootstrap.done"
mkdir -p "$(dirname "${STAMP}")"

log() { echo "[speedkam-firstrun] $*"; }

# Idempotent: never do this twice.
if [[ -f "${STAMP}" ]]; then
  log "already bootstrapped; nothing to do."
  exit 0
fi

log "bootstrapping from ${BOOT_DIR}"

# --- 1. run Imager's original identity setup first, if we chained it ---------
# prepare-boot.sh renamed Imager's firstrun.sh to this when present. Run it as a
# child (not `source`) so its `exit 0` doesn't end us. It sets hostname / user /
# Wi-Fi / SSH and typically de-hooks cmdline + removes /boot/firmware/firstrun.sh
# on its way out -- all of which is fine and desirable for us.
IMAGER="${BOOT_DIR}/speedkam-imager-firstrun.sh"
if [[ -f "${IMAGER}" ]]; then
  log "running Imager's customisation (${IMAGER})..."
  bash "${IMAGER}"
  log "Imager customisation returned $?"
  rm -f "${IMAGER}"
fi

# --- 2. install the Phase-2 provisioning service ----------------------------
# It is ordered after the network is online and self-disables once done.
cat > /etc/systemd/system/speedkam-provision.service <<'UNIT'
[Unit]
Description=SpeedKam first-boot online provisioning (apt + git + service install)
Wants=network-online.target
After=network-online.target
# Only until it has provisioned this node once.
ConditionPathExists=!/var/lib/speedkam/provision.done

[Service]
Type=oneshot
RemainAfterExit=yes
# provision-node.sh is staged on the boot partition by prepare-boot.sh.
ExecStart=/bin/bash /boot/firmware/provision-node.sh
TimeoutStartSec=1800
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable speedkam-provision.service
log "installed + enabled speedkam-provision.service (runs on next boot, online)"

# --- 3. make sure we never run again ----------------------------------------
# If Imager's script already de-hooked cmdline + removed this file, these are
# no-ops. If we own the hook (no Imager customisation), strip it ourselves.
if [[ -f /boot/firmware/cmdline.txt ]]; then
  sed -i 's| systemd.run=[^ ]*||g; s| systemd.run_success_action=[^ ]*||g; s| systemd.unit=kernel-command-line.target||g' \
    /boot/firmware/cmdline.txt
fi
rm -f "${BOOT_DIR}/firstrun.sh"

date -u '+%Y-%m-%dT%H:%M:%SZ' > "${STAMP}"
log "bootstrap complete; rebooting into online provisioning."
exit 0
