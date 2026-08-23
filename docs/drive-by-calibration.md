# Drive-by auto-calibration — runbook

Calibrate a SpeedKam node **without a tape measure**: drive past the camera a few
times at a known, steady speed and let the geometry fall out. This is the
`Drive-by (auto)` tab on the dashboard's **Calibrate** page
(`http://<node-ip>:8080/calibrate`), the alternative to
[manual 4-point calibration](../deploy/raspberry-pi.md#calibrate-from-your-phone-headless--recommended).

> **One-liner:** press Start, drive a few steady passes **each way**, tick the
> ones that were you, press **Calibrate from selected**. It hot-swaps the live
> calibration — no restart.

---

## When to use it (vs. manual points)

| | Drive-by (auto) | Manual points |
|---|---|---|
| Effort | Drive by a few times | Tape-measure + click 4+ road points |
| Metric precision | Good; rides on how well you hold/known the speed | Highest (targets < 0.3 m reprojection error) |
| Best for | Quick setup, no tape measure handy, a road you can drive | The most accurate speeds; when you can't drive the road |

Both write the same `calibration.json` and feed the same speed estimator — pick
whichever fits. For a *speed camera*, calibration accuracy **is** speed accuracy,
so if drive-by's reprojection error comes out high, add more passes or fall back
to manual.

---

## The one thing that trips everyone up: **you must drive BOTH directions**

The homography needs points that aren't all on a single line. One straight lane
of travel is a single line — mathematically degenerate, and the build refuses it.

SpeedKam solves this by treating **each travel direction as a lane**: a pass
going right (`→`) is lane 0, a pass going left (`←`) is lane 1. So on a normal
two-way street you get your two lanes for free — **as long as you select passes
from both directions.** Select only one-way passes and you'll get:

> *"All selected passes go the same way. Select passes from BOTH directions
> (some → and some ←) — otherwise every point lies on one line and no
> homography exists."*

---

## Step by step

1. **Set your speed.** In *Your drive-by speed*, type the speed you will actually
   drive and the units (e.g. `30 mph`). This one number is applied to **every**
   pass you select, so drive them all at the **same** steady speed.
   - **Use a phone GPS speed app, not the car's speedometer** — dashboards read a
     few % high, and the calibration is only as accurate as the number you type.
2. **Press `Start recording`.** The indicator flips from `idle` to armed and the
   node starts grabbing a thumbnail of every vehicle that crosses.
3. **Drive.** Do a few passes **each way** (up your side, back the other) at that
   steady speed, straight through the whole frame. Every car that crosses —
   yours *and* the neighbours' — lands in the list with a photo and a direction
   arrow (`→`/`←`).
4. **Tick the passes that were you.** Use the thumbnails to pick your vehicle out
   of any passing traffic. **Include some `→` and some `←`.** Aim for at least
   2–3 passes in *each* direction so a bad one averages out.
5. **Press `Calibrate from selected`.** It builds the homography from only the
   ticked passes, saves `calibration.json`, and **hot-swaps it live** — the node
   starts reporting speed immediately, no restart.
6. **Verify.** Drive one more known-speed pass and confirm the dashboard reads it
   back correctly. If it's off, add more passes (especially in the weaker
   direction) and rebuild, or switch to manual points.

`Stop` disarms recording (and stops the per-pass thumbnail cost). `Clear list`
throws away the collected passes to start fresh.

### Lane width

`Lane width` (default `3.66 m` ≈ 12 ft) only sets the **across-road** metric
scale. Speed rides on the **along-road** axis, so this barely affects measured
speed — its real job is just to keep the two directions from being collinear.
Leave it at the default unless your lanes are unusually wide/narrow.

---

## What it needs to succeed (the guardrails)

The build rejects a session with a plain-language reason if:

- **Fewer than 2 usable passes selected** — each must be a real drive-by that
  crosses the frame (≥ 3 tracked samples, and the car must sweep ≥ 40 px; a
  parked/blob "pass" is dropped).
- **All selected passes go one way** — see the both-directions rule above.
- A selected speed of 0 or negative.

Good result: a **reprojection error** in the tens of centimetres. The build
returns it; treat a large value the same as a large manual-calibration error —
re-drive and rebuild.

---

## How it works under the hood (so we don't forget)

The core identity is **distance = speed × time**. The tracker already records
each vehicle's ground-contact point in pixels at every frame
(`tracker.Sample.ground_px` + `.t`). For a pass held at a constant known speed
*v*:

- Pick one image column **`u_ref`** that every pass crosses and call the road
  cross-section under it **X = 0**. Each pass is anchored by the time `t_ref` it
  crosses `u_ref`.
- Then the along-road position of the pixel the car occupies at time *t* is
  `X = dir_sign · v · (t − t_ref)` — so a pixel maps to the same X no matter
  which pass or which *speed* produced it. **Passes at different speeds reinforce
  each other** rather than conflict.
- The across-road position is `Y = lane_index · lane_width`, where lane_index
  comes from the travel direction (`→` = 0, `←` = 1).

Every frame of every pass becomes one `pixel → metres` correspondence, and those
go straight into the same `cv2.findHomography` path as manual calibration — no
new format, no change to the speed estimator.

Code: [`src/speedkam/driveby.py`](../src/speedkam/driveby.py)
(`build_correspondences`), the `/api/driveby/*` routes and `driveby_build` in
[`src/speedkam/web.py`](../src/speedkam/web.py), and the `Drive-by (auto)` tab in
the calibrate template. Tests: [`tests/test_driveby.py`](../tests/test_driveby.py).

### API (what the UI calls)

| Route | Does |
|---|---|
| `POST /api/driveby/start` | Arm the session; begin capturing per-pass thumbnails. |
| `GET  /api/driveby/poll` | Adopt every pass finished since the last poll (all traffic). |
| `POST /api/driveby/build` | Calibrate from the selected pass ids at the given speed; saves + hot-swaps. |
| `POST /api/driveby/remove` | Drop one pass from the list. |
| `POST /api/driveby/reset` | Empty the collected-pass list. |
| `POST /api/driveby/stop` | Disarm; stop thumbnail capture. |

---

## Interaction with the YOLO gate (Phase 14 nodes)

Nothing to worry about. While the node is **uncalibrated**, a finished track is
recorded (its pixel trail — that's what drive-by consumes) and then skipped
*before* the car-vs-not gate ever runs, so drive-by operates in the pre-gate
world. The moment `Calibrate from selected` succeeds and hot-swaps the
calibration, speeds start flowing and the gate activates on the next finished
pass — a clean, automatic handoff.
