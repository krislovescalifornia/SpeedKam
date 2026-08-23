# Phase 14 — Pi 4 + on-node YOLO car-vs-person classifier (kickoff plan)

**Status:** planned, not started. This doc is a self-contained brief so a future session can
execute it without re-deriving the context.

**One-line goal:** put a *real* object classifier (car vs person/bicycle/animal) on the node so
false positives are caught by *what the object is*, not just *where it is* — which the Pi 3 could
never host, but a Pi 4 (8 GB) can.

---

## Why this exists

The [road-region gate](pi3-performance-tuning.md#the-road-region-gate-2026-08-23) (Phase 13,
commit `f024442`) rejects anything whose ground contact is **off the calibrated road** — foreground
bystanders, roadside motion, near-lens vegetation. That decisively killed the "two kids on the lawn
logged as 69 mph" class of failure.

What geometry **cannot** do is separate a car from a **person or cyclist who is genuinely on the
road**: both have their feet on the calibrated plane, so location can't tell them apart, and the
`min_vehicle_aspect` shape proxy is fragile (a walking pair merges into a car-wide blob; a
close/occluded person reads wide). The only robust separator left is **content classification** — a
neural detector that actually recognizes a car vs a person. That needs a model the Pi 3 can't run.

## Why the Pi 3 can't, and the Pi 4 (8 GB) can

- **Pi 3 Model B:** ~906 MB RAM, and in practice ~250 MB free with SpeedKam running (already dipping
  into swap). `torch` + `ultralytics` need ~1–2 GB resident just to import — it would **OOM the
  node**, not merely run slow. This is *why* `recognition.defer: true` and why every gate to date is
  geometry-only. (See the "Red herrings" note in the tuning doc — YOLO was never actually running on
  the Pi 3.)
- **Pi 4, 8 GB:** RAM ceiling gone. The model imports and runs comfortably.
- **The right cadence is per-*pass*, not per-frame.** We never need 14–20 inferences/second. We need
  **one classification per vehicle**, run in the finalize step when a track ends — a handful per
  minute on a residential street. Rough CPU cost on a Pi 4 (no accelerator):

  | Model / input size | ~time per crop |
  |---|---|
  | YOLOv8n @ 320 px, torch CPU | ~200 ms |
  | YOLOv8n @ 320 px, NCNN export | ~100–150 ms |
  | YOLOv8n @ 640 px, torch CPU | ~450–700 ms |

  At ~200 ms once per pass, run on a **worker thread**, it never touches the detection loop's frame
  rate.

## Decisions already locked (with the operator)

1. **Pi 4 base = fresh 64-bit install + port config** (not the existing golden image — see below).
   64-bit is mandatory: `torch` only ships **aarch64** ARM wheels; a 32-bit OS cannot install it.
2. **Sequencing: road gate now (done, `f024442`), YOLO after the Pi 4 is up.**
3. **Keep BOTH gates.** Geometry says "on the road," the model says "it's a car." Belt and
   suspenders — an object must pass both to be counted. The classifier is *additive*, never a
   replacement for the road-region/brightness gates (which also cover cases a tiny/blurry crop makes
   the model unsure about).

## Do we need a new golden image? (the operator's question)

Short answer: **not for this upgrade.**

- **To get the road-region fix onto any node:** No new image. Every golden-image node runs the
  `speedkam-update.timer` (git pull on boot + daily), so a freshly flashed node pulls `f024442`
  automatically on first network boot. The current image is fine for the *code*.
- **For the Pi 4 with on-node YOLO:** We are **not using the golden image at all** — the plan is a
  fresh 64-bit install. The current golden image (`C:\Users\Kris\speedkam-golden.img.xz`) was built
  on a **Pi 3B+** and is almost certainly **32-bit** (confirm with `uname -m` → `armv7l` = 32-bit,
  `aarch64` = 64-bit, or `dpkg --print-architecture` → `armhf` vs `arm64`). Even though a Pi 4 will
  boot a 32-bit image, torch/YOLO can't install on it. So don't rebuild the golden image for this;
  do the fresh 64-bit install and port the config.
- **Future fleet step (not required now):** once the Pi 4 + 64-bit + YOLO node is proven, it's worth
  **building a new 64-bit golden image** so the whole fleet can be "image-and-go" *with*
  classification. That's a follow-on to this phase, tracked at the end of this doc.

## Node bring-up checklist (fresh Pi 4, 8 GB)

1. **Flash 64-bit Raspberry Pi OS (Bookworm, Lite, arm64)** to the Pi 4's card.
2. **Provision SpeedKam** the online way (`deploy/provision-node.sh`) — clones the repo, installs
   the service + systemd units (`speedkam`, `speedkam-update.timer`, `speedkam-netcfg`, power
   sudoers). Confirm `git`, `python3`, `libcamera`/`picamera2`, and OpenCV import cleanly.
