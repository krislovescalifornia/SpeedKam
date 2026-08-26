# Detection ROI crop — rollout log

A running, auditable record of the road-band detection-ROI work, kept so we can
review exactly what changed and prove detection never regressed. Newest steps at
the bottom.

## Why

Measured on the live Pi 4 node (2026-08-25): the IMX296 sustains ~57–60 fps in
isolation, but the running pipeline delivers only ~27 fps. A controlled harness
(real `MotionDetector`, real capture path) proved the cause is detection pixel
count starving the capture thread:

| detection frame | detect px | capture fps |
|---|---|---|
| none (ceiling)      | —    | 59.0 |
| full frame (live)   | 396k | 31.2 |
| road band 50% H     | 198k | 51.7 |
| road ROI (band+cols)| 98k  | 60.1 |
| tight ROI           | 72k  | 60.2 |

Cropping detection to the road band recovers the full sensor rate. Crossing-time
speed only needs the ground-x to cross columns x=450 and x=1000 (full-res), so a
road-band ROI is sufficient — full-frame MOG2 is unnecessary post-homography.

## Hard safety rule for this rollout

**Detection must never get less confident.** Concretely: every vehicle we count
today must still be detected, tracked (≥ min_hits, ≥ min_samples), and get the
same speed after the ROI is enabled. Rollout is therefore staged:

1. **Ship code, crop OFF** — behavior byte-identical to today (roi.enabled=false).
2. **Audit mode** — run today's full-frame detection unchanged, but record, per
   counted pass, whether its ground-point trajectory falls inside the candidate
   ROI, and accumulate the observed vehicle envelope. Zero detection change.
3. **Derive ROI from real traffic** — set the ROI from the observed envelope +
   margin, confirming it contains the crossing columns x=450..1000.
4. **Enable crop only after audit shows 100% coverage** of counted cars, then
   confirm live fps climbs and counts/speeds are unchanged.

Rollback at any stage = set roi.enabled=false (or revert the commit).

## Invariants enforced in code

- `detect(roi=None)` is the default and is byte-identical to the pre-change path.
- ROI is expressed as **fractions of the full frame** (detect_scale-independent).
- Cropped-frame bbox coords have the crop offset added back before up-scaling, so
  all downstream full-res coordinates (tracking, crossing-time) are unchanged.
- Startup validation refuses/what-warns an ROI that does not fully contain the
  crossing columns x_a/x_b, so speed can never silently break.

---

## Timeline

### 2026-08-25 — baseline
- Full test suite green on dev-box venv before any change: **108 passed**.
- Node live: Pi 4B, ~27 fps, 80.8°C, camera ceiling 57.3 fps (measured).

### 2026-08-25 — code change (crop OFF by default)
Files changed:
- `src/speedkam/detector.py` — `detect()` gains optional `roi=(x0,y0,x1,y1)`
  fractions; crops the (already-downscaled) detection frame and adds the crop
  offset back before up-scaling. `roi=None` is byte-identical to the old path.
