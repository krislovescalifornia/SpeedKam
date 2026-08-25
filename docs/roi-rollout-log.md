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