3. **Port config from the Pi 3** (do NOT re-derive):
   - `calibration.json` — the road homography is camera-mounting-specific. If the camera/mount is
     unchanged, copy it verbatim; if remounted, re-calibrate on-site.
   - `config.local.yaml` — real `backup.url` + secret, any per-node overrides (width/height/
     `detect_scale`, Wi-Fi, etc.).
   - `captures/runtime.json` — persisted live settings (speed limit, SpeedKapture threshold,
     orientation, reject knobs). Optional but keeps continuity.
   - Consider copying `captures/events.csv` if you want history continuity; the node self-IDs by CPU
     serial, so the **fleet bucket on speedkam.com changes** (new board = new serial) unless you
     carry data over. Decide: fresh start vs. migrate the old bucket.
4. **Install the ML stack:** `pip install ultralytics` (pulls torch CPU aarch64). Pre-fetch
   `yolov8n.pt` (~6 MB). Optionally export to NCNN for ~2× speed. Verify import + a single-image
   inference works and note the per-image latency at imgsz 320.
5. **Raise the detection budget now that it's a Pi 4:** bump `detection.detect_scale` back up
   (0.3 → 0.4–0.5) and confirm the loop holds a healthy fps cooler than the Pi 3 — higher
   detect_scale + fps also *reduces the blob-merge artifacts* that fooled the aspect gate.
6. **Enable recognition** (`recognition.enabled: true`, `defer: false`) and wire it as a gate (code
   task below).

## Implementation tasks (code)

The scaffolding already exists — `src/speedkam/recognition.py` loads `yolov8n` and reads COCO
classes; today it only harvests **vehicle** classes (car/truck/bus/motorcycle) for the *type*
attribute and is invoked purely as best-effort enrichment inside `pipeline._recognize`.

1. **Add a classification *verdict* to the recognizer.** Extend `VehicleRecognizer` to return, for a
   crop, the best COCO class among a relevant set — vehicles **and** the non-vehicle nuisances:
   `person` (0), `bicycle` (1), plus animals if desired (`dog` 16, `cat` 15). Return
   `(is_vehicle: bool, label, confidence)`, not just the vehicle label.
2. **Wire it as a reject gate in `pipeline._finalize`/`_classify_reading`** — run *once per finished
   track* on a representative mid-pass crop (reuse `_recognize`'s frame/bbox selection):
   - Non-vehicle with confidence ≥ threshold (e.g. person/bicycle) → **reject** with a clear reason
     ("classified as a person (0.82)"), logged to the reject bin like the other gates.
   - Vehicle → keep (and fill the existing `vehicle_type` attribute for free).
   - Low-confidence / nothing → **do not hard-reject**; fall back to the geometry gates (avoid
     dropping real cars the model was unsure about on a tiny/blurry crop). Make this fallback policy
     a config knob.
3. **Run inference off the hot path.** At ~200 ms/pass a synchronous call in `_finalize` (which runs
   on the process/detect thread in the parallel pipeline) would stall detection for that window on
   back-to-back traffic. Prefer a **classification worker thread + queue**: `_finalize` enqueues the
   crop; the counted/rejected status is finalized when the verdict returns (or make the count
   provisional and correct it — simpler is a short bounded queue and accepting a small per-pass
   stall on a quiet street; decide during implementation).
4. **Config:** a `recognition` gate section — enable flag, `min_confidence` for rejection, the
   nuisance class list, and the low-confidence fallback policy. Keep `defer: true` valid for Pi 3
   nodes (they stay geometry-only).
5. **Tests:** stub the recognizer to return canned verdicts; assert a `person` verdict rejects and a
   `car` verdict keeps, plus the low-confidence fallback path. Follow the existing
   `tests/test_carfilter.py` style (constructs `SpeedCamera.__new__` + `FakeState`).

## Validation plan

- **Unit** — as above, stubbed verdicts through `_classify_reading`.
- **On the real clips** — the same two clips used for the road gate are the perfect regression set:
  the **kids clip (id90)** should now be rejected by *both* the road gate (44 % on-road) *and* the
  classifier (`person`); the **car clip (id243)** kept by both. Re-run the offline harness on the
  node (it has cv2/numpy; use the pytest shim trick for pytest-importing modules).
- **Live** — watch a real pedestrian/cyclist pass *on the road* get rejected (the case the Pi 3
  couldn't catch), and confirm real cars still count and get a `vehicle_type`. Confirm the detection
  loop fps is unaffected (worker thread) and thermals are healthy on the Pi 4.

## Risks / caveats to keep honest

- **Tiny/distant/blurry crops** still challenge yolov8n; that's why the low-confidence fallback must
  not hard-reject.
- **Latency placement** is the main design risk — get the worker-thread/queue right so classification
  never throttles detection under back-to-back traffic.
- **64-bit is non-negotiable** for torch; a 32-bit base silently blocks the whole phase.
- **Fleet bucket identity** changes with a new board (CPU-serial self-ID) — decide up front whether
  to migrate history or start fresh, so off-site stats aren't unexpectedly split.

## Follow-on (after this phase proves out)

- **Build a new 64-bit golden image** from the working Pi 4 node so the fleet can be image-and-go
  with classification (mirrors the Phase 5 golden-image process, but arm64 and with the ML stack
  baked or auto-installed on first boot). Weigh baking torch/ultralytics into the image (bigger
  image, instant) vs. a first-boot `pip install` (smaller image, needs network at first boot).
- Consider NCNN/quantized export fleet-wide if per-pass latency matters at scale.