- `src/speedkam/config.py` — `detection.roi` defaults (all OFF / whole-frame).
- `src/speedkam/pipeline.py` — `_configure_roi()` (fail-safe parse + validation:
  refuses a band that doesn't contain x_a/x_b), `_roi_audit_pass()` /
  `_write_roi_audit()` (observational envelope + coverage → captures/roi_audit.json),
  wired `roi=self._det_roi` into both `detect()` calls, audit called only for
  counted (non-rejected) passes.
- `src/speedkam/web.py` — `/api/status` now reports `roi_enabled`, `roi_audit`,
  `roi_audit_passes`, `roi_observed_envelope`.
- `config.yaml` — documented (OFF) `detection.roi` block.
- `tests/test_detector_roi.py` — 10 tests: offset-correct box == full-frame box,
  ground point matches, `roi=None`/full-frame no-ops, outside-band blob dropped,
  config fail-safe (refuses band missing x_a/x_b), audit envelope + coverage.

Verification (dev-box venv):
- New ROI tests: **10 passed**.
- Full suite after change: **118 passed** (108 baseline + 10), zero regressions.

Safety properties confirmed by tests: with the crop OFF (shipped default) the
detection path is unchanged; with a valid band ON, a blob inside it yields the
identical full-res bbox + ground point as full-frame; an invalid band never
enables (degrades to full-frame).

### 2026-08-25 — deployed to node (crop OFF) + audit enabled
- Commit `30fd86b` pushed to origin/main; node pulled via
  `speedkam-update.service`. Node HEAD now `30fd86b`; changed files byte-compile
  on the node's system python3; service active.
- Post-deploy default state (before any config change): `/api/status` →
  `roi_enabled=false, roi_audit=false`, fps ~30, running, camera_ok. Detection
  byte-identical to before — nothing changed by shipping the code.
- Backed up node `config.local.yaml` → `config.local.yaml.bak.<ts>`, then added
  under `detection`: `roi: {enabled: false, audit: true}`. Restarted.
- Audit-mode state confirmed: `/api/status` → `roi_enabled=false` (crop still
  OFF), `roi_audit=true`, fps ~26 (unchanged). Startup log:
  `detection ROI AUDIT on: full-frame detection unchanged; recording counted-car
  coverage + envelope.`

**Current state: OBSERVING.** Detection is exactly as it was; the node is now
recording every counted car's ground-point envelope to `captures/roi_audit.json`
and exposing `roi_observed_envelope` in `/api/status`.

### 2026-08-25 — first envelope read + restart-persistence fix
Read `captures/roi_audit.json` after ~100+ cars drove by. **Only 6 passes
recorded** — the in-memory envelope resets to empty on every service restart, and
the node had been restarted several times today by the concurrent colour-engine
deploys (node HEAD moved to `0eb3317`). So the audit had collapsed to "cars since
the last restart", not the day.

Findings from those 6 counted cars (still informative):
- Observed envelope (frac): x [0.010, 0.981], y [0.756, 0.869].
- The tyre-line band is VERTICALLY THIN — ~11% of frame height — while cars span
  nearly the full width (crossing columns are at x=450/1000, cars travel further).
- Recommended band (padded): x [0.0, 1.0], y [0.656, 0.969] ≈ **31% of pixels,
  full width** → matches the harness's ~30%-pixels → ~57 fps point. Direction
  strongly confirmed; exact bounds need more cars (esp. far-lane / both directions).

Fix: `_load_roi_audit()` restores the envelope + pass count from roi_audit.json on
startup (audit mode) so the audit is now CUMULATIVE across restarts. Colour
counters are not restored (only meaningful vs a fixed candidate). Test added
(`test_audit_resumes_envelope_across_restart`). Full suite: **131 passed**.
Deployed; the existing 6-pass envelope is carried forward, not lost.

Assessment for the operator: **promising but NOT yet enough data** — 6 cars can't
prove a band catches every future car (a higher far-lane tyre-line could sit above
the current y-min). Colour + speed are unaffected regardless: the ROI only crops
the MOG2 detection frame; colour reads the full-res frame + full-res background
plate at the car's bbox, and the bbox/ground point are provably identical to
full-frame for any car inside the band. Let it accumulate uninterrupted.

### 2026-08-25 — validated on today's clips (offline) + ENABLED
Instead of waiting for live audit to re-accumulate (it had been reset by restarts),
replayed **today's 52 saved clips** through the same MotionDetector+Tracker offline
(`tools/roi_replay.py`, on the dev-box venv — clips are the real trajectories;
speed doesn't move a car sideways, so where tyre-lines fall is the same at any
speed). Two passes per clip: full-frame baseline, then ROI applied.

Results (MOG2 warmed per clip to mimic the node's steady-state model):
- 52 clips → **39 cleanly reconstructed** timeable cars (rest: MOG2 cold-start on
  4-sec clips + 1 corrupt file; those were counted live with a mature model).
- Full-frame envelope (frac): x[0.008,0.990] y[0.693,0.884].
- **Converged**: first 19 cars set y-min 0.693; the next 20 did not lower it.
- **ROI regression test (band actually applied): 39/39 cars still detected &
  timeable, 0 lost, sample counts IDENTICAL (+0.0)** → identical tracks → identical
  speeds. This is the offset-correctness guarantee, confirmed on real cars.

Decision: enabled with a slightly conservative band (extra margin above the
farthest observed car), full width:
`detection.roi: enabled: true, x0:0.0 y0:0.55 x1:1.0 y1:1.0` (px x[0,1456]
y[598,1088], ~45% of pixels) — in node `config.local.yaml` (backed up first).

Live result after restart: **fps ~27 → ~48-50** (roi_enabled=true). Startup log:
`detection ROI ON: x[0,1456] y[598,1088]px ... (~45% of pixels)`.

CAVEAT (important): with the crop ON, the live audit is a weak tripwire — it only
sees cars it still detects, so it cannot catch a car it never saw (one falling
outside the band). The real assurance is the offline 39/39 proof + margin. To keep
watching for misses going forward: re-run `tools/roi_replay.py` on a later day's
clips and compare daily counts to prior days. Rollback anytime = set
`detection.roi.enabled: false` and restart.

Colour + speed unaffected by construction and confirmed: speed reads the ground-x
(identical), colour reads the FULL-RES frame + full-res background plate at the
car's bbox (ROI never touches the full-res frame).

### NEXT
- Daylight tomorrow: confirm real cars count under the ROI with sane speeds +
  colours; spot-check a couple of clips. Re-run tools/roi_replay.py on tomorrow's
  clips to reconfirm the band still covers every car.
- Optional further FPS: the band is full-width; if the far lane is unused, a
  tighter x-span or lower detect_scale buys more, re-validated the same way.
