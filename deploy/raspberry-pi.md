# SpeedKam on a Raspberry Pi — Deploy / Setup / Maintain

A single-page runbook for taking a Raspberry Pi from a bare board to a
calibrated, self-starting speed camera you reach from your phone at
`http://<pi-ip>:8080`.

> In a hurry? [`QUICKSTART.md`](QUICKSTART.md) is the zero-touch path as a bare
> checklist. This page is the full runbook behind it.

The work splits into two kinds of steps:

- **Deploy** (software) — OS, dependencies, the SpeedKam service. This is fully
  automatable, and the recommended path below gets it to **near zero-touch**.
- **Setup** (physical) — mounting the camera and **calibrating** it. This is the
  one part that *cannot* be imaged or scripted: every camera sees a different
  road, so it must be measured on-site. Once per install.

### Pick a deploy path

| Path | Touch per board | Use when |
|---|---|---|
| **[A. Zero-touch first boot](#a-zero-touch-first-boot--recommended)** ⭐ | Flash + drop 3 files + power on | **Default.** Any board with internet on first boot. |
| **[B. Golden image clone](#b-golden-image-clone-offline-fleets)** | Flash a prebuilt `.img` + power on | Offline / air-gapped fleets; many identical cards. |
| **[C. Manual runbook](#c-manual-runbook-board-1--debugging)** | ~15 SSH commands | Your first board, or when you're debugging the stack. |

All three end in the same place, then share the same **[Setup](#setup--camera--calibration)**
and **[Maintain](#3-maintain)** steps. If you're still buying hardware, read
**[Minimum system requirements](#minimum-system-requirements--how-cheap-can-you-go)**
first — you probably don't need a Pi 5.

---

## A. Zero-touch first boot ⭐ (recommended)

Flash a **stock** Raspberry Pi OS card, stage three small files on it, and power
on. On first boot the card installs its dependencies, pulls the project, drops in
your secrets, and comes up serving the dashboard on **:8080** — **no SSH, no typing.**

The division of labour: **Raspberry Pi Imager sets the identity** (user, Wi-Fi,
SSH, hostname — the thing it's good at), and **our provisioner installs the
software** (`deploy/image/firstrun.sh` + `provision-node.sh`, wired up by
`deploy/image/prepare-boot.sh`). Nothing is baked into a custom image, so a code
change is just "it clones the latest on next boot" — there's no `.img` to rebuild.

> **One requirement:** the board needs **internet on first boot** (to `apt
> install` and `git clone`). No internet where it runs? Use **[Path B](#b-golden-image-clone-offline-fleets)** instead.

### A.1 Flash stock Raspberry Pi OS

Use **Raspberry Pi Imager** on your laptop.

- **OS:** Raspberry Pi OS (64-bit) **Lite**, Bookworm. 64-bit matters (the YOLO
  recognition path is effectively 64-bit only; OpenCV is faster there). Lite is
  all you need — SpeedKam is headless and you calibrate from a browser.
- In Imager's **⚙ / Edit Settings** before writing, set the fleet identity:
  - **Username + password**, **Wi-Fi** SSID + password (or plan on Ethernet),
    **Enable SSH**, **hostname** (e.g. `speedkam-frontgate`), and **locale /
    timezone** (event timestamps use it).

That's the *only* GUI step. `prepare-boot.sh` chains onto this customisation, so
you keep all of it.

### A.2 Prepare your secrets overlay (once)

Off-site backup credentials must never live in git or in a shared image, so they
ride in an untracked overlay that the provisioner installs on the node and then
**shreds off the card**:

```bash
cp config.local.example.yaml config.local.yaml
# edit config.local.yaml -> backup.url + backup.secret
#   (and optionally web.auth for a dashboard password)
```

Reuse the same `config.local.yaml` for every card in a fleet — they all share the
same off-site receiver. (No off-site backup? Skip this; pass nothing in A.3 and
set `backup.enabled: false` later, or just leave it — the node still counts,
times, and records locally.)

### A.3 Stage the provisioner onto the card

Leave the just-flashed card in the reader (its small FAT **`bootfs`** volume
mounts automatically) and run `prepare-boot.sh` against it:

```bash
sudo bash deploy/image/prepare-boot.sh \
  --boot /media/$USER/bootfs \
  --config-local ./config.local.yaml
```

- **Windows:** run it from **Git Bash** or **WSL**; `--boot` is the `bootfs`
  drive, e.g. `--boot /e/` for `E:`.
- Pin a specific version with `--ref v1.2.0` (a tag or branch); default is `main`.
- Point at a fork with `--repo <url>`; pin the login user with `--user <name>`.

It copies `firstrun.sh` + `provision-node.sh` + your `config.local.yaml` onto the
card, writes a tiny `speedkam-provision.conf`, and wires the first-boot hook
*without* clobbering Imager's customisation. Eject when it says done.

> Prefer to stage by hand (no bash on your flashing machine)? The manual
> file-drop equivalent is in [`deploy/image/README.md`](image/README.md#zero-touch-online-first-boot).

### A.4 Boot — it provisions itself

Insert the card, apply power, and wait **~5 minutes** (first boot expands the
filesystem, then `apt install` + `git clone` run). Then browse to:

```
http://<hostname>.local:8080          # e.g. http://speedkam-frontgate.local:8080
```

If you enabled SSH you can watch it happen live:

```bash
journalctl -u speedkam-provision -f      # or: tail -f /var/log/speedkam-provision.log
```

The provisioner disables and deletes itself once done, so later boots are normal.

### A.5 Calibrate on-site — the only manual step

The dashboard is up, but it can't report *speed* until it's calibrated to the
road it's looking at. That's physical and per-install: continue to
**[Setup](#setup--camera--calibration)** below. Everything after this section is
shared by all three deploy paths.

---

## B. Golden image clone (offline fleets)

Build **one** fully-installed card, capture it to an `.img`, and clone that to as
many cards as you want. Each clone boots straight into the dashboard with **no
internet needed on first boot** — the price is a build host, a multi-GB image,
and a re-capture whenever the code changes.

This is the right call for **air-gapped** installs or bulk-cloning identical
cards. Full recipe (build master → capture + shrink → clone → per-clone identity
reset): [`deploy/image/README.md`](image/README.md).

Then calibrate each node on-site: **[Setup](#setup--camera--calibration)**.

---

## C. Manual runbook (board #1 / debugging)

The longhand path. Slower, but it's what the automated paths do under the hood —
reach for it on your first board or when something's wrong.

### C.1 Flash + first boot

Flash as in [A.1](#a1-flash-stock-raspberry-pi-os), boot, then:

```bash
ssh <user>@<pi-ip>
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

### C.2 Install dependencies (system packages — the recommended path)

SpeedKam's installer defaults to the **system** Python precisely so these `apt`
packages are used. Don't build a venv unless you have a reason to.

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
> recognition venv in [Turn on recognition](#optional-turn-on-vehicle-recognition)
> — a leftover or half-built `.venv` will hijack the service's interpreter and
> can break it. If you're not enabling recognition, don't create one.

- `python3-picamera2` is only needed for the **CSI ribbon camera**. Harmless with
  a USB webcam — the `auto` backend just falls back to USB.
- `picamera2` is **not** reliably pip-installable; always use the `apt` package.

> **Do not copy the Windows `.venv` to the Pi.** It contains Windows binaries.
> Either `git clone` fresh or copy the folder *excluding* `.venv`.

### C.3 Get the project + configure

```bash
cd ~
git clone https://github.com/krislovescalifornia/SpeedKam.git SpeedKam
cd SpeedKam
```

Only a few `config.yaml` keys change from a Windows test rig:

| Key | Value | Why |
|---|---|---|
| `camera.backend` | `auto` | Picks CSI camera if present, else USB. Leave as-is. |
| `camera.source` | `0` | Only used for USB/OpenCV fallback (webcam index). |
| `camera.width` / `height` / `fps` | see requirements table | Lower = less CPU. Start `1280×720 @ 30` on a Pi 5/4-4GB; drop to `640×480 @ 15` on smaller boards. For max plate detail, the IMX296 global-shutter cam's native mode is `1456×1088` — on a Pi 3 that's ~23 fps with `detect_scale 0.3` (see [Pi 3 tuning](../docs/pi3-performance-tuning.md#native-resolution-capture-2026-08-22)). |
| `display.show_window` | `false` | **Required** for headless. No monitor attached. |
| `recognition.enabled` | `false` **at first** | YOLO is the heavy part. Get speed working first (see [recognition](#optional-turn-on-vehicle-recognition)). |
| `speed.speed_limit_kmh` | your limit | Used only to flag/annotate speeders. |
| `speed.display_units` | `mph` or `kmh` | |

Put **secrets** (backup url/secret, dashboard auth) in an untracked overlay so
they never reach git:

```bash
cp config.local.example.yaml config.local.yaml
# edit config.local.yaml -> backup.url + backup.secret
```

`config.local.yaml` is deep-merged over `config.yaml` at load time and is
gitignored, so `config.yaml` stays a shareable, secret-free template.

### C.4 Install the autostart service

```bash
sudo bash deploy/install-service.sh
```

This figures out your user, project path, and Python; adds you to the `video`
group for camera access; renders `deploy/speedkam.service`; and enables it. From
now on SpeedKam **starts on boot**, serves the dashboard on **:8080**, and
**restarts itself** if it crashes.

> **First-time gotcha:** if the installer *just* added you to the `video` group,
> `sudo reboot` once so the running service inherits camera permission.

```bash
sudo systemctl status speedkam
journalctl -u speedkam -f          # live logs / measured speeds
```

Then calibrate: **[Setup](#setup--camera--calibration)**.

---

## Setup — camera + calibration

Shared by every deploy path. This is the physical, per-install work that can't be
automated.

### Camera hardware

- **USB webcam:** just plug it in. Nothing else to do.
- **CSI camera (ribbon):** power off, connect the ribbon (contacts toward the
  correct side), power on. On Bookworm the camera stack auto-detects — verify:

  ```bash
  rpicam-hello --list-cameras
  ```

  You should see your sensor (e.g. `imx296` for the Global Shutter camera). If
  it's not listed, reseat the ribbon and check `/boot/firmware/config.txt`.

### Sanity-check the pipeline (optional but reassuring)

Proves the speed math works before you touch a real road, using a synthetic
drive-by — no camera needed:

```bash
python3 tools/make_test_video.py
python3 run.py --config config.test.yaml --source test_road.mp4 --no-display
#   -> should report ~30 mph for the synthetic car
```

### Mount the camera — then calibrate (order matters)

**Mount first, rigidly, in the final position.** Any wobble after calibration
invalidates the speed math. Aim it at a flat stretch of road where you can both
*see* features and *tape-measure* between them. Keep the timed zone in the nearer
part of the frame (the far field has poor metric resolution).

### Calibrate from your phone (headless — recommended)

The desktop `tools/calibrate.py` opens an OpenCV window that needs a monitor, so
on a headless Pi use the **dashboard's browser calibration** instead:

1. Open the dashboard at `http://<pi-ip>:8080` → **Calibrate**. (On the manual
   path before the service is installed, start it by hand with `python3 serve.py`
   and stop it with `Ctrl+C` when done.)
2. Physically tape-measure **4+ points** on the road you can also see in the
   image (lane cracks, chalk crosses, cones, driveway corners). Define an origin
   `(0,0)` and axes in meters — e.g. **X = along the road, Y = across it**. A
   `20 m × 4 m` rectangle is a good default: `(0,0) (20,0) (20,4) (0,4)`.
3. Click each point on the snapshot **in the same order** and type its measured
   `X Y` in meters. Save.
4. It writes `calibration.json` and reports a **reprojection error**. Aim for
   **under ~0.3 m**; if it's large, re-measure and redo. (Prefer the desktop
   tool? On a Desktop-OS Pi or over VNC: `python3 tools/calibrate.py`.)
5. `sudo systemctl restart speedkam`, then watch a few real cars on the live view
   and confirm the reported speeds look sane.

### (Optional) Turn on vehicle recognition

Recognition is the one compute-hungry feature. Two very different costs:

- **Colour** — pure OpenCV, cheap, runs on any Pi. Free with recognition on.
- **Type / make / model** — needs YOLO via `pip install ultralytics` (pulls in
  PyTorch, ~hundreds of MB, wants **≥4 GB RAM**). It runs once per pass at
  finalize time (not every frame), so it's viable on a Pi 4-4GB / Pi 5, but leave
  it **off** on 2 GB boards and the Zero 2 W.

Enable it only after speed works. Set `recognition.enabled: true`; **Colour**
needs nothing more. For **type / make / model** you need `ultralytics` in a venv
that can also see the apt-installed `opencv`/`picamera2` — hence
`--system-site-packages`:

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install ultralytics
```

> **The service must run *this* venv's Python, or recognition silently does
> nothing.** The installer picks `./.venv/bin/python` when a `.venv` exists, else
> system `python3`. If you create the venv **after** installing the service, it's
> still on system `python3` (no `ultralytics`) and type/make/model just stay
> blank — with no error. Re-point it and restart:
>
> ```bash
> sudo bash deploy/install-service.sh   # re-renders the unit to use .venv
> sudo systemctl restart speedkam
> ```

> The type model (`yolov8n.pt`) **auto-downloads on first use**, so the first
> recognized vehicle needs internet — or pre-place the weights file in the
> project folder.

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

If the camera is ever bumped or remounted, [recalibrate](#calibrate-from-your-phone-headless--recommended)
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
  `synced`. Setup: [`deploy/webhost/README.md`](webhost/README.md).

### Health checks

- Dashboard **Traffic summary** card — Today / week / month counts survive even
  after clips rotate away (computed from the CSV).
- Receiver health page + **backup pill** — off-site sync status.
- `df -h` — SD card free space, if you're not using rotation.

### Deploying more than one camera

Use **[Path A](#a-zero-touch-first-boot--recommended)** — flash stock cards and
stage each with `prepare-boot.sh`, reusing one `config.local.yaml` across the
fleet. Each node self-provisions and needs only its own one-time on-site
calibration. For **offline** fleets, build one golden image and clone it
(**[Path B](#b-golden-image-clone-offline-fleets)** / [`deploy/image/README.md`](image/README.md)).

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

- **Power supply** — the official one for your board. Undervolting causes random
  camera/USB failures that look like software bugs.
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
