# Pi 3 performance tuning — the full journey

A field investigation of a Raspberry Pi 3 Model B node (Sony IMX296 global-shutter
camera) that *felt* stuck at ~3 fps. It turned out to be **thermal throttling plus a
single-threaded pipeline** — not a slow board — and was taken to the camera's 30 fps
sensor ceiling with **no new hardware**, just a ~$5 cooler and better use of the cores
already present.

This doc records what we measured, what we tried, what was a red herring, and where it
landed — so we don't re-litigate any of it.

## TL;DR

| Stage | Loop fps | Notes |
|---|---|---|
| Reported symptom | "3" | Actually the choppy browser *preview*; the real loop was 9.5 |
| Encode off-thread + preview downscale + `detect_scale 0.4` | 14.5 | |
| Parallel capture‖process (no cooler) | 13 | Extra cores → more heat → *harder* throttle |
| + temporary cooler | 14 | Thermal fixed, but process thread still annotating every frame |
| + preview render moved to the encoder thread | **~29.5** | **Sensor-capped (camera at 30 fps), 66 °C, ~2.4/4 cores** |

**From a perceived 3 fps to the hardware maximum, $0 of new compute.**

Then a follow-on chapter — [Reclaiming clip quality](#reclaiming-clip-quality-2026-08-21-post-cooling) —
took the *saved clips* from 15 fps back to the full 30 fps once the node was cooled, with **no
change to the detection frame rate** (the clip-rate/buffer knobs were RAM limits, not fps limits).

A later chapter — [Native-resolution capture](#native-resolution-capture-2026-08-22) — then
deliberately traded some of that frame rate back for **detail**, moving capture to the IMX296's
native **1456×1088** (720p had been a downscale of it) and dropping `detect_scale` to 0.3 to hold
**~23 fps** on the Pi 3 while clips gain the full 1.58 MP.

A further chapter — [Browser-playable clips](#browser-playable-clips-2026-08-22) — is off the
fps/detail axis entirely: the clips were being written in a codec (`mp4v`) that desktop players
accept but **browsers refuse**, so the dashboard's inline player showed only an error. The fix
switched the encoder to H.264 (`avc1`) and back-filled the existing clips.

A further chapter — [The low-light gate](#the-low-light-gate-2026-08-23) — is about *when* the
pipeline should run at all: after dark the motion detector locks onto headlight glare and sensor
noise instead of cars, inventing phantom "90–170 mph" passes. Rather than a slower/faster or
sharper trade, this one **pauses detection when the scene is too dark and auto-resumes at dawn** —
which also happens to let the node idle cool all night instead of burning cores on noise.

A last chapter — [The road-region gate](#the-road-region-gate-2026-08-23) — is the daylight
counterpart to that: two children standing on the grass *in front of* the camera were logged as a
**69 mph car**. The motion blob was real; it just wasn't on the road, so the road-only speed
homography extrapolated their footsteps into a phantom speed. The fix stops trusting shape/size
proxies and gates on the one invariant that actually separates a car from a bystander — **is the
object's ground contact on the calibrated road at all?**

A closing note points forward to the hardware step that finally makes *content* classification
(car vs person, not just geometry) affordable on-node — see the separate
[Pi 4 / Phase 14 plan](pi4-yolo-classifier-phase14.md).

## The board question that started it

The original question was whether to buy a **Pi 4 (2 GB)** or a **Pi 5 (1 GB)**, later
reframed as **Pi 4/4 GB ($94)** vs **Pi 5/8 GB ($170)**. The answer that emerged from the
data: **buy neither — buy a ~$5 heatsink + fan.** The Pi 3 was thermally capped with two
idle cores, so more/faster silicon wasn't the constraint. A Pi 5 also runs hotter and
draws more, so in the same passive enclosure it would throttle *worse*, not better.

## Method: measure, don't guess

Every step was driven by hardware readings, not hunches. The two instruments:

- **`tools/fps_profile.py`** — per-stage millisecond breakdown (capture / detect / encode /
  copy) plus `vcgencmd` health (throttle bits, temp, arm clock), memory/swap, and loadavg.
  Run it with the service **stopped** (it needs the camera): `sudo systemctl stop speedkam
  && python3 tools/fps_profile.py --frames 60 && sudo systemctl start speedkam`.
- **`/api/status`** — the live **detection-loop** fps (`fps` field). This is the number
  that matters; the browser preview refresh is a *different* number and is what "3 fps"
  actually referred to.

Key `vcgencmd get_throttled` bits: `bit 0` under-voltage now, **`bit 1` ARM frequency
capped now**, `bit 16` under-voltage since boot, **`bit 17` frequency cap since boot**,
`bit 18` throttled since boot. `0x0` is clean. We saw `0x20002` (bit 1 + bit 17 = *actively*
frequency-capped) at 81.7 °C, and `0x20000` (only the historical bit) once cooled.

## What we found

### Red herrings (ruled out with evidence)
- **Inline YOLO recognition.** Config had `recognition.enabled: true, defer: false`, which
  *looked* like it would run YOLO on the capture thread per car. But the node logged
  `ultralytics not available (No module named 'ultralytics')` — YOLO **never ran**; it
  self-downgraded to the cheap color pass. Not the wall. (We still set `defer: true` as
  harmless hardening in case ultralytics is ever installed.)
- **Under-voltage.** `get_throttled` showed no under-voltage bits. The PSU was fine.
- **Memory / swap thrash.** `free -h` showed 0 B swap in use and plenty free. The recorder
  buffer was already RAM-bounded.

### The two real limits
1. **Thermal throttling.** Under sustained load the chip hit 81.7 °C and the ARM-frequency
   cap engaged (`0x20002`), dropping the clock. This alone turned a ~14 fps-capable pipeline
   into ~9–13.
2. **Single-threaded pipeline.** `python3` used ~159 % CPU (~1.6 of 4 cores). **Two cores
   sat idle** while the one capture/detect/encode thread was the bottleneck.

## What we changed (in order), and why

Each landed as a commit on `main` and was deployed to the node over SSH (edit → commit →
push → `git pull --ff-only` + `systemctl restart` on the Pi), then re-measured.

1. **`9e69e20` — defer recognition.** Inert here (no ultralytics), kept as hardening.
2. **`6c3c155` — preview downscale + off-thread encode.** The live MJPEG preview was being
   JPEG-encoded at full 1280×720 (~34 ms) **inline on the capture thread**. Moved the encode
   to its own thread and downscaled the preview to 640 px first (`web.stream_max_width`).
   → **9.5 → 14.5 fps.**
3. **`5249838` — `detect_scale 0.4` fleet default + parallel capture‖process pipeline.**
   Split the headless run path so **camera capture runs on one thread and
   detect/track/finalize on another** (`pipeline.py` `_run_parallel` / `_process_frame`).
   The capture thread records *every* frame (gap-free clips); the process thread works the
   latest frame (drops intermediate ones for detection only). `Recorder` made thread-safe
   (a lock guards the ring buffer; slow video encode runs off a snapshot).
   → On the *uncooled* Pi this made fps **worse (13)**: lighting up more cores generated more
   heat, which deepened the throttle. **This is the definitive proof the wall was watts of
   heat dissipation, not cores.**
4. **Temporary cooler installed** → 66 °C, no active throttle, full 1.2 GHz. But fps stayed
   ~14: profiling showed `capture + detect` alone can do **31.6 fps**, yet the process thread
   was *still* doing the preview `frame.copy()` + all `annotate` drawing every frame —
   preview-only cosmetic work sitting on the measurement path.
5. **`162f4b5` — move preview copy+annotate off the process thread.** The process thread now
   takes only a tiny **immutable overlay snapshot** (per-track id/bbox/ground + HUD text) and
   hands it off; the **encoder thread** does the copy + annotate (`SpeedCamera.render_preview`)
   + resize + encode. Because that thread is already throttled to `stream_fps`, annotation
   also stops running on frames nobody sees. `annotate.draw_track_boxes` draws from snapshots
   so it's safe off the tracker thread. The desktop-window path still renders inline.
   → **14 → ~29.5 fps (the 30 fps sensor cap).** Now sensor-limited, not compute-limited.
6. **`a5e373c` — `recording.record_fps` cap (default 15).** With capture now delivering
   ~30 fps, the recorder buffered every frame at the sensor rate — but the 128 MB clip-buffer
   cap on a 1 GB Pi then held only ~1.5 s of 720p, shrinking the saved-clip pre-roll.
   `Recorder.push` now throttles *stored* frames in frame-time, decoupling clip rate from
   detection. At 15 fps the pre-roll is ~3.1 s within the same RAM.

## Final architecture (headless / web path)

Four kinds of work now spread across the Pi's cores:

- **Capture thread** — reads the camera at the sensor rate, records every frame to the ring
  buffer (rate-limited by `record_fps`), publishes the latest frame to the process thread.
- **Process thread** — detect → track → finalize (speed, logging, clip save). Takes a cheap
  overlay snapshot for the preview and hands it off. This is the measurement-critical path.
- **Encoder thread** — renders the annotated preview (copy + annotate) and JPEG-encodes it at
  `stream_fps`, off the measurement path.
- Flask, backup sync, remote control, retention — their own threads as before.

The **desktop window path** (`run.py` with a preview window) stays single-threaded, because
`cv2.imshow` must run on one thread; it shares the per-frame logic via `_process_frame`.

## Config knobs (what to turn, and when)

All in `config.yaml` (fleet-wide) or `config.local.yaml` (per-node override, deep-merged):

- **`detection.detect_scale`** (0.3) — detection runs on a frame scaled by this factor. Lower
  = less CPU/heat, at the cost of small/distant vehicles and speed precision (centroids scale
  back up by 1/scale). Was **0.4** at 720p; dropped to **0.3** when capture went native
  1456×1088 to recover the frame rate (see
  [Native-resolution capture](#native-resolution-capture-2026-08-22)). Raise toward 0.35–0.4 if
  distant cars get missed, or 0.5–1.0 on a Pi 4/5.
- **`web.stream_fps`** (10) / **`web.stream_max_width`** (640) — preview refresh cap and
  preview downscale width. Preview only; never limits detection.
- **`recording.record_fps`** (0 = every frame) — frames/sec actually stored for clips,
  independent of detection. A RAM-vs-smoothness knob, **not an fps knob** — the detection loop
  runs at the sensor cap regardless. It was **15** through the cooling work (to fit the pre-roll
  into a 1 GB Pi's RAM), then set to **0** (full sensor rate) once cooled — see
  [Reclaiming clip quality](#reclaiming-clip-quality-2026-08-21-post-cooling). **Do not set it
  equal to the sensor rate** (e.g. `30` on a 30 fps camera): the throttle drops any frame
  arriving `< 1/record_fps` after the last kept one, and at the sensor rate real inter-frame
  jitter trips that on ~1 in 4 frames, giving only ~22 fps clips. `0` for full rate; a value
  *well below* the sensor rate to deliberately trade smoothness for a longer pre-roll.
- **`recording.max_buffer_mb`** (256) — hard RAM ceiling for the clip buffer. 256 MB (~92
  720p frames) holds ~3.1 s of pre-roll at 30 fps and fits a cooled 1 GB Pi (~350 MB headroom
  measured). Was 128 during the cooling work; drop back to 128 if RAM gets tight, raise further
  on a Pi 4/5.
- **`camera.width` / `camera.height`** (1456×1088) — capture resolution, set to the IMX296's
  native 1.58 MP for maximum plate detail. The sensor's only libcamera mode *is* 1456×1088, so
  720p was a downscale of it. Native costs frame rate on a Pi 3 (compute-limited *below* the
  sensor cap) — see [Native-resolution capture](#native-resolution-capture-2026-08-22). Drop to
  1280×720 to trade detail for ~29 fps.
- **`camera.fps`** (30) — the sensor frame-duration cap. The detection loop can't exceed this;
  raising it past 30 buys little for a speed camera and adds heat. At native resolution the loop
  is compute-limited well under this cap anyway (~23 fps).
- **`light_gate.*`** — pause detection when it's too dark to work and resume at dawn (see
  [The low-light gate](#the-low-light-gate-2026-08-23)). `enabled` (true); `sleep_below` (40) /
  `wake_above` (60) — mean-luma (0–255) thresholds with a deliberate dead-band so dusk/dawn cross
  once instead of flapping; `dwell_seconds` (30) — how long brightness must hold past a threshold
  before the gate flips (rejects a passing headlight or a momentary dark truck). Tune the two
  thresholds from the dashboard's live **brightness** readout at your site — the reference node
  read ~90–105 in daylight and <15 in full dark, so 40/60 sits safely in the gap.
- **`speed.min_on_road_frac`** (0.6) / **`speed.road_margin_frac`** (0.03) — the road-region gate
  (see [The road-region gate](#the-road-region-gate-2026-08-23)). A track is rejected as off-road
  junk unless at least `min_on_road_frac` of its ground points fall on the calibrated road surface;
  `road_margin_frac` widens the road box by that fraction of the frame on its near and lateral
  edges (the far edge is intentionally open so distant cars still count). Needs a calibration to
  apply; `min_on_road_frac 0` disables it. Lower `min_on_road_frac` if real cars ever get rejected;
  raise it to be stricter about foreground/roadside motion.

### Clip length vs record_fps and buffer (720p, ~2.76 MB/frame)

Pre-roll seconds ≈ (`max_buffer_mb` ÷ 2.76) ÷ effective-stored-fps. The buffer default moved
from 128 MB to 256 MB alongside `record_fps` 15 → 0 (full rate), so the pre-roll length held at
~3.1 s while the motion doubled in smoothness. "Effective stored fps" is the *delivered* rate
when `record_fps` is 0 or set above it — the throttle only reduces it when set below delivery
(and, per the gotcha above, mangles it when set right at the sensor rate):

| `record_fps` | @128 MB | @256 MB (default) | Look |
|---|---|---|---|
| **0 = full 30 (default)** | ~1.5 s | **~3.1 s (measured 30.04 fps)** | smoothest |
| 15 (old default) | ~3.1 s | ~6.1 s | smooth |
| ~8 | ~5.7 s | ~11 s | slightly steppy |
| ~4 | ~11 s (full window) | full window | choppy but complete |

> This table is for 720p (~2.76 MB/frame). At native 1456×1088 a frame is ~4.75 MB (1.72×),
> so the same 256 MB buffer holds ~54 frames ≈ ~2.3 s at the ~23 fps native loop rate — see
> [Native-resolution capture](#native-resolution-capture-2026-08-22).

**Is 3 s enough?** Yes for the intended use (fast cars only). Time on screen = meters of road
in frame ÷ speed. A typical side-on view shows ~15–25 m, so a 30 mph car crosses in
~1.1–1.9 s and anything faster is quicker — fast cars need *less* buffer, not more. The clip
also triggers ~0.4 s after the car exits (`tracker.max_missed`), so 3.1 s of pre-roll covers
the whole pass plus a little approach. Raising `max_buffer_mb` mostly buys more approach
footage of cars you don't care about, at real RAM cost.

## Open items / re-check after calibration

- **The node is not yet calibrated** (`calibration.json` absent → detection-only, no speeds
  reported). Calibrate on-site (dashboard → Calibrate). After that, the real along-road frame
  coverage can be read from `calibration.json` to confirm clip length in seconds; if the
  framing is unusually wide, bump `record_fps` down (or `max_buffer_mb` up) a notch.
- **The cooler is temporary.** The permanent heatsink+fan is a drop-in; nothing in software
  needs to change — the node is already at the sensor cap with thermal headroom.
- **Headroom remains** (~2.4/4 cores at 66 °C). Going past 30 fps would require raising
  `camera.fps` and revisiting `record_fps`/`max_buffer_mb`; not needed for a speed camera.

## Reclaiming clip quality (2026-08-21, post-cooling)

Once the node was sitting comfortably at the 30 fps sensor cap with thermal headroom, the
question became the *inverse* of the whole journey: **now that we're fast, what did we give up
to get here that we can take back?** Specifically — could the saved clips and thumbnails be
made better without spending the frame rate again?

The audit split every change we'd made into two classes, and the distinction is the whole
lesson:

- **Detection-path changes** (`detect_scale 0.4`, off-thread encode, parallel capture‖process,
  preview-render offload) — these *are* the fps. Reversing any of them costs frame rate and
  reclaims quality nowhere useful. Left alone.
- **RAM-vs-media changes** (`record_fps 15`, `max_buffer_mb 128`) — these were **never fps
  optimizations.** They existed only because a 1 GB Pi couldn't hold a usable pre-roll of
  720p frames once capture started delivering the full 30 fps. The detection loop runs at the
  sensor cap no matter what they're set to.

So the one thing degrading the clips — 15 fps stored motion — was reversible for the price of
**RAM, not frame rate.** Two things were checked and found already-optimal and left as-is:
clip **resolution** (stored frames are full 1280×720; `detect_scale` only shrinks the
*detection* copy, never the saved frame) and the **snapshot/thumbnail** (already a full-res,
quality-95 JPEG).

**The change:** `record_fps` 15 → 0 (store every frame, full sensor rate) and `max_buffer_mb`
128 → 256 — so the pre-roll stays ~3.1 s at the doubled rate. This took **two commits**, and
the correction is the interesting part:

- **`a0bb4db` (first try) — `record_fps: 30`.** The obvious reading of "store at 30 fps." It
  deployed cleanly and detection stayed at 29–30 fps, so at a glance it looked done.
- **`c0ae380` (the fix) — `record_fps: 0`.** Verifying by writing an *actual clip through the
  deployed `Recorder`* (below) exposed that `record_fps: 30` produced only **~22.6 fps** clips,
  not 30. `Recorder.push` drops any frame arriving `< 1/record_fps` after the last kept one;
  with the camera at a clean 30.0 fps, real inter-frame jitter puts ~1 in 4 gaps just under
  33.3 ms, so a quarter of frames were dropped. **Setting the throttle equal to the sensor rate
  fights the sensor.** `record_fps: 0` (no throttle, every frame) gives true full rate.

**Verification method (uncalibrated node, no traffic).** The node is uncalibrated and
`save_only_with_speed` is on, so it saves no clips on its own — nothing to "grab." Instead a
probe drove the **real deployed `Recorder` against the live picamera2 camera** (service stopped
for exclusive camera access, ~5 s per run, restarted after), then read the written MP4's fps
back with OpenCV. This exercises the exact saved-clip code path with the on-disk config.

| `record_fps` | Camera delivery | Written clip fps | Frames / pre-roll |
|---|---|---|---|
| `30` (first try) | 30.01 fps | **22.6 fps** ✗ | 92 / 4.08 s |
| `0` (fix, deployed) | 30.03 fps | **30.04 fps** ✓ | 92 / 3.06 s |

**Before/after on the node** (detection loop + resources, `record_fps: 0` deployed):

| Metric | Before (15 fps / 128 MB) | After (0 = 30 fps / 256 MB) |
|---|---|---|
| Stored clip rate | 15 fps | **30.04 fps** (measured, 2× smoother) |
| Pre-roll length | ~3.1 s | ~3.06 s (held, by design) |
| Detection loop fps | 29.6–29.9 (one 25.4 dip) | **29.7–30.5, steady — no loss** |
| Temp / throttle | 66 °C, `0x20000` (historical) | 58–66 °C, `0x20000` — no active throttle |
| RAM used / available | 425 / 480 MB | 531 / 374 MB |
| Swap | 0 MB | ~11–18 MB (stable, not climbing) |

The predicted trade held: **clips took the full sensor rate, detection fps didn't move**, and
the extra ~100 MB of resident RAM sat within the cooled node's headroom. The swap that appeared
is Linux evicting cold pages to fund the larger buffer + file cache — it settled, which is
eviction, not thrash (thrash climbs tens of MB while `available` collapses; `available` held at
~374 MB). If a future feature needs the headroom back, `max_buffer_mb 192` reclaims 64 MB while
keeping `record_fps 0` (pre-roll ~2.3 s).

**Lesson within the lesson:** "store at 30 fps" and "don't throttle" are *not* the same setting
when the throttle threshold lands on the sensor rate — and the only way that surfaced was
writing a real clip and measuring it, not trusting that the config *looked* right. Detection fps
staying green said nothing about the clip rate, because they're independent by design.

**Lesson:** keep the RAM-vs-media knobs mentally separate from the fps knobs. It's easy, after
a hard-won frame-rate fight, to assume every constraint you added was protecting the frame
rate — but `record_fps` and `max_buffer_mb` were only ever protecting *memory*, and cost
nothing to relax once the memory was there.

## Native-resolution capture (2026-08-22)

The journey above ended at the **30 fps sensor cap at 1280×720** — but capture was *downscaling*
the IMX296's native 1456×1088 readout to 720p, throwing away ~40% of the sensor's pixels before
the pipeline ever saw them. On a speed camera those pixels are plate legibility. So the next
question was the detail/rate trade: **run native resolution and still keep a usable frame rate?**

Native resolution moves the node **back off** the sensor cap — at 1456×1088 the Pi 3 is
compute-limited again, not sensor-limited, so this is a deliberate detail-for-fps trade, not a
free win.

**Measured on `speedkam-47790c` (live `/api/status`):**

| Capture | `detect_scale` | Detection frame | Loop fps | Limited by |
|---|---|---|---|---|
| 1280×720 | 0.4 | 512×288 | ~29 | sensor cap (30) |
| 1456×1088 (native) | 0.4 | 582×435 | ~16 | compute |
| **1456×1088 (native)** | **0.3** | **437×326** | **~23** | compute |

Going native at the old `detect_scale 0.4` dropped the loop to ~16 fps — the 1.72× pixel jump
(0.92 → 1.58 MP) hits both capture *and* the detection frame. Dropping `detect_scale` to **0.3**
recovered most of it: detection cost scales with the **square** of the factor, so 0.4 → 0.3 is
~44% less detection pixel work (582×435 → 437×326), buying **~16 → ~23 fps** while capture and
saved clips stay full native resolution.

**Why 0.3 is the floor, not lower.** The detector centroid is measured in detection space and
scaled back up by 1/scale — ×3.33 at 0.3 (vs ×2.5 at 0.4). A 1-px jitter in the coarse frame
becomes a ~3.3-px wobble in full-res coordinates, feeding the pixels→metres speed calc — so
speed on fast cars gets slightly noisier and small/distant vehicles (fewer detection pixels)
lose some reach. 0.3 is about as low as a car-blob detector should go; **raise toward 0.35–0.4
if distant cars start getting missed** (costs a few fps).

**Stability (4-minute watch, 20 s samples).** `active` every sample, **0 restarts**, loop
**18.6–22.1 fps (avg ~21)**, temp **flat at ~70.9 °C** (equilibrium, not climbing — below the
80 °C soft-throttle point), `get_throttled 0x20000` (historical bit only, no active throttle),
free RAM 334–359 MB steady, no log errors. The heavier native-res load runs a few degrees
warmer than the 66 °C of the 720p era but is thermally stable with headroom.

**Clip-buffer consequence (computed — verify on a calibrated node).** Saved clips store at full
capture resolution, so a native frame is **~4.75 MB** vs ~2.76 MB at 720p (1.72×). With
`max_buffer_mb 256` and `record_fps 0` (full rate), the buffer now holds ~54 native frames; at
~23 fps stored that's **~2.3 s of pre-roll** (down from ~3.1 s at 720p/30). Still covers a
fast-car pass (see the clip-length reasoning above), but if it reads short once calibrated,
raise `max_buffer_mb` toward 320–384.

**Committed:** `cf47f3b` (`camera.width`/`height` → 1456×1088, `detect_scale` → 0.3). The
`config.py` defaults were separately synced to the live `config.yaml` in `5a10e89`.

## Browser-playable clips (2026-08-22)

Every chapter above is about *making* the clips — frame rate, resolution, pre-roll. This one is
about a clip that recorded perfectly and still wouldn't **play**. Clicking the 7:03 PM / 49 mph
pass in the dashboard's clip grid returned a bare browser error:

> No video with supported format and MIME type found.

**Root cause: codec, not container.** `recorder.py` wrote clips with
`cv2.VideoWriter_fourcc(*"mp4v")` — an `.mp4` file, but the *codec* inside was **MPEG-4 Part 2**
(the old DivX/Xvid family). Desktop players (VLC, the OS media player) decode it fine, so it
looked correct for years. But **HTML5 `<video>` supports only H.264 (`avc1`), VP9, and AV1** —
never MPEG-4 Part 2. The bug had always existed; it only became *visible* when Phase 10 added the
inline clip player. Before that, clips were download-and-open-in-VLC, which hid it. The `.mp4`
extension is a container name and says nothing about whether a browser can play the bytes inside.

Confirmed by reading the offending file's fourcc back with OpenCV: `mp4v`, 1456×1088, 22 fps.

### The fix (two parts)

**1. Future clips — the encoder.** Switched the `VideoWriter` to `avc1` (real H.264). The node's
OpenCV is FFmpeg-backed (`avcodec 59.37`), and a probe confirmed it writes genuine `avc1` for the
`avc1`/`H264`/`X264` tags (all fall through to the same encoder); only `mp4v` stays MPEG-4 Part 2.
The change keeps an **mp4v fallback** guarded on `writer.isOpened()`: if some fleet node's OpenCV
build can't open an H.264 writer, it still records a (VLC-playable) clip rather than a silently
**empty** file — a failed-to-open `VideoWriter` drops every frame. Committed `18b8e5c`, deployed
to the node (pull + restart), clean restart verified.

**2. Existing clips — the backfill.** The 19 clips already recorded (on the node *and* mirrored
off-site) were still `mp4v` and stayed unplayable regardless of the source fix. Re-encoded all 19
on the Pi — OpenCV decodes `mp4v` → re-encodes `avc1`, written to a temp file and `os.replace`d
in only after verifying the output's fourcc is `avc1` and frame count > 0 (atomic, never leaves a
half-written clip). Then pushed the re-encoded files up to the webhost, overwriting the `mp4v`
copies. **The transcode had to run on the Pi**: the Bluehost webhost is SFTP-only with no shell
and no ffmpeg, so it can't transcode its own copies — the node is the only machine in the system
that can encode video. Verified byte-for-byte size match between the local re-encode and the
uploaded file.

### Verification: actually play it, don't trust the fourcc

Same discipline as the `record_fps` chapter — a clip reading back as `avc1` proves the *tag*, not
that a browser will decode it (profile, pixel format, and moov atom all matter). So the
re-encoded 49 mph clip was served over a real localhost HTTP server and loaded into an actual
Chromium engine with `<video>` event listeners. It fired **`loadeddata` (1456×1088)** and
**`playing`** — genuine decode-and-play in a browser, not just a valid-looking header.

### Gotcha: the cache hides the fix

The dashboard's media proxy sends `Cache-Control: private, max-age=3600`. The broken `mp4v`
response for a given `?media=…` URL can sit in the browser cache for up to an hour, so the fixed
clip won't appear until a **hard refresh** (Ctrl/Cmd-F5) evicts it. The clip was already fixed
on-disk; the stale 200 was purely client-side.

### Lesson

**A file extension is a container; playability is a codec.** `.mp4` told us nothing — `mp4v`
inside it is a browser dead-end. And "it plays on my machine" meant VLC, not the target
(browsers), which is the whole point of a web dashboard. When the surface is a browser, the only
honest test is a browser.

## The low-light gate (2026-08-23)

Every chapter above tuned *how well* the pipeline runs. This one is about *whether it should be
running at all* — because after dark it was producing confident garbage.

**The symptom.** As night fell on 2026-08-22 the node logged a wave of false positives at
impossible speeds — **91 mph, 107 mph, 52 mph**, several with tiny sample counts (7–10 points,
sub-second tracks). By day the same node reads clean.

**Root cause: the detector has nothing to lock onto in the dark.** SpeedKam's motion detector
(MOG2 background subtraction) finds *moving foreground*. In daylight that's a car. At night the
scene is near-black, so the only things that move-and-contrast are **headlight glare sweeping the
road and sensor noise** flickering frame to frame. The tracker stitches those into "objects," and
because a glare blob can jump a long way between frames, the pixels→metres math reports a wild
speed. The existing false-positive gates can't save it: `max_track_distance_m`, `min_vehicle_span_m`
and `min_vehicle_aspect` all assume a *real* blob with plausible size/shape, and a headlight
smear can satisfy all three. The problem isn't a bad reading of a car — it's that **there is no
car**, and no per-track geometry check distinguishes "headlight" from "small fast vehicle."

**The data drew the line for us.** Rather than guess a sunset time, we measured the mean luma
(0–255) of every evening snapshot the node had saved:

| Time | Mean frame luma | State |
|---|---|---|
| 19:00–20:17 | **90–105** | daylight — real cars, measured fine |
| 20:35 → 20:42 | 73 → 50 → 30 | dusk collapse |
| 20:46 onward | 25 → **2–13** | dark — every "pass" a glare/noise phantom |

The transition is sharp and monotonic, and detection quality dies right as brightness crosses
~40. That clean separation is what makes a brightness gate viable: there's a wide, unambiguous
dead-band between "working" (>60) and "hopeless" (<40).

**The fix: gate on brightness, not on a clock.** `pipeline.py` `_process_frame` now measures the
frame's mean luma every frame (a cheap `cv2.mean` on the tiny detection frame — negligible next
to detection) and, when it's too dark, **returns before `detector.detect` even runs**. No
detection → no tracks → no phantom readings and no junk clips. The live preview still publishes,
with a `PAUSED — low light` banner so the dashboard shows *why* it's idle.

Three design choices matter:

- **Purely brightness-driven — no clock, sunset table, or GPS.** A fleet node can be anywhere, at
  any latitude, in any season; a hard-coded schedule would be wrong somewhere and drift
  everywhere. The camera already sees the only signal that matters.
- **The service stays up.** The gate pauses *detection*, not the process. Flask, the live view,
  backup sync and the heartbeat keep running — which is essential, because a fully powered-off
  node **can't detect that morning arrived.** By staying up and still measuring luma, the camera
  wakes *itself* at dawn.
- **Hysteresis + dwell so it can't flap.** It sleeps only when luma holds **below `sleep_below`
  (40)** for **`dwell_seconds` (30 s)**, and wakes only when it holds **above `wake_above` (60)**
  for the same. The 40↔60 dead-band means dusk and dawn each cross it exactly once; the dwell
  means a single passing headlight (bright) or one dark truck (dim) can't toggle the gate.

**A quiet performance side-effect.** Because detection and preview encode stop overnight, the
node stops burning cores on noise for ~8 hours a night — it idles cool instead of running the
full pipeline against a black frame. Not why the gate exists, but a real thermal/wear win that
fits this doc's theme: *don't spend compute on frames nobody can measure.*

**Observability + tuning.** Both status payloads (`/api/status` and the off-site heartbeat) now
report `paused_low_light` and the live `scene_brightness`, so both dashboards show the state and
expose the exact number to tune the thresholds against. The knobs live under `light_gate` in
`config.yaml` / `config.py` defaults (see the config-knobs list above) and, being defaults, are
active fleet-wide even on a node whose `config.yaml` predates them.

**Verification: trip it in daylight, don't wait for night.** Same discipline as the `record_fps`
and codec chapters — prove the real behavior, don't trust that the config looks right. Two levels:

- **Unit** — `tests/test_lightgate.py` (9 tests) covers the luma measurement (grayscale, colour
  channel-average, `None`) and the full state machine: sleeps only after the dwell, a brief dip
  resets the timer, the dead-band keeps it asleep, wakes only after the dwell above `wake_above`,
  disabled never pauses. (The node runs the service as `/usr/bin/python3` with no venv or pytest,
  so on-box these are validated by importing the module and calling the `test_*` functions
  directly with `PYTHONPATH=src` — cv2/numpy are system-wide.)
- **Live, in daylight** — temporarily appended `light_gate.sleep_below: 130` to the node's
  `config.local.yaml` and restarted. With the scene at luma **106**, the gate paused within the
  dwell window (`paused_low_light: true`, zero events logged), proving the end-to-end pause path;
  then reverted the overlay and restarted back to awake/running. Tonight it pauses on its own when
  luma drops under 40 (~20:45 per the table) and resumes over 60 at dawn.

**The night's junk was also swept.** The 23 dark-hours phantoms already in the log were rejected
on the node and mirrored off-site (kept out of every stat, restorable in the reject bin) — the
gate stops *new* ones; the sweep cleaned the ones already recorded before it shipped.

**The honest limitation.** This pauses *counting* at night, so a genuine nighttime speeder is not
captured at all. That's the right call — the data is unusable in the dark regardless — but if
night coverage is ever wanted, it's a **hardware** fix (an IR illuminator or a low-light/starlight
camera), not a software one. No threshold tuning recovers a signal the sensor never captured.

**Committed:** `d432df8` (`light_gate` in `pipeline.py` + `config.py`/`config.yaml`,
`paused_low_light`/`scene_brightness` in `web.py` + `remotecontrol.py`, `tests/test_lightgate.py`).

## The road-region gate (2026-08-23)

The low-light gate above kills phantoms that appear when the scene is too dark to see a car. This
chapter is the same failure in broad daylight — a motion blob that is **real, well-lit, and simply
not a car** — and it's the one that mattered most to the operator, because it produced a headline
lie.

**The symptom.** A saved clip of **two children walking on the grass in front of the camera** was
logged as a **69 mph westbound car** and flagged SPEEDING. Not a subtle few-mph error — a
kids-on-the-lawn snapshot with "69 mph SPEEDING" burned across it. The operator's verdict was
blunt and correct: *if it can do that, none of the numbers can be trusted.*

**Root cause: the speed math was applied off the road plane.** SpeedKam's speed comes from a
**homography** — a pixels→metres map fitted *only on the flat road surface* during calibration.
It is valid **on that plane and nowhere else.** The two kids were in the **near foreground**, on
the grass strip between the lens and the road — i.e. *below* the calibrated plane. Re-running the
real detector+tracker over the clip on the node made it quantitative:

| | Ground-Y in image | Calibrated road band | Verdict |
|---|---|---|---|
| The two kids (id90) | **843–863 px** | 758–825 px | **below the road — foreground** |
| A genuine car (id243) | 720–790 px | 758–825 px | on the road |

Their feet were 20–40 px *below* the road's near edge for the whole pass. Feed a point that far
off-plane into the homography and it extrapolates nonsense: their slow walk mapped to **8.9 m
traveled in 0.30 s → 111 km/h (69 mph)**, westbound because they happened to drift right-to-left.

**Why every existing false-positive gate missed it.** All three prior gates are *proxies* for
"is it a car," and two small children close to the lens defeat each one:

| Gate | Why it passed the kids |
|---|---|
| `min_vehicle_aspect` ≥ 1.1 (car boxes are wide) | The two kids intermittently **merged into one wide blob** at the live frame rate — a merged frame reads wide even though a single child reads tall (~0.5). |
| `min_vehicle_span_m` ≥ 1.0 (cars are >1 m wide) | Close to the lens they *look* big; the homography inflates their apparent ground footprint past a metre. |
| `max_track_distance_m` ≤ 45 (phantoms wander far) | 8.9 m is nowhere near 45 m — that ceiling was tuned for the old 100 m+ vegetation ghosts, not an 8.9 m foreground teleport. |

Shape and size are guessed stand-ins. The invariant they were standing in for is simpler and
un-foolable: **is the object's ground contact on the road at all?**

**The fix: gate on road location, not on blob shape.** New `Calibration.on_road_side(pts,
frame_wh, margin_frac)` returns, per ground point, whether it sits on the calibrated road surface;
`pipeline._on_road_fraction(track)` computes the fraction of a track's ground points that qualify,
and `_classify_reading` rejects the reading — *before* the shape/size checks — when that fraction
is below `min_on_road_frac` (0.6). It is a **majority vote over the whole track**, so a transient
off-road sample or a one-frame blob merge can't flip it, and it is **completely indifferent to the
blob's shape** — which is exactly why it catches what the aspect proxy couldn't.

Two design choices carry the chapter:

- **Only the near and lateral edges are bounded; the far edge is deliberately left open.**
  Calibration points get clicked across a *narrow strip* of road, but a real car legitimately
  rides "above" that strip (smaller Y) as it recedes toward the vanishing point. The failure is
  always on the **near/foreground** side (feet *below* the strip) or off to the side — never the
  far side. A symmetric margin can't express this: the distant car needs ~95 px of slack *above*
  the strip while a foreground pedestrian sits only ~18 px *below* it, so any symmetric box that
  keeps the car also admits the kids. Bounding only the near+lateral edges resolves the asymmetry.
- **Self-configuring from the calibration.** The road box is derived from each node's own
  `image_points`, so there is nothing to tune per site — a node that's calibrated already knows
  where its road is. (Uncalibrated nodes have no plane and report no speed anyway, so the gate
  simply no-ops there.)

**Verification: run it on the exact failing clip, not a synthetic one.** Same discipline as the
`record_fps` and codec chapters. The real detector+tracker were run over both clips on the node
and scored through the actual gate:

| Clip | On-road fraction | Gate verdict | Correct? |
|---|---|---|---|
| Two kids (id90) | **44 %** | reject | ✓ |
| Real car (id243) | **70 %** | keep | ✓ |

The 70 % for the car is a **pessimistic floor**: replayed offline it produced a messy 46-sample
track that absorbed background noise, whereas the live tracker logs a car as a tight ~6-sample
track that scores far higher. So 0.6 sits comfortably below real cars and well above the kids. Six
new unit tests (`tests/test_calibration.py` road-region cases + `tests/test_carfilter.py` gate
cases) plus the end-to-end clip check all pass. (As with the low-light chapter, the node has no
pytest — on-box these run by importing the modules and calling `test_*` directly; `test_calibration`
imports `pytest` at module top, so on the node a **minimal `pytest` shim** with `approx`/`raises`
is injected into `sys.modules` before import.)

**The data was also swept — carefully.** Scanning the log for counted rows above 45 mph on this
25 mph road surfaced exactly two. One was id90 (the kids), rejected on the node via `/api/reject`
and mirrored to the webhost by a single-line CSV edit. The other, id183 at 48.8 mph, turned out
on inspection to be a **real blue Jeep, squarely on the road** — a genuine speeder, and *kept*.
That is the discipline the low-light chapter also insisted on: **speed alone is never a junk
signal — always look.** Rejecting a real speeder because it's fast would be the same failure in
the opposite direction.

**The honest limitation — and why the next chapter is hardware.** This gate rejects anything
*off* the road. What it cannot do is tell a car from a **person or cyclist who is genuinely on the
road** — both have their feet on the plane, so location can't separate them, and shape (aspect) is
the fragile proxy that started this whole chapter. Truly answering "car vs person" needs
*content* classification (a neural detector), which the Pi 3 cannot run (torch/YOLO would exhaust
its ~900 MB RAM). That is the entire reason for the hardware step documented separately in the
[Pi 4 / Phase 14 plan](pi4-yolo-classifier-phase14.md): with a Pi 4 (8 GB) a per-pass YOLO
classifier becomes affordable, and it stacks *on top of* this gate — geometry says "on the road,"
the model says "it's a car."

**Committed:** `f024442` (`on_road_side`/`image_bounds` in `calibration.py`, `_on_road_fraction`
+ road-region check in `pipeline.py`, `min_on_road_frac`/`road_margin_frac` in `config.py`,
`tests/test_calibration.py` + `tests/test_carfilter.py`).

## Lessons

- **Measure the right number.** "3 fps" was the preview, not the detection loop. `/api/status`
  vs the browser refresh are different things.
- **On a thermally-bound chip, more cores can be *worse*.** Parallelism converts to throughput
  only once the heat is removed — otherwise it just trades clock for cores at constant total
  work. Cooling is the master unlock.
- **Keep cosmetic work off the measurement path.** The single biggest post-cooling win was
  moving preview copy+annotate to the encoder thread — detection had been paying for pixels
  nobody was measuring on.
- **A cheap board with a global-shutter camera + a $5 cooler beats an expensive board** for
  this workload. Spend on the camera and cooling, not the SoC.
- **Gate on the signal you actually have, not a proxy.** The low-light cutoff is measured frame
  brightness, not a sunset clock — the camera already sees the only thing that matters, so the
  gate is correct at any latitude/season and needs no configuration per site. And don't spend
  compute on frames nobody can measure: pausing detection in the dark kills phantom readings
  *and* lets the node idle cool all night.
- **Trust the invariant, not the stand-in.** The road-region gate fixed a class of false positive
  that three shape/size proxies had each waved through, because "are the feet on the road?" is a
  physical fact while "is the box wide / big / not-wandering?" is a guess about what a car looks
  like. When a proxy keeps getting fooled, find the invariant it was approximating and gate on
  that instead. And respect the asymmetry — a distant car and a foreground bystander are *not*
  symmetric about the calibrated strip, so the gate isn't either.
- **Know what the current hardware genuinely can't do.** Geometry gates (brightness, road region)
  are free and took the false-positive rate a long way, but "car vs a person standing in the road"
  needs *content* classification the Pi 3 can't host. Naming that ceiling honestly is what turned
  the next step into a scoped hardware plan ([Phase 14](pi4-yolo-classifier-phase14.md)) instead
  of another proxy that would eventually get fooled too.
