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
