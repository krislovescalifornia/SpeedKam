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

- **`detection.detect_scale`** (0.4) — detection runs on a frame scaled by this factor. Lower
  = less CPU/heat, at the cost of small/distant vehicles. 0.4 suited the Pi 3; raise toward
  0.5–1.0 on a Pi 4/5.
- **`web.stream_fps`** (10) / **`web.stream_max_width`** (640) — preview refresh cap and
  preview downscale width. Preview only; never limits detection.
- **`recording.record_fps`** (30) — frames/sec actually stored for clips, independent of
  detection. A RAM-vs-smoothness knob, **not an fps knob** — the detection loop runs at the
  sensor cap regardless of this value. It was **15** through the cooling work (to fit the
  pre-roll into a 1 GB Pi's RAM), then raised to the full 30 fps sensor rate once the node was
  cooled and had the headroom — see [Reclaiming clip quality](#reclaiming-clip-quality-2026-08-21-post-cooling).
  `0` = store every frame.
- **`recording.max_buffer_mb`** (256) — hard RAM ceiling for the clip buffer. 256 MB (~92
  720p frames) holds ~3.1 s of pre-roll at 30 fps and fits a cooled 1 GB Pi (~350 MB headroom
  measured). Was 128 during the cooling work; drop back to 128 if RAM gets tight, raise further
  on a Pi 4/5.
- **`camera.fps`** (30) — the sensor frame-duration cap. The detection loop can't exceed this;
  raising it past 30 buys little for a speed camera and adds heat.

### Clip length vs record_fps and buffer (720p, ~2.76 MB/frame)

Pre-roll seconds ≈ (`max_buffer_mb` ÷ 2.76) ÷ `record_fps`. The buffer default moved from
128 MB to 256 MB alongside `record_fps` 15 → 30, so the pre-roll length held at ~3.1 s while
the motion doubled in smoothness:

| `record_fps` | @128 MB | @256 MB (default) | Look |
|---|---|---|---|
| **30 (default)** | ~1.5 s | **~3.1 s** | smoothest |
| 15 (old default) | ~3.1 s | ~6.1 s | smooth |
| ~8 | ~5.7 s | ~11 s | slightly steppy |
| ~4 | ~11 s (full window) | full window | choppy but complete |

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

**The change (`a0bb4db`):** `record_fps` 15 → 30 and `max_buffer_mb` 128 → 256 — store clips
at the full sensor rate, and raise the buffer so the pre-roll stays ~3.1 s at the doubled rate.

**Measured before/after on the node** (edit → commit → push → `speedkam-update.service` →
re-measure):

| Metric | Before (15 fps / 128 MB) | After (30 fps / 256 MB) |
|---|---|---|
| Stored clip rate | 15 fps | **30 fps** (2× smoother motion) |
| Pre-roll length | ~3.1 s | ~3.1 s (held, by design) |
| Detection loop fps | 29.6–29.9 (one 25.4 dip) | **28.8–30.2, steady — no loss** |
| Temp / throttle | 66 °C, `0x20000` (historical) | 64–66 °C, `0x20000` — no active throttle |
| RAM used / available | 425 / 480 MB | 531 / 374 MB |
| Swap | 0 MB | 11 MB (stable, not climbing) |

The predicted trade held exactly: **clips doubled in smoothness, detection fps didn't move**,
and the extra ~100 MB of resident RAM sat comfortably within the cooled node's headroom. The
11 MB of swap that appeared is Linux evicting cold pages to fund the larger buffer + file
cache — it settled and stopped, which is eviction, not thrash (thrash climbs tens of MB while
`available` collapses; `available` held at ~374 MB). If a future feature needs the headroom
back, `max_buffer_mb 192` reclaims 64 MB while keeping `record_fps 30` (pre-roll ~2.3 s).

**Lesson:** keep the RAM-vs-media knobs mentally separate from the fps knobs. It's easy, after
a hard-won frame-rate fight, to assume every constraint you added was protecting the frame
rate — but `record_fps` and `max_buffer_mb` were only ever protecting *memory*, and cost
nothing to relax once the memory was there.

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
