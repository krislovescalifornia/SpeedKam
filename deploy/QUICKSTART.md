<!-- SPDX-FileCopyrightText: 2026 Kris Kling -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# SpeedKam Pi imaging — quick checklist

The zero-touch path: flash a **stock** Raspberry Pi OS card, stage three files on
it, power on. The card installs itself on first boot and comes up serving the
dashboard on **:8080** — no SSH, no typing.

**One requirement:** the Pi needs **internet on first boot** (to `apt install` and
`git clone`). No internet where it runs? Use the golden-image path instead — see
[`raspberry-pi.md` Path B](raspberry-pi.md#b-golden-image-clone-offline-fleets).

Full walkthrough: [`raspberry-pi.md` Path A](raspberry-pi.md#a-zero-touch-first-boot--recommended).
Manual (no-bash) equivalent: [`image/README.md`](image/README.md#the-manual-file-drop-equivalent).

---

## 1. Flash with Raspberry Pi Imager

- [ ] **OS:** Raspberry Pi OS (64-bit) **Lite** (Bookworm). 64-bit matters; Lite is enough (headless).
- [ ] Click **⚙ / Edit Settings** *before* writing and set:
  - [ ] Username + password
  - [ ] Wi-Fi SSID + password (or plan on Ethernet)
  - [ ] **Enable SSH**
  - [ ] Hostname, e.g. `speedkam-frontgate`
  - [ ] Locale / timezone (event timestamps use it)
- [ ] Write the card.

This is the only GUI step — the stager in step 3 chains onto this customisation
without clobbering it.

## 2. Find the `bootfs` drive letter

- [ ] After flashing, Windows re-mounts the small FAT **`bootfs`** partition with a
      drive letter (e.g. `E:`). Note which letter it got in File Explorer.

## 3. Stage the provisioner (Git Bash, from the repo root)

- [ ] Open **Git Bash** in the SpeedKam repo and run — replace `/e/` with your
      actual `bootfs` letter:

  ```bash
  bash deploy/image/prepare-boot.sh --boot /e/ --config-local ./config.local.yaml
  ```

  - No off-site backup? Drop `--config-local ./config.local.yaml`.
  - Pin a version instead of `main`: add `--ref v1.2.0`.
  - Point at a fork: add `--repo <url>`.

- [ ] Wait for it to print **Done**, then eject the card.

Your `config.local.yaml` is **shredded off the card** automatically after first
boot, so the secret doesn't linger on the FAT partition.

## 4. Boot the Pi

- [ ] Insert the card, apply power, **wait ~5 min** (first boot expands the
      filesystem, then `apt install` + `git clone` run).
- [ ] Browse to `http://<hostname>.local:8080`
      (e.g. `http://speedkam-frontgate.local:8080`), or the Pi's IP from your router
      if `.local` doesn't resolve.

Optional — watch it provision live (if you enabled SSH):

```bash
ssh <user>@<hostname>.local "journalctl -u speedkam-provision -f"
```

The provisioner disables and deletes itself once done, so later boots are normal.

## 5. Calibrate on-site (the only manual step)

The dashboard is up, but it can't report *speed* until it's calibrated to the road
it's looking at. Once the Pi is in its final mounted position:

- [ ] Dashboard → **Calibrate**, click the measured points, Save (it restarts), or
- [ ] `python3 tools/calibrate.py` on the node.

That's the whole per-node checklist.

---

## Re-imaging after a code change

Nothing to re-image. Either bump `--ref` for the next batch of cards, or on a live
node: `git pull && sudo systemctl restart speedkam`.
