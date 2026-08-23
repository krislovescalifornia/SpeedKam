# Phase 14 — Pi 4 rebuild: YOLO as the gate of record (execution plan)

**Status:** planned, ready to execute in a fresh session once the Pi 4 is booted.
**Read first:** [`pi4-yolo-classifier-phase14.md`](pi4-yolo-classifier-phase14.md) — the
kickoff brief. It has the hardware rationale (why Pi 3 OOMs, why 64-bit is mandatory), the
per-pass cadence math, the node bring-up checklist, and the RAM/latency tables. **All of that
still stands and is not repeated here.** This doc records what *changed* after we validated the
approach on real footage, and the concrete tasks to run.

---

## What changed since the kickoff brief

The kickoff brief framed the classifier as **additive** to the geometry gates ("keep BOTH gates,
the classifier is additive, never a replacement"). Field data forced a stronger position:

- **The geometry gates are what failed.** On 2026-08-23 the Pi 3 logged blank-road frames as
  **92 mph and 93 mph cars**, with confidence, and these sailed past *every* proxy (distance
  29–42 m < 45 m plausible; on the road plane; daylight; car-like blob). No geometric gate can
  reject them without also rejecting real cars — they are geometrically indistinguishable from a
  car. A phantom that looks car-shaped is the ceiling of the proxy approach.
- **The contamination was worse than the 90+ clips.** Auditing the *kept* ("ok") pile turned up a
  clip labeled **37 mph car** that is also an empty road. The proxies never flagged it. So the
  "150 ok events" were not clean either.
- **Decision (operator, 2026-08-23): all Pi 3 data is garbage. Full stop, start over.** The Pi 3
  is unplugged and retired. We do not migrate its `events.csv` or its fleet bucket.

**Therefore YOLO is promoted from "additive enrichment" to the gate of record for car-vs-not.**
Geometry does not get to *keep* an event the model says has no vehicle. This is the one decision
that overrides the kickoff brief's "belt and suspenders / additive only" stance. The gates still
exist, but with new roles (below).

## Validation evidence (why we believe this works)

Ran YOLOv8n (`ultralytics` 8.4, torch CPU) against 7 real clips pulled from the off-site backup —
the 2 blank 90+ phantoms plus 5 clips the Pi 3 had labeled "ok". Harness saved at
[`tools/yolo_validate.py`](../tools/yolo_validate.py). Result:

| Clip (Pi 3 label) | YOLO (53 frames each) | Correct? |
|---|---|---|
| id195 — 92 mph "car" | 0 vehicle frames | ✅ phantom rejected |
| id283 — 93 mph "car" | 0 vehicle frames | ✅ phantom rejected |
| id305 — 37 mph "car" | 0 vehicle frames | ✅ phantom (was in *keep* pile) |
| id243 — 33 mph car | car @ 0.94, 45/53 frames | ✅ real car kept |
| id121 — 34 mph car | car @ 0.94, 37/53 frames | ✅ real car kept |
| id232 — 35 mph car | car @ 0.95, 38/53 frames | ✅ real car kept |
| id353 — 34 mph car | car @ 0.93, 33/53 frames | ✅ real car kept |

**7/7 correct on ground truth.** Real cars detected in the large majority of frames at 0.93–0.95;
phantoms produced *zero* detections in *any* frame. This ran on the dev-box CPU at imgsz 640 — it
proves the **discriminator is correct**. It does **not** yet prove **latency on the Pi 4** or
**night** (see Open questions). Those get measured on hardware, not assumed.

## Refined architecture — roles after the pivot

- **Motion tracker → trigger only.** It is good at "something crossed here, at this box, moving
  this fast." It no longer decides *what* the thing is.
- **YOLO → the decider (multi-frame vote, not a single crop).** The kickoff brief proposed one
  classification on a representative mid-pass crop. The validation shows a **vote across frames** is
  far more robust and nearly free: sample ~8–10 frames spanning the track, count vehicle detections,
  and require **≥ K vehicle-frames** (start K=2) to keep. Real cars scored 33–45/53; phantoms 0/53
  — the separation is enormous, so the vote is safe and forgiving of a single missed frame.
- **Geometry gates → demoted, still useful in three narrow roles** (this is the reconciliation with
  the kickoff brief's "keep both"):
  1. **Cheap pre-filter** — skip YOLO on frames that are obviously dark (low-light gate) or whose
     blob is off-road (road-region gate). Saves inference cost; never *keeps* anything on its own.
  2. **Speed sanity** — the distance/plausibility check still catches a broken homography producing
     a 6634 m track; keep it as a sanity clamp on the *speed*, independent of car-vs-not.
  3. **Low-confidence fallback** — if YOLO is genuinely unsure on a tiny/distant/blurry crop
     (below `min_confidence`, but nonzero), do **not** hard-reject; defer to geometry. A clip with
     **zero** detections across the whole pass in good light is a hard reject regardless of geometry
     — that is the case the proxies could never catch.
- **Speed math + calibration → unchanged.** They are correct *given a real car*; that was never the
  bug.

## Rebuild the validation discipline (the real lesson)

The Pi 3 shipped four gates, each "DEPLOYED + VERIFIED LIVE," while the actual disease — nothing
ever checked for a car — went unaddressed. The discipline fix:

- **Maintain a labeled regression set** of real clips, seeded with the exact phantoms that
  embarrassed us. Known ground truth as of now (re-pullable from the off-site backup, node serial
  `000000002d47790c`, `media/2026-08-23/`):
  - **Phantoms (must reject):** `20260823_125927_id195_92mph.mp4`,
    `20260823_132833_id283_93mph.mp4`, `20260823_132945_id305_37mph.mp4`
  - **Real cars (must keep):** `20260823_111315_id243_33mph.mp4`,
    `20260823_105419_id121_34mph.mp4`, `20260823_130451_id232_35mph.mp4`,
    `20260823_134356_id353_34mph.mp4`
- **No "verified live" without proving empty-frame rejection** against that set first, via
  `tools/yolo_validate.py` (or the on-node equivalent). A change to recognition that does not run the
  regression set is not done.
- Grow the set with new field failures as they appear (especially **on-road pedestrians/cyclists**
  — the case geometry provably cannot separate — and **night**).

## Execution task list

Do the [kickoff brief's bring-up checklist](pi4-yolo-classifier-phase14.md#node-bring-up-checklist-fresh-pi-4-8-gb)
first (fresh arm64 install, provision, port `calibration.json` + `config.local.yaml`, install
ultralytics, pre-fetch `yolov8n.pt`). Then:

1. **Measure real Pi 4 latency FIRST** (gates every downstream decision). Time a single inference at
   imgsz 320 and 640, torch CPU, then again after NCNN export. Record the numbers. Pick imgsz +
   backend so a ~8–10-frame vote per pass fits comfortably on a worker thread. (Kickoff table
   estimates ~200 ms @ 320 torch; verify, don't trust.)
2. **Extend `VehicleRecognizer`** (`src/speedkam/recognition.py`) to return a per-crop verdict over
   vehicles **and** nuisances (`person` 0, `bicycle` 1, optionally `dog` 16 / `cat` 15):
   `(is_vehicle, label, confidence)`. (Same as kickoff task 1.)
3. **Add a pass-level vote** helper: given a finished track, sample N frames across it, run the
   recognizer on each, return vehicle-frame count + best label/conf. This is the new decider.
4. **Wire it as the gate in `pipeline._finalize` / `_classify_reading`:**
   - vehicle-frames ≥ K and best conf ≥ `min_confidence` → **keep**, fill `vehicle_type`.
   - nonzero but below vote/confidence → **low-confidence fallback** to geometry (config knob).
   - **zero vehicle-frames in good light → hard reject**, reason e.g. `"no vehicle detected in N
     frames — phantom"`, logged to the reject bin.
5. **Run inference off the hot path** — worker thread + bounded queue, per kickoff task 3. With a
   multi-frame vote the per-pass cost is N×latency, so the worker thread matters more than before;
   confirm it never throttles the detection loop under back-to-back traffic.
6. **Config** — a `recognition` gate section: `enabled`, `min_confidence`, `vote_frames` (N),
   `min_vehicle_frames` (K), nuisance class list, low-confidence fallback policy. Keep `defer: true`
   valid so any legacy Pi 3 node stays geometry-only.
7. **Tests** — stub the recognizer with canned per-frame verdicts; assert: all-vehicle → keep,
   all-person → reject, **zero-detection → reject** (the phantom case), and low-confidence →
   fallback. Follow `tests/test_carfilter.py` style. Node has no venv/pytest — run test fns directly
   with `PYTHONPATH=src` (see memory `speedkam-primetime-roadmap`).
8. **Regression gate** — run `tools/yolo_validate.py` on the 7 labeled clips (pull them to the node
   or run on the dev box); require 7/7 before declaring the gate live.
9. **Live confirmation** — watch a real car count and get a `vehicle_type`; confirm detection-loop
   fps and Pi 4 thermals are unaffected; confirm no phantom appears on an empty street over a
   daylight window.

## Data / fleet identity

- **Start fresh.** New board = new CPU serial = new fleet bucket on speedkam.com automatically. Do
  **not** migrate the contaminated Pi 3 bucket (`000000002d47790c`). Its clips are still available
  read-only for the regression set, but its stats are garbage and stay retired.
- The dashboards, receiver, and fleet aggregate already key on CPU serial, so the new node appears
  as a clean node with no code change.

## Open questions to resolve on hardware (be honest, measure)

- **Pi 4 per-pass latency** at the chosen imgsz/backend (task 1). Everything else assumes this fits.
- **Night.** YOLO degrades in the dark; the low-light gate already pauses detection when it is too
  dark to trust. Expect *reduced night coverage*, not night phantoms — state that plainly rather
  than letting it invent 2 a.m. cars again.
- **Tiny/distant/blurry crops** — the low-confidence fallback (task 4) exists precisely so the model
  being unsure on a far crop does not drop a real car.

## Follow-on (unchanged from kickoff brief)

Once the Pi 4 + YOLO node is proven, build a **new arm64 golden image** so the fleet is
image-and-go with classification, and weigh baking torch/ultralytics in vs. first-boot `pip
install`. See the kickoff brief's "Follow-on" section.
