# SpeedKam on a Raspberry Pi — Deploy / Setup / Maintain

A single-page runbook for taking one Raspberry Pi from a bare board to a
calibrated, self-starting speed camera you reach from your phone at
`http://<pi-ip>:8080`.

Three phases:

1. **[Deploy](#1-deploy--hardware--os)** — hardware, OS, dependencies (once per board).
2. **[Setup](#2-setup--project-camera-calibration)** — project, camera, calibration (once per install site).
3. **[Maintain](#3-maintain)** — day-to-day operation, updates, storage.

If you're buying hardware, read **[Minimum system requirements](#minimum-system-requirements--how-cheap-can-you-go)** at the bottom *first* — you probably don't need a Pi 5.

---

## 1. Deploy — hardware + OS

### 1.1 Flash the OS

Use **Raspberry Pi Imager** on your laptop.

- **OS:** Raspberry Pi OS (64-bit), Bookworm. The 64-bit build matters — the
  recognition path (PyTorch/YOLO) is effectively 64-bit only, and OpenCV is
  faster there.
- **Lite vs Desktop:** **Lite** is all you need. SpeedKam runs headless and you
  calibrate from a browser, so there's no reason to pay the RAM/CPU cost of a
  desktop. (If you specifically want to run the `tools/calibrate.py` desktop
  window on the Pi itself, pick Desktop — but the browser calibration below is
  the better path.)
- In Imager's **⚙ / Edit Settings** before writing, set:
  - **Hostname** (e.g. `speedkam-frontgate`)
  - **Enable SSH** (password or key)
  - **Wi-Fi** SSID + password (or plan to use Ethernet)
  - **Locale / timezone** — set this correctly; event timestamps use it.

### 1.2 First boot + update

```bash
ssh <user>@<pi-ip>
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

### 1.3 Install dependencies (system packages — the recommended path)

SpeedKam's own installer defaults to the **system** Python precisely so these
`apt` packages are used. Don't build a venv unless you have a reason to.

```bash
sudo apt install -y \
  python3-opencv \
  python3-picamera2 \
  python3-yaml \
  python3-flask \
  python3-requests \
  git
```

> **A `.venv` folder silently changes which Python the service runs.** The
> installer auto-prefers `./.venv/bin/python` if it exists, else system
> `python3`. The **only** supported venv is the `--system-site-packages`
> recognition venv in [2.6](#26-optional-turn-on-vehicle-recognition) — a
> leftover or half-built `.venv` will hijack the service's interpreter and can
> break it. If you're not enabling recognition, don't create one.

- `python3-picamera2` is only needed for the **CSI ribbon camera** (Global
  Shutter / Camera Module). Harmless to install even if you use a USB webcam —
  the `auto` backend just won't find a CSI camera and falls back to USB.
- `picamera2` is **not** reliably pip-installable; always use the `apt` package.

> **Do not copy the Windows `.venv` to the Pi.** It contains Windows binaries.
> Either `git clone` fresh (next section) or copy the folder *excluding* `.venv`.

### 1.4 Camera hardware

- **USB webcam:** just plug it in. Nothing else to do.
- **CSI camera (ribbon):** power off, connect the ribbon (contacts toward the
  correct side), power on. On Bookworm the camera stack auto-detects — verify:

  ```bash
  rpicam-hello --list-cameras
  ```

  You should see your sensor (e.g. `imx296` for the Global Shutter camera). If
  it's not listed, reseat the ribbon and check `/boot/firmware/config.txt`.

---

## 2. Setup — project, camera, calibration

### 2.1 Get the project onto the Pi

```bash
cd ~
git clone <your-repo-url> SpeedKam    # or: rsync the folder, minus .venv
cd SpeedKam
```

### 2.2 Edit `config.yaml` for the Pi

Only a few keys change from the Windows test rig. Open `config.yaml` and set:

| Key | Value | Why |
|---|---|---|
| `camera.backend` | `auto` | Picks CSI camera if present, else USB. Leave as-is. |
| `camera.source` | `0` | Only used for USB/OpenCV fallback (webcam index). |
| `camera.width` / `height` / `fps` | see requirements table | Lower = less CPU. Start at `1280×720 @ 30` on a Pi 5/4-4GB; drop to `640×480 @ 15` on smaller boards. |
| `display.show_window` | `false` | **Required** for headless. No monitor attached. |
| `recognition.enabled` | `false` **at first** | YOLO is the heavy part. Get speed working first, then decide (see [2.6](#26-optional-turn-on-vehicle-recognition)). |
| `speed.speed_limit_kmh` | your limit | Used only to flag/annotate speeders. |
| `speed.display_units` | `mph` or `kmh` | |
| `backup.*` | your domain + secret | Set these in `config.local.yaml`, not here (see below). Only if you set up off-site backup (`deploy/webhost/README.md`). |

> The shipped `config.yaml` has `recognition.enabled: true` and **placeholder**
> backup credentials. Turn recognition **off** for the first bring-up. Put your
> **real** backup url + secret in an untracked overlay so they never reach git:
>
> ```bash
> cp config.local.example.yaml config.local.yaml
> # edit config.local.yaml -> backup.url + backup.secret
> ```
>
> `config.local.yaml` is deep-merged over `config.yaml` at load time and is
> gitignored, so `config.yaml` stays a shareable, secret-free template. On a
> golden-master Pi, fill it in before imaging — it rides along on every clone.

### 2.3 Sanity-check the pipeline (optional but reassuring)

Proves the speed math works before you touch a real road, using a synthetic
drive-by — no camera needed:

```bash
python3 tools/make_test_video.py
python3 run.py --config config.test.yaml --source test_road.mp4 --no-display
#   -> should report ~30 mph for the synthetic car
```

### 2.4 Mount the camera — then calibrate (order matters)

**Mount first, rigidly, in the final position.** Any wobble after calibration
invalidates the speed math. Aim it at a flat stretch of road where you can both
*see* features and *tape-measure* between them. Keep the timed zone in the
nearer part of the frame (the far field has poor metric resolution).

### 2.5 Calibrate from your phone (headless — recommended)

The desktop `tools/calibrate.py` opens an OpenCV window that needs a monitor, so
on a headless Pi use the **dashboard's browser calibration** instead:

1. Start the dashboard (temporarily, by hand — before installing the service):

   ```bash
   python3 serve.py
   ```

   It prints a URL like `http://192.168.1.42:8080`.

2. On your phone/laptop (same network), open that URL → **Calibrate**.
3. Physically tape-measure **4+ points** on the road you can also see in the
   image (lane cracks, chalk crosses, cones, driveway corners). Define an origin
   `(0,0)` and axes in meters — e.g. **X = along the road, Y = across it**. A
   `20 m × 4 m` rectangle is a good default: `(0,0) (20,0) (20,4) (0,4)`.
4. On the calibrate page, click each point on the snapshot **in the same order**
   and type its measured `X Y` in meters. Save.
5. It writes `calibration.json` and reports a **reprojection error**. Aim for
   **under ~0.3 m**; if it's large, re-measure and redo. (Prefer the desktop
   tool? On a Desktop-OS Pi or over VNC: `python3 tools/calibrate.py`.)

Then watch a few real cars go by on the live view and confirm the reported
speeds look sane. Stop the hand-run server with `Ctrl+C` once you're happy.

### 2.6 (Optional) Turn on vehicle recognition

Recognition is the one compute-hungry feature. Two very different costs:

- **Colour** — pure OpenCV, cheap, runs on any Pi. Free with recognition on.
- **Type / make / model** — needs YOLO via `pip install ultralytics` (pulls in
  PyTorch, ~hundreds of MB, wants **≥4 GB RAM** to be comfortable). Heavy on a
  Pi. It runs once per pass at finalize time (not every frame), so it's viable
  on a Pi 4-4GB / Pi 5, but leave it **off** on 2 GB boards and the Zero 2 W.

Enable it only after speed works, then watch `journalctl` for the pipeline
keeping up. Set `recognition.enabled: true`. **Colour** needs nothing more.

For **type / make / model** you need `ultralytics`, which must live in a venv
that can also see the apt-installed `opencv`/`picamera2` — hence
`--system-site-packages` (those packages are not cleanly pip-installable, per
[1.3](#13-install-dependencies-system-packages--the-recommended-path)):

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install ultralytics
```

> **The service must run *this* venv's Python, or recognition silently does
> nothing.** The installer picks `./.venv/bin/python` when a `.venv` exists,
> else system `python3`. If you create the venv **after** already installing the
> service ([2.7](#27-install-the-autostart-service)), the service is still on
> system `python3` (no `ultralytics`) and type/make/model just stay blank — with
> no error, because the installer's dependency check doesn't test for
> `ultralytics`. Re-point it and restart:
>
> ```bash
> sudo bash deploy/install-service.sh   # re-renders the unit to use .venv
> sudo systemctl restart speedkam
> ```

> The type model (`yolov8n.pt`) **auto-downloads on first use**, so the first
> recognized vehicle needs internet — or pre-place the weights file in the
> project folder.

### 2.7 Install the autostart service

From inside the project folder:

```bash
sudo bash deploy/install-service.sh
```

This figures out your user, project path, and Python; adds you to the `video`
group for camera access; renders `deploy/speedkam.service`; and enables it. From
now on SpeedKam **starts on boot**, serves the dashboard on **port 8080**, and
**restarts itself** if it crashes or the camera hiccups.

> **First-time gotcha:** if the installer *just* added you to the `video` group,
> `sudo reboot` once so the running service inherits camera permission.

Confirm:

```bash
sudo systemctl status speedkam
journalctl -u speedkam -f          # live logs / measured speeds
```

Open `http://<pi-ip>:8080` from your phone. Done.

---

## 3. Maintain

### Everyday commands

```bash
sudo systemctl status speedkam     # is it running?
journalctl -u speedkam -f          # live logs + measured speeds
sudo systemctl restart speedkam    # after editing config.yaml or recalibrating
sudo systemctl stop speedkam       # pause
sudo bash deploy/uninstall-service.sh   # remove entirely
```

**Any change to `config.yaml` or `calibration.json` requires a restart** — the
service reads them at startup.

### Update the code

```bash
cd ~/SpeedKam
git pull
sudo systemctl restart speedkam
```

### Recalibrate

If the camera is ever bumped or remounted, recalibrate ([2.5](#25-calibrate-from-your-phone-headless--recommended))
and `sudo systemctl restart speedkam`. Speed is only ever as good as the current
calibration + a rigid mount.

### Storage (keep the SD card from filling up)

- **SpeedKapture** (`recording.speedkapture_threshold`, also live on the
  dashboard): only *save a clip* above this speed. Slower vehicles are still
  **counted, timed, and logged** — just no video. Set `0` to capture everything.
- **Local rotation** (`retention.enabled: true`, `retention.local_days`): delete
  local clips older than N days. With `require_backup: true` a file is deleted
  only after off-site backup confirms it uploaded, so you never drop your only
  copy. The CSV event log (your counts/history) is kept **forever**.
- **Off-site backup** (`backup.*`): mirror every event to a domain you own so
  records survive theft/damage. Watch the dashboard's **backup pill** go to
  `synced`. Setup: `deploy/webhost/README.md`.

### Health checks

- Dashboard **Traffic summary** card — Today / week / month counts survive even
  after clips rotate away (computed from the CSV).
- Receiver health page + **backup pill** — off-site sync status.
- `df -h` — SD card free space, if you're not using rotation.

### Deploying more than one camera

Don't hand-provision each board. Build **one** golden SD image and clone it:
`sudo bash deploy/image/provision.sh`, then capture + flash. Each clone gets a
unique hostname/SSH identity on first boot and needs only its own one-time
on-site calibration. Full recipe: `deploy/image/README.md`.

---

## Minimum system requirements — how cheap can you go?

**Short answer: you don't need a Pi 5.** The core pipeline is classical computer
vision (MOG2 background subtraction + a homography + a lightweight tracker), not
a neural net. The *only* thing that demands a Pi 5 / lots of RAM is the **YOLO
make/model recognition**, which is optional. Match the board to whether you want
that.

### What actually costs compute

| Workload | Cost | Notes |
|---|---|---|
| Motion detection + tracking + speed | Low–moderate | Scales with **resolution × fps**. This is the whole job for a speed camera. |
| Clip encoding + dashboard | Low | Only when saving a clip / when a browser is watching. |
| Colour recognition | Very low | Pure OpenCV, no model. Fine on any Pi. |
| **YOLO type/make/model** | **High** | PyTorch. Wants **≥4 GB RAM**; this is the only Pi-5-justifying feature. |

The two biggest knobs are `camera.width/height` and `camera.fps`. Dropping
`1280×720 → 640×480` is ~4× less pixel work; `30 → 15 fps` halves it again. For a
private-road speed camera, `640–800 px` wide at `15 fps` is plenty — you just
need enough samples per pass (`speed.min_samples: 6`).

### Recommended tiers

| Tier | Board (approx. price*) | Settings | Good for |
|---|---|---|---|
| **Cheapest that works** | **Pi Zero 2 W (~$15) + USB webcam** | `640×480 @ 15`, recognition **off** | Speed + count + colour on a low-traffic private road. 512 MB RAM rules out YOLO. Budget-champion. |
| **Sweet spot (recommended)** | **Pi 4, 2–4 GB (~$45–60)** | `1280×720 @ 30`, recognition off (4 GB: YOLO usable) | Comfortable real-time speed at full res. The 4 GB can also run occasional YOLO. Best value. |
| **Only if you want smooth YOLO** | **Pi 5, 4–8 GB (~$60–80)** | `1280×720 @ 30`, recognition on | Fast enough that make/model recognition doesn't stall the pipeline; higher res/fps headroom. |

\* *Board only; prices are ballpark and vary. Add the extras below.*

**So: is a Pi 5 wasted?** For a plain speed camera, **yes** — a Pi 4 (or even a
Zero 2 W at reduced resolution) does the job and leaves the CPU idle much of the
time. Spend the Pi-5 premium only if you specifically want responsive YOLO
make/model recognition or lots of high-res clips.

### Non-negotiable extras (budget for these too)

- **Power supply** — the official one for your board. Undervolting causes
  random camera/USB failures that look like software bugs.
- **microSD** — 64 GB+ **A2**-rated (or boot from a **USB SSD** if you'll keep
  many clips). Rotation + SpeedKapture keep space bounded, but faster = happier.
- **Camera** — see the trade-off below.
- **Weatherproof enclosure** with a clear window + sun shade, and a **rigid**
  mount, for any outdoor install.

### Camera choice (matters more than the board)

For a *speed* camera the single most important feature is a **global shutter**,
which exposes the whole frame at once so a fast car isn't skewed/smeared like it
is on the rolling-shutter sensors in webcams and phones.

- **Best:** Raspberry Pi **Global Shutter Camera** (Sony IMX296) + a 6 mm
  (wider) or 8 mm (tighter/farther) CS-mount lens, with a short manual exposure
  to freeze motion.
- **Budget:** Pi **Camera Module 3** (autofocus, rolling shutter) — fine for the
  lower speeds of a private road.
- **Testing / cheapest:** any **USB webcam** — works with zero code changes via
  the `opencv` backend.

A cheap board with a global-shutter camera beats an expensive board with a
webcam for accuracy. Spend here before you spend on the Pi.
