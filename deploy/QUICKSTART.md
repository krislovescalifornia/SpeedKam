<!-- SPDX-FileCopyrightText: 2026 Kris Kling -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# SpeedKam Pi imaging — step-by-step (Windows)

This is the beginner-proof version: every click and every command spelled out.
Follow it top to bottom. It assumes you're on the same Windows PC where the
SpeedKam code lives (`C:\Users\Kris\dev\SpeedKam`).

**What you need in hand:**

- A microSD card (16 GB or bigger) and an SD card reader/slot on your PC.
- The Raspberry Pi, its power supply, and internet it can reach on first boot
  (Wi-Fi or an Ethernet cable). **The Pi must have internet the first time it
  powers on** — that's how it installs itself.

**Roughly what happens:** you write a fresh Raspberry Pi operating system to the
card, drop a few SpeedKam files onto it, and put it in the Pi. The first time the
Pi turns on it installs everything by itself and, about 5 minutes later, shows a
dashboard in your web browser. The only hands-on step after that is aiming/
calibrating the camera on-site.

---

## Step 1 — Install the two tools you need (one time only)

1. **Raspberry Pi Imager** — writes the OS to the card.
   Download from <https://www.raspberrypi.com/software/>, run the installer,
   click through with the defaults.

2. **Git Bash** — a small terminal you'll type one command into in Step 4.
   You almost certainly already have it (the SpeedKam code is a git repo). To
   check: click the Windows **Start** button and type `Git Bash`. If it shows up,
   you're set. If not, install "Git for Windows" from <https://git-scm.com/download/win>
   with the default options — Git Bash comes with it.

---

## Step 2 — Write the OS to the card with Raspberry Pi Imager

1. Put the microSD card into your PC's card reader.
2. Open **Raspberry Pi Imager**.
3. Click **CHOOSE DEVICE** → pick your Pi model (e.g. *Raspberry Pi 4*).
4. Click **CHOOSE OS** → **Raspberry Pi OS (other)** → **Raspberry Pi OS Lite (64-bit)**.
   - *Lite* (no desktop) and *64-bit* both matter. Don't pick the normal/Full one.
5. Click **CHOOSE STORAGE** → pick your microSD card.
   - ⚠️ Double-check the size matches your card — this **erases** whatever you pick.
6. Click **NEXT**. It asks *"Would you like to apply OS customisation settings?"* →
   click **EDIT SETTINGS**.
7. In the **General** tab, fill in:
   - ✅ **Set hostname:** `speedkam-frontgate`
     *(any name is fine; this is what you'll type in your browser later. If you
     make more than one, give each a different name.)*
   - ✅ **Set username and password:** pick a username (e.g. `pi`) and a password.
     **Write both down** — you'll need them if you ever log in.
   - ✅ **Configure wireless LAN:** your Wi-Fi network name (SSID) and password.
     *(Skip this only if you'll plug the Pi into Ethernet instead.)*
   - ✅ **Set locale settings:** your time zone (used for event timestamps).
8. Click the **Services** tab → ✅ **Enable SSH** → **Use password authentication**.
9. Click **SAVE**, then **YES** to apply the settings, then **YES** again to
   confirm erasing the card.
10. Wait for it to finish **Writing** and **Verifying**. When it says it's done and
    safe to remove, click **CONTINUE** and leave the card where it is for now.

---

## Step 3 — Re-insert the card so Windows can see it

When Imager finishes it "ejects" the card, so Windows temporarily can't see it.

1. **Physically pull the microSD card out** of the reader and **push it back in.**
2. Open **File Explorer** (the yellow folder icon on your taskbar) and click
   **This PC** on the left.
3. Under *Devices and drives* you'll see a small drive named **`bootfs`**. Note
   the **drive letter** next to it in parentheses — for example `bootfs (E:)`
   means the letter is **E**.

> 💡 You may also get a pop-up saying *"You need to format the disk"* for a second,
> larger drive. That's the Linux part of the card, which Windows can't read.
> **Click Cancel — do NOT format anything.** Only the `bootfs` drive matters here.

Remember your letter (we'll use **`E`** in the example below — substitute yours).

---

## Step 4 — Drop the SpeedKam files onto the card

1. Click the Windows **Start** button, type `Git Bash`, and open it. A black
   terminal window appears.
2. Click into that window and type this exact line, then press **Enter** — it
   moves the terminal into the SpeedKam folder:

   ```bash
   cd /c/Users/Kris/dev/SpeedKam
   ```

3. Now type this line and press **Enter**. **Replace the `e` in `/e/` with your
   own drive letter from Step 3** (lower-case; e.g. if yours was `F:` use `/f/`):

   ```bash
   bash deploy/image/prepare-boot.sh --boot /e/ --config-local ./config.local.yaml
   ```

4. It prints a few lines and finishes with a box that says **Done.** That's the
   signal it worked. (If it prints an error instead, copy the message and ask —
   the most common cause is the wrong drive letter.)

> **What that command just did:** copied the SpeedKam setup files and your backup
> settings onto the card, so the Pi can finish installing itself on first boot.
> Your secret backup settings get automatically wiped off the card during that
> first boot, so they don't linger.

5. Back in **File Explorer**, right-click the **`bootfs`** drive → **Eject**.
   When Windows says it's safe, pull the card out.

---

## Step 5 — Boot the Pi and open the dashboard

1. Put the microSD card into the Raspberry Pi.
2. Plug in the Pi's power. **Leave it alone for about 5 minutes** — on the very
   first boot it's downloading and installing SpeedKam. (Nothing shows on a
   screen; it's headless. Just wait.)
3. On your PC or phone (on the **same network**), open a web browser and go to:

   ```
   http://speedkam-frontgate.local:8080
   ```

   Use the hostname you set in Step 2. If the page doesn't load, wait another
   couple of minutes and refresh — first boot can be slow.

> **If `.local` doesn't work** (some Windows setups can't resolve it): open your
> Wi-Fi router's admin page, find the device named `speedkam-frontgate` in its
> device list, note its IP address (e.g. `192.168.1.42`), and browse to
> `http://192.168.1.42:8080` instead.

---

## Step 6 — Calibrate the camera on-site (the one hands-on step)

The dashboard works now, but it can't report a car's **speed** until you tell it
how the camera is aimed at the road — every camera sees a different scene, so this
can't be done ahead of time.

Once the Pi and camera are mounted in their final spot:

- In the dashboard, click **Calibrate**, click the measured points it asks for,
  and **Save**. It restarts itself and you're done.

That's the whole thing. From here the node counts, times, and records cars on its
own.

---

## Quick reference

| Thing | Value |
|---|---|
| Dashboard address | `http://<your-hostname>.local:8080` |
| The one command | `bash deploy/image/prepare-boot.sh --boot /X/ --config-local ./config.local.yaml` (X = your drive letter) |
| First boot wait | ~5 minutes, then refresh the browser |
| Needs internet on first boot? | **Yes** |

## Making another card later

Repeat Steps 2–5 with a new card. Give it a different **hostname** in Step 2 so
you can tell them apart. Nothing else changes.

## Full details / troubleshooting

- Full walkthrough with options: [`raspberry-pi.md` Path A](raspberry-pi.md#a-zero-touch-first-boot--recommended)
- Doing it without Git Bash (drag-and-drop files by hand): [`image/README.md`](image/README.md#the-manual-file-drop-equivalent)
- No internet where the Pi runs? Use the offline image instead: [`raspberry-pi.md` Path B](raspberry-pi.md#b-golden-image-clone-offline-fleets)
