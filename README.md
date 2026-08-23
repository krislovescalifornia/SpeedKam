# SpeedKam

[![CI](https://github.com/krislovescalifornia/SpeedKam/actions/workflows/ci.yml/badge.svg)](https://github.com/krislovescalifornia/SpeedKam/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)

A camera-based vehicle **speed camera** for a fixed location on a private road.
It watches a road, detects passing vehicles, estimates their speed from
real-world ground markers, and saves an annotated video clip + snapshot + a CSV
log for every pass.

It runs **now on Windows with a USB webcam** for testing, and moves **unchanged
to a Raspberry Pi** later — you only edit `config.yaml`.

### Documentation

- **This README** — quick start, dashboard, calibration, configuration, features.
- [Raspberry Pi deployment](deploy/raspberry-pi.md) — the full on-Pi runbook.
- [Off-site host setup](deploy/webhost/README.md) — the backup receiver + dashboard.
- [Fleet imaging](deploy/image/README.md) — clone one SD card to many nodes.
- [Deferred recognition](docs/deferred-recognition.md) — offload YOLO to another box.
- [Privacy & legal](docs/PRIVACY.md) — what's stored and how to deploy responsibly.
- [Pi tuning & accuracy journey](docs/pi3-performance-tuning.md) — fps/thermal tuning, the
  low-light gate, and the road-region false-positive gate, with what was measured at each step.
- [Pi 4 / Phase 14 plan](docs/pi4-yolo-classifier-phase14.md) — kickoff brief for on-node
  car-vs-person classification (needs a 64-bit Pi 4).

---

## How it estimates speed

A fixed camera looking at a flat road is a solved geometry problem. You measure
a few real points on the road once (with a tape measure) and click them in the
image. SpeedKam computes a **homography** that maps image pixels → real-world
meters on the road surface. Then for each vehicle it:

1. **Detects** motion (background subtraction) → bounding box.
2. Takes the box's **bottom-centre** as the tire/ground contact point.
3. Maps that point to **meters** via the homography, every frame.
4. **Tracks** the vehicle across frames using **real capture timestamps**.
5. Fits distance-vs-time to get **speed = metres / second**, converted to mph/kmh.
   A robust regression + a median cross-check reject per-frame noise.

Accuracy is bounded by your calibration: measure carefully, spread the markers
along the whole measurement zone, and keep the camera rigidly mounted.

---

## Quick start (Windows test rig)

```bash
# 1. Create the environment (already done once; repeat on a new machine)
python -m venv .venv
.venv\Scripts\python -m pip install -e .        # installs the package + deps
#   reproducible (exact tested versions):  pip install -e . -c constraints.txt
#   add recognition (YOLO, AGPL, heavy):   pip install -e ".[recognition]"
#   plain deps without packaging instead:  pip install -r requirements.txt

# 2. Find your webcam index and put it in config.yaml (camera.source)
.venv\Scripts\python tools\camera_check.py

# 3. Prove the whole chain works with a synthetic drive-by (no camera needed)
.venv\Scripts\python tools\make_test_video.py
.venv\Scripts\python run.py --config config.test.yaml --source test_road.mp4 --no-display
#   -> should report ~30 mph for the synthetic car

# 4. Calibrate against YOUR road (see below), then run live
.venv\Scripts\python tools\calibrate.py
.venv\Scripts\python run.py
```

Press `q` in the preview window (or `Ctrl+C`) to stop. Captures land in
`captures/` with a running log in `captures/events.csv`.

> **Installed vs. uninstalled.** After `pip install -e .` you also get two
> commands — `speedkam` (desktop/headless) and `speedkam-serve` (web dashboard) —
> equivalent to `python run.py` / `python serve.py`. The `run.py`/`serve.py`
> launchers keep working **without** any install, which is how the Raspberry Pi
> runs from apt-provided packages (no pip).

---

## Web dashboard (recommended)

Instead of the desktop preview window, run the dashboard — a browser UI with a
**live view**, the **most recent clips** (click to play), **stats**, and a
**click-to-calibrate** page. This is the best way to use the Pi: no monitor
needed, just browse to it from your phone or laptop.

```bash
.venv\Scripts\python serve.py
```

It prints a URL like `http://192.168.1.42:8080`. Open it on any device on the
same network. `run.py` (the desktop window) still works and is handy for quick
local checks; `serve.py` is what the boot service runs on the Pi.

> **Password-protect the dashboard (optional).** By default the dashboard is
> open — fine on a trusted home LAN. To require a login for everything (pages,
> APIs, live stream, and captured media), set a `web.auth.password` in
> `config.local.yaml` (it's a secret, so it stays out of git):
>
> ```yaml
> web:
>   auth:
>     username: admin
>     password: "a-long-password"
> ```
>
> The camera then uses HTTP Basic Auth; the startup banner shows whether the
> dashboard is `password-protected` or `OPEN`. Recommended if the camera shares a
> network with devices or people you don't fully trust. Use HTTPS (e.g. behind a
> reverse proxy) if you expose it beyond the LAN.

The dashboard has:
- **Live view** — the annotated camera stream (boxes, speeds, calibration zone).
- **Latest reading** and **stats** — last speed, vehicle count, over-limit count.
- **Recent clips** — thumbnails of recent captures; click to play the clip.
- **Date filter** — narrow the clips and Top-10 to a From/To day range.
- **Top 10 speeders** — the fastest speeds ever recorded (or within the filter),
  click any to play its clip.
- **CSV export** — download the filtered event log, or the Top-10, as CSV
  (buttons on each section; exports respect the date filter).
- **Backup pill** — shows off-site backup health (`synced` / `N queued`).
- **Calibrate** button — opens a page where you click points on a snapshot and
  type their measured meters, then Save. No monitor or mouse-on-the-Pi needed.

---

## Off-site backup (survive theft/damage)

Everything is stored locally in `captures/` **and** can be mirrored to a web
domain you own, so records survive if the camera is stolen or damaged. Each
event (CSV row + snapshot + video clip) is uploaded over HTTPS to a small PHP
receiver on your site, authenticated with a shared secret.

It's reliable by design: events are written to an on-disk queue first, then a
background worker uploads them. If the Pi is briefly offline, jobs stay queued
and retry automatically — nothing is lost. The server dedupes by event id, so
retries never create duplicates.

**Setup** (details in [`deploy/webhost/README.md`](deploy/webhost/README.md)):
1. Upload the PHP files in [`deploy/webhost/`](deploy/webhost/) to your domain.
   Copy `speedkam_config.example.php` to `speedkam_config.php` (gitignored) and
   set a long random `$SECRET` and a `$DASHBOARD_PASSWORD`.
2. Copy `config.local.example.yaml` to `config.local.yaml` (gitignored) and set
   the `url` and the same `secret` there; in `config.yaml` under `backup:` just
   set `enabled: true`.
3. Watch the dashboard's **backup** pill go to `synced`. To push records that
   already exist locally (first-time backup / after downtime):

```bash
python tools/backfill_sync.py
```

> Keep `backup.secret` private and use an `https://` URL. It lives in the
> untracked `config.local.yaml` overlay (not `config.yaml`), so it never reaches
> git — the tracked `config.yaml` only ever holds a placeholder.

For a **complete** off-site archive, also set `backup.mirror_all: true` — see
[Making the off-site copy a full historical archive](#making-the-off-site-copy-a-full-historical-archive).

---

## Off-site dashboard & remote control

The web host isn't just storage — [`speedkam_dashboard.php`](deploy/webhost/speedkam_dashboard.php)
is a **password-protected web UI on your own domain** that reads the mirrored
data, so you can see everything from anywhere and it keeps working even if the
camera is taken. It shows an online/offline pill with the camera's last check-in,
today/week/month counts (with over-limit tallies), and a gallery of passes with
snapshot thumbnails and speeder clips — media served through an authenticated
proxy, never public.

It also does **basic remote control**. Your camera lives behind your home router,
so the host can't reach *in* to it. Instead the camera **checks in** every
`control.poll_seconds` (`control.enabled: true`), reporting status and pulling any
settings you changed on the dashboard. So when you adjust the SpeedKapture
threshold on the website, the camera picks it up on its next check-in a few
seconds later — no port-forwarding, nothing about your home network exposed.
Remote and on-LAN edits don't fight: changes carry a revision number and are only
re-applied when it advances (and that survives a camera reboot).

This is the recommended shape for a camera in the yard: the **Pi does all the
work** (capture, speed, recognition) and mirrors finished results home; the
off-site host **stores, displays, and relays your control** — plain PHP, no
Python or special server needed.

---

## SpeedKapture — record only what matters

By default SpeedKam saves a clip for every vehicle it can measure. Set a
**SpeedKapture** threshold and it will only **save and off-site-post a clip when
the speed is above that number** (in your `display_units`). A SpeedKapture of
`35` captures a car doing 35.9 mph but ignores one doing 34.

Crucially, slower vehicles are **still counted** — you keep the daily/weekly/
monthly totals, direction, speed, and any recognized attributes — there's just
no video clip eating disk space for a car that wasn't speeding. Set it to `0` to
capture everything.

Change it live from the **SpeedKapture** card on the dashboard (it persists
across restarts), or set `recording.speedkapture_threshold` in `config.yaml`.

The dashboard's **Traffic summary** card shows Today / This week / This month
counts with a direction breakdown, computed from the CSV log — so the numbers
survive even after old clips are rotated away.

---

## Storage rotation (SD card + off-site)

Two independent knobs keep storage from filling up — one for the Pi, one for
your web host:

- **Local** (`retention.local_days`): delete local clips/snapshots older than N
  days. With `retention.require_backup: true` (the default) a file is only
  deleted **once off-site backup has confirmed it uploaded**, so you never drop
  the only copy. Turn the whole thing on with `retention.enabled: true`. If you
  run without backup, set `require_backup: false` to allow plain age-based
  cleanup.
- **Remote** (`backup.remote_retention_days`): the camera periodically asks the
  web receiver to delete off-site media older than N days. `0` = keep forever.

Only **media** is ever deleted — the CSV event log (and therefore your vehicle
counts and history) is kept indefinitely, locally and off-site.

### Making the off-site copy a full historical archive

The two rotation knobs are independent on purpose: set a short `local_days` and
leave `remote_retention_days: 0`, and the Pi trims old media off its SD card
while the web host keeps everything forever. For that to be a *complete* archive,
turn on **`backup.mirror_all: true`** — then every counted pass is mirrored
off-site (its CSV row + snapshot), not just captured (above-SpeedKapture) clips.
Below-threshold passes have no clip to mirror, but their row and snapshot go up,
so nothing is missing when local media rotates away. (A file is still only
trimmed locally once backup has confirmed it uploaded, so the mirror is never
racing ahead of the delete.)

---

## Vehicle recognition (type / make / model / year / colour)

Optional and best-effort. With `recognition.enabled: true` SpeedKam annotates
each pass with what it can determine and logs it to the CSV + dashboard:

- **Colour** is estimated from the vehicle crop with a cheap OpenCV analysis —
  no model needed, so it works even on a bare Pi.
- **Type** (car/truck/bus/motorcycle) comes from a YOLO model (`recognition.model`,
  auto-downloaded) when `ultralytics` is installed — `pip install -e ".[recognition]"`
  (or `pip install ultralytics`). Note this pulls in **AGPL-3.0** code; see
  [License](#license).
- **Make / model / year** only appear if you point
  `recognition.make_model_weights` at a fine-grained classifier — otherwise they
  stay blank. That's the "when available" contract: missing attributes never
  block the count or the speed.

Recognition is heavy on a Pi; leave it off until you've tested throughput. It
runs on a buffered frame at finalize time, so it works for counted-but-not-
captured (sub-SpeedKapture) passes too.

### Offloading recognition (defer the YOLO to another machine)

On a **busy** road where per-pass inference can't keep up, you can let the Pi do
only capture + speed + colour in real time and run the heavy YOLO later on a
beefier box (`recognition.defer: true` + `tools/recognize_worker.py`). On a
low-traffic drive you don't need this — inline recognition is plenty.

**See [docs/deferred-recognition.md](docs/deferred-recognition.md)** for the full
setup, the worker's flags, and how off-site enrichment stays idempotent.

---

## Calibrating your road

This is the one step that makes speed accurate. Do it once, with the camera in
its final mounted position.

1. Pick **4 or more** points you can both **see in the image** and
   **tape-measure between** on the road surface: lane markings, cracks, chalk
   crosses, cones, driveway corners, fence posts. Spread them out to cover the
   stretch where cars will be timed.
2. Define an origin `(0,0)` and axes in meters, e.g. **X = along the road**,
   **Y = across it**. Write down each point's `(X, Y)`.
   Example rectangle: `(0,0) (20,0) (20,4) (0,4)`.
3. Run `python tools/calibrate.py`, press **SPACE** to freeze a frame, click the
   points **in the same order** as your measured list, press **ENTER**, and type
   each `X Y` in the terminal.
4. It saves `calibration.json` and reports a **reprojection error** in meters.
   Under ~0.3 m is good; if it's large, re-measure.

Tip: you can also calibrate from a saved photo: `python tools/calibrate.py --image road.jpg`.

---

## Configuration

Everything is in `config.yaml` (fully commented). The knobs you'll actually
touch:

| Setting | What it does |
|---|---|
| `camera.source` | Webcam index (0), or a video file / RTSP URL for testing |
| `camera.backend` | `auto` (poll on boot: CSI camera if present, else USB webcam), or force `opencv` / `picamera2` |
| `detection.min_area` | Blob size to count as a vehicle — tune to your framing |
| `speed.speed_limit_kmh` | Flags/annotates speeders |
| `speed.display_units` | `mph` or `kmh` |
| `recording.clip_seconds` | Seconds of pre-trigger footage kept in the clip |
| `recording.speedkapture_threshold` | **SpeedKapture** — only record/post clips above this speed (also set live on the dashboard) |
| `recording.always_snapshot` | Keep a JPEG for every pass (even sub-threshold) so deferred recognition can enrich them later |
| `retention.local_days` | Auto-delete local clips older than N days (once backed up) |
| `backup.mirror_all` | Mirror every counted pass off-site (row + snapshot), not just captured clips — full historical archive |
| `backup.remote_retention_days` | Auto-delete off-site clips older than N days |
| `control.enabled` | Camera checks in with the off-site host for liveness + settings you change on the dashboard |
| `recognition.enabled` | Best-effort vehicle type/make/model/year/colour (needs `ultralytics`) |
| `recognition.defer` | Offload YOLO to another machine (busy roads only) — keep `false` for a low-traffic drive |
| `display.show_window` | `false` for headless Pi deployment |

---

## Recommended hardware

**Compute — Raspberry Pi 5 (4 GB or 8 GB).**
An Arduino cannot do computer vision; this is a video-processing task. A Pi 5
runs this OpenCV pipeline comfortably in real time. (A Pi 4 works but slower.)

**Camera — Raspberry Pi Global Shutter Camera (Sony IMX296) + a 6 mm or 8 mm
CS-mount lens.**
For a *speed* camera the single most important feature is a **global shutter**:
it exposes the whole frame at once, so a fast car isn't skewed/smeared like it
is on the rolling-shutter sensors in most webcams and phone cameras. Pair it
with a manual exposure short enough to freeze motion. Pick the lens focal length
for your distance/road width (6 mm ≈ wider, 8 mm ≈ tighter/farther).

- *Budget alternative:* Raspberry Pi Camera Module 3 (autofocus, rolling
  shutter) — fine for the lower speeds of a private road.
- *Testing now:* your USB Logitech webcam works with zero code changes.

**Also:** a good power supply, a 64 GB+ A2 microSD (or a USB SSD for lots of
clips), and a weatherproof enclosure with a clear window + sun shade for outdoor
mounting. Mount the camera **rigidly** — any wobble invalidates the calibration.

### Moving to the Pi

> **Full runbook:** [`deploy/raspberry-pi.md`](deploy/raspberry-pi.md) walks
> through Deploy / Setup / Maintain end-to-end (OS flash, dependencies, headless
> calibration, autostart) and includes minimum system requirements — you
> probably don't need a Pi 5. The steps below are the short version.

```bash
sudo apt update && sudo apt install -y python3-opencv python3-picamera2 python3-yaml
git clone <this project>  # or copy the folder
```
Then in `config.yaml` set `display.show_window: false` and re-run `calibrate.py`
in the final mounted position. Leave `camera.backend: auto` (the default) and it
picks the CSI camera or a USB webcam automatically at boot — or pin it to
`picamera2` / `opencv` if you want to force one.

### Autorun on boot (plug in and go)

Install the systemd service once, on the Pi, from inside the project folder:

```bash
sudo bash deploy/install-service.sh
```

That's it. From now on SpeedKam starts automatically at every boot, serves the
**web dashboard on port 8080**, and restarts itself if it ever crashes or the
camera hiccups — so you can just give the Pi power with no keyboard/monitor
attached and open `http://<pi-ip>:8080` from your phone. (Prefer a fully
headless run with no web UI? Edit the unit's `ExecStart` to use
`run.py --no-display` instead of `serve.py`.)

The installer figures out the login user, project path, and Python for you,
adds that user to the `video` group for camera access, renders
`deploy/speedkam.service`, and enables it. By default it uses the project
`.venv` if present, otherwise the system `python3` (which is what you want when
you installed OpenCV/picamera2 via `apt`). Force a specific interpreter with
`sudo PYTHON=/usr/bin/python3 bash deploy/install-service.sh`.

Manage it with:

```bash
sudo systemctl status speedkam     # is it running?
journalctl -u speedkam -f          # live logs / measured speeds
sudo systemctl restart speedkam    # after editing config.yaml or recalibrating
sudo systemctl stop speedkam       # stop for now
sudo bash deploy/uninstall-service.sh   # remove it entirely
```

Do calibrate (`python tools/calibrate.py`) and set the right `camera.source`
**before** relying on it — the service reads the same `config.yaml` and
`calibration.json`. After recalibrating, `sudo systemctl restart speedkam`.

### Deploy a fleet — clone one SD card to many

Setting up more than one camera? Build **one** fully-installed card and clone its
image to the rest instead of provisioning each by hand. On the Pi you're turning
into the master, run:

```bash
sudo bash deploy/image/provision.sh
```

It installs every dependency from `apt`, installs the autostart service, wipes
per-node state so the image starts blank, and arms a first-boot step that gives
each clone a unique hostname + fresh SSH host keys / machine-id. Then capture the
card to an `.img` and flash it to as many cards as you like — full recipe in
[`deploy/image/README.md`](deploy/image/README.md). Each node still gets a
one-time on-site calibration; nothing else is manual.

> First-time gotcha: if the installer just added you to the `video` group,
> reboot once so the running service picks up camera permissions.

---

## Project layout

```
config.yaml            Main configuration (commented; secret-free, tracked)
config.local.example.yaml  Template for the untracked secrets overlay
config.local.yaml      Your real secrets/overrides (gitignored; merged over config.yaml)
config.test.yaml       Config for the synthetic self-test
run.py                 Entry point (desktop preview window)
serve.py               Entry point (web dashboard) -- recommended
tools/
  camera_check.py      Find your webcam index
  calibrate.py         Interactive ground-plane calibration
  make_test_video.py   Generate a synthetic drive-by to validate speed math
  backfill_sync.py     Push all existing local records to the off-site backup
  recognize_worker.py  Run YOLO off-box to fill deferred type/make/model attributes
deploy/
  speedkam.service     systemd unit template (autorun on boot)
  install-service.sh   One-command installer for the Pi
  uninstall-service.sh Remove the service
  image/               Golden-image build: clone one SD card to a whole fleet
    provision.sh              Turn a fresh Pi OS card into a ready-to-clone master
    firstboot.sh              Per-clone identity reset (hostname/SSH keys/machine-id)
    speedkam-firstboot.service  Runs firstboot.sh once on each clone
    README.md                 Capture + shrink + clone workflow
  webhost/             Off-site host: backup receiver + web dashboard
    speedkam_config.example.php  Template for the shared settings (tracked)
    speedkam_config.php        Your copy: secret + dashboard password (gitignored)
    speedkam_receiver.php      Camera endpoint (uploads + heartbeat + settings)
    speedkam_dashboard.php     Password-gated web UI: view records + control camera
    htaccess-for-data-folder   Protect the backup folder from public access
    README.md                  Web-host setup guide
src/speedkam/
  capture.py           Camera abstraction (webcam / file / RTSP / picamera2)
  detector.py          MOG2 motion detection
  tracker.py           Multi-object tracker
  calibration.py       Homography (pixels -> meters)
  speed.py             Robust speed estimation
  recorder.py          Ring-buffer clip + snapshot + CSV logging
  recognition.py       Optional vehicle type/make/model/year/colour (YOLO)
  retention.py         Local + remote media rotation (SD card / off-site)
  state.py             Dashboard-adjustable settings (SpeedKapture), persisted
  annotate.py          Overlays
  pipeline.py          Ties it all together
  web.py               Flask dashboard + pipeline runner thread
  sync.py              Off-site backup uploader (disk-queued, retrying)
  remotecontrol.py     Pull-based remote control + heartbeat to the off-site host
  webui/               dashboard.html, calibrate.html
```

---

## Accuracy & limitations

- Speed is only as good as the calibration and a rigid mount. Re-calibrate if
  the camera moves.
- Motion-blur (rolling shutter, long exposure) hurts the ground-point estimate
  at speed — hence the global-shutter recommendation.
- The far field (top of frame) has poor metric resolution; keep the measured
  zone in the nearer, well-conditioned part of the image.
- Background subtraction handles one vehicle at a time well; heavy/overlapping
  traffic would want a learned detector (YOLO) — the detector module is a
  drop-in swap behind the same interface.

## Privacy & legal note

This is a monitoring/measurement tool for **your own private road**. It is not a
calibrated legal-enforcement instrument. It records video/snapshots of passing
vehicles (which can incidentally capture plates, faces, and neighbours) and logs
timestamped speeds and attributes — so treat that data as sensitive. If you plan
to record beyond your property or use footage against third parties, check your
local laws on video recording and privacy first.

**See [docs/PRIVACY.md](docs/PRIVACY.md)** for an operator checklist: what's
stored, what local rules may apply, and how to deploy responsibly (aim tight, set
retention, protect the dashboards, use HTTPS). It is guidance, not legal advice.

## License

Copyright (C) 2026 Kris Kling.

SpeedKam is free software licensed under the **GNU Affero General Public License,
version 3 or later (AGPL-3.0-or-later)** — see [`LICENSE`](LICENSE). Every source
file carries an `SPDX-License-Identifier` header. The AGPL's network clause
(section 13) means that if you run a modified version as a network service, you
must offer its users the corresponding source.

> **Why AGPL:** the optional vehicle-recognition feature depends on
> [Ultralytics](https://github.com/ultralytics/ultralytics) (YOLO), which is
> itself **AGPL-3.0**. Enabling `recognition.enabled: true` pulls that in, so the
> project as a whole is AGPL to stay compatible. If you need a permissive
> license, you must run with recognition **disabled** and remove the Ultralytics
> dependency — the rest of the stack (OpenCV, NumPy, Flask, PyYAML, requests) is
> permissively licensed. The `yolov8n.pt` weights are downloaded on demand and
> are **not** distributed with this repository.
