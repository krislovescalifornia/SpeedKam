# SpeedKam golden-image deployment

Build **one** fully-installed SpeedKam SD card, capture it to an `.img`, and
clone that image to as many cards as you want. Each clone boots straight into
the dashboard on port **8080** — no per-card setup beyond on-site calibration.

This is the *golden-master* approach: simplest, works today, no Linux build host
required. (A from-scratch `pi-gen` build is the more "reproducible CI" route; see
the note at the bottom if you'd rather go that way.)

---

## What ends up on the image

- Raspberry Pi OS + all SpeedKam runtime deps from **apt** (no pip/venv):
  `python3-opencv numpy pyyaml flask requests picamera2`.
- The SpeedKam project itself.
- The **autostart service** (`speedkam.service`) → dashboard on `:8080`, restarts
  on crash. Same one `deploy/install-service.sh` installs.
- A **first-boot personaliser** (`speedkam-firstboot.service`) that, the first
  time *each clone* powers on, regenerates SSH host keys + machine-id and sets a
  unique hostname `speedkam-<serial>`.

**Deliberately NOT on the image:** `calibration.json`, `captures/`, the backup
`.sync_queue`, or a `.venv`. Calibration is per-install and captures are per-node
— `provision.sh` wipes them so every card starts blank.

---

## Step 1 — build the master (once)

1. Flash a normal **Raspberry Pi OS (64-bit)** card with Raspberry Pi Imager. In
   the Imager's ⚙️ settings, set your user, Wi‑Fi, locale, and **enable SSH** —
   these become the fleet defaults, so set them the way you want every node.
2. Boot that Pi, then copy this project onto it (git clone or `scp` the folder)
   into the home directory, e.g. `~/SpeedKam`.
3. From inside the project, run the provisioner:

   ```bash
   sudo bash deploy/image/provision.sh
   ```

   It installs every dependency, installs both services, wipes per-node state,
   and stops the service again so the master doesn't record anything.
4. Set the **shared backup** credentials in `config.yaml` — every clone inherits
   them, so fill in the real `backup.url` and `backup.secret` on the master now.
   `provision.sh` **refuses to build** while they're still placeholders, so you
   can't accidentally ship a fleet that can't back up. (Don't want off-site
   backup at all? Set `backup.enabled: false`.)

   Other fleet defaults already shipped in `config.yaml`:
   - `speed.display_units: mph` (Imperial)
   - `recording.speedkapture_threshold: 25` (only clip cars over 25 mph; slower
     ones are still counted)
   - `camera.backend: auto` — **each node polls on boot** and uses a Pi CSI
     camera if one is attached, otherwise a USB webcam. So the *same* image runs
     on a CSI-camera node and a USB-webcam node with no per-card edit.

   **Leave calibration alone** — it's done on-site.
5. Shut the master down cleanly:

   ```bash
   sudo shutdown -h now
   ```

Do **not** boot this card again before capturing it — booting it would trip its
own first-boot reset and re-arm nothing. If you do boot it, re-arm with
`sudo rm -f /var/lib/speedkam/firstboot.done && sudo systemctl enable speedkam-firstboot.service`.

---

## Step 2 — capture + shrink the image

Move the master card to a computer with a card reader.

### On Linux / macOS / Raspberry Pi

```bash
# Find the card's device (e.g. /dev/sdX or /dev/mmcblk0). BE SURE it's the card.
lsblk

# Read the whole card to a file (use the RAW device; unmount partitions first).
sudo dd if=/dev/sdX of=speedkam-golden.img bs=4M status=progress conv=fsync
```

That `.img` is the full card size (e.g. 64 GB). Shrink it so it flashes fast and
fits smaller cards, with [PiShrink](https://github.com/Drewsif/PiShrink):

```bash
sudo pishrink.sh -Z speedkam-golden.img        # -Z also gzips it
```

Shrinking auto-expands the filesystem to fill each target card on first boot.

### On Windows

Use **Win32 Disk Imager** → *Read* to pull `speedkam-golden.img` off the card.
Windows has no PiShrink; either flash the full-size image (fine, just slower) or
run PiShrink from WSL2 against the `.img`.

---

## Step 3 — clone to every deployment card

Flash `speedkam-golden.img` to each new card with **Raspberry Pi Imager**
(*Use custom* → pick the image) or Win32 Disk Imager / `dd`.

> Skip the Imager's OS-customisation for clones — the image already carries your
> user/Wi‑Fi/SSH, and the first-boot step handles the unique bits.

Pop a cloned card into a Pi, give it power, and after ~1 minute browse to
`http://speedkam-<serial>.local:8080` (or the IP from your router). First boot is
slightly slower — it's regenerating identity and expanding the filesystem.

---

## Step 4 — on-site, per node (the only manual bit)

Calibration is physical: each camera sees a different road, so it can't be
imaged. On each installed node, once, in its final mounted position:

- Dashboard → **Calibrate**, click the measured points, Save — or
- `python3 tools/calibrate.py`

Then `sudo systemctl restart speedkam`. That's the whole per-node checklist.

---

## Re-imaging after a code change

When you update SpeedKam, you don't need a fresh OS: on the master, `git pull`
(or re-copy), `sudo bash deploy/image/provision.sh` again, shut down, and
re-capture. Or just `git pull && sudo systemctl restart speedkam` on each live
node if you'd rather not re-clone.

---

## Alternative: reproducible build with pi-gen

If you want the image built from scratch in CI instead of captured from a running
card, use Raspberry Pi's [`pi-gen`](https://github.com/RPi-Distro/pi-gen) on a
Linux/Docker host: add a custom stage whose `run.sh` does what `provision.sh`
does (apt install the deps, drop the project into `/home/<user>/SpeedKam`, enable
both services). More setup, but fully reproducible and no golden card to babysit.
The `provision.sh` in this folder is written so its body ports almost verbatim
into a pi-gen stage.
