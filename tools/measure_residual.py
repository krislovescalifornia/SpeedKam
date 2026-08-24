#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Measure the residual: what does the deterministic geometry gate let through?

This is Step 2 of the gate-of-record plan ("Telling a Car From a Dog"): before
deciding whether any classifier (YOLO) is needed at all, MEASURE what a
strengthened, physics-only gate actually leaks. The answer is a number, not an
opinion.

It runs the REAL gate -- speedkam.speed.estimate() feeding
SpeedCamera._classify_reading() -- so what this harness reports is exactly what
the pipeline decides. Two modes:

  * Synthetic battery (default, no args): a labelled set of the adversaries the
    tuning journey actually hit -- wind-blown foliage, a swaying branch, a noise
    teleport, a size-flickering blob, the two-kids-on-the-lawn off-road blob, a
    pedestrian, a cyclist -- plus real cars. Deterministic, needs nothing
    installed beyond numpy. Prints, per case: kept/rejected, which gate fired,
    and every trajectory signal. The bottom line: how many JUNK cases survived
    (the residual) and how many CAR cases were wrongly rejected (false
    negatives). tests/test_residual.py asserts residual == 0 and FN == 0.

  * Real clips (--clips DIR --calibration FILE): decode event .mp4s through the
    same MotionDetector -> Tracker -> estimate -> gate the node runs, and grade
    them. Point this at a fresh daylight capture of trees/bikes/joggers (the
    adversaries we can't synthesise) to measure the TRUE field residual, and at
    the labelled car clips to confirm the gate keeps real cars. Ground truth is
    read from the filename (``..._41mph.mp4`` -> car) unless --labels overrides.

Usage:
    python tools/measure_residual.py                       # synthetic battery
    python tools/measure_residual.py --enforce proposed    # gates ON (default)
    python tools/measure_residual.py --enforce shipped     # current defaults
    python tools/measure_residual.py --clips captures_test --calibration test_calibration.json
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from speedkam import speed as speed_mod          # noqa: E402
from speedkam.pipeline import SpeedCamera        # noqa: E402
from speedkam.tracker import Sample, Track        # noqa: E402

# Speed-estimation config for the synthetic battery (matches config.DEFAULTS).
SPEED_CFG = {
    "min_track_distance_m": 3.0,
    "min_samples": 6,
    "min_speed_kmh": 3,
    "max_speed_kmh": 200,
    "direction_positive": "outbound",
    "direction_negative": "inbound",
}

# Two threshold sets. "shipped" = config.DEFAULTS today (the two motion-physics
# gates OFF). "proposed" = the completed gate of record with them ON, so the
# battery shows the strengthened geometry closing every adversary.
THRESHOLDS = {
    "shipped": {
        "max_track_distance_m": 45.0, "min_vehicle_span_m": 1.0,
        "min_vehicle_aspect": 1.1, "dedupe_seconds": 0.0,
        "min_on_road_frac": 0.6, "min_straightness": 0.80,
        "max_area_cv": 0.90, "min_monotonicity": 0.0, "max_accel_mps2": 0.0,
    },
    "proposed": {
        "max_track_distance_m": 45.0, "min_vehicle_span_m": 1.0,
        "min_vehicle_aspect": 1.1, "dedupe_seconds": 0.0,
        "min_on_road_frac": 0.6, "min_straightness": 0.80,
        "max_area_cv": 0.90, "min_monotonicity": 0.75, "max_accel_mps2": 25.0,
    },
}


class _FakeState:
    """Minimal RuntimeState stand-in so we can drive the real gate without a
    camera, recorder, or persisted state file (same pattern as the gate tests)."""

    def __init__(self, d):
        self.d = dict(d)

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


def _gate(thresholds):
    """A bare SpeedCamera wired only enough to run _classify_reading/_is_duplicate."""
    cam = SpeedCamera.__new__(SpeedCamera)
    cam.state = _FakeState(thresholds)
    cam._last_count = None
    return cam


# --------------------------------------------------------------------- battery
class Case:
    def __init__(self, name, kind, worlds, bboxes, on_road_frac, span_m, dt=0.1):
        self.name = name
        self.kind = kind                  # "car" (keep) or "junk" (reject/drop)
        self.on_road_frac = on_road_frac
        self.span_m = span_m
        self.track = Track(id=1, samples=[
            Sample(t=i * dt, ground_px=(500 + i, 500),
                   world=(float(wx), float(wy)), bbox=tuple(bb))
            for i, ((wx, wy), bb) in enumerate(zip(worlds, bboxes))
        ])


def _car_bbox(n, w=200, h=80):
    return [(0, 0, w, h)] * n


def build_battery():
    """The labelled adversary set drawn from the actual tuning journey."""
    n = 12
    # A real car: straight, constant speed (~13.4 m/s == 30 mph), wide box, on road.
    car_worlds = [(1.2 * i, 0.0) for i in range(n)]
    cases = [
        Case("real_car_30mph", "car", car_worlds, _car_bbox(n), 0.97, 2.0),
        Case("real_car_slow_15mph", "car",
             [(0.55 * i, 0.0) for i in range(n)], _car_bbox(n), 0.95, 1.8),
        # Wind-blown foliage / stitched noise: net displacement tiny vs path
        # length (low straightness).
        Case("wind_foliage", "junk",
             [(x, 0.0) for x in (0, 1.5, 0, 1.6, 0.1, 1.5, 0, 1.6, 0.1, 1.5, 0, 1.6)],
             _car_bbox(n), 0.9, 2.0),
        # A branch swaying: fairly straight axis but reverses constantly (low
        # monotonicity) -- the case straightness alone can miss.
        Case("swaying_branch", "junk",
             [(x, 0.0) for x in (0, 2, 4, 3, 5, 4, 6, 5, 7, 6, 8, 7)],
             _car_bbox(n), 0.9, 2.0),
        # Sensor-noise teleport: a huge single-frame jump (impossible accel).
        Case("noise_teleport", "junk",
             [(0, 0), (0.4, 0), (0.8, 0), (28, 0), (28.4, 0), (28.8, 0),
              (29.2, 0), (29.6, 0), (30, 0), (30.4, 0), (30.8, 0), (31.2, 0)],
             _car_bbox(n), 0.9, 2.0),
        # Size-flickering blob (noise): area varies wildly frame to frame.
        Case("flicker_blob", "junk", car_worlds,
             [(0, 0, 40, 40), (0, 0, 300, 220), (0, 0, 30, 30), (0, 0, 280, 200),
              (0, 0, 35, 35), (0, 0, 260, 190), (0, 0, 45, 45), (0, 0, 300, 210),
              (0, 0, 30, 30), (0, 0, 250, 180), (0, 0, 40, 40), (0, 0, 290, 205)],
             0.9, 2.0),
        # Two kids on the foreground lawn merged into a wide blob: car-shaped and
        # car-sized, but mostly OFF the calibrated road (the 69 mph incident).
        Case("two_kids_offroad", "junk", car_worlds, _car_bbox(n), 0.30, 2.2),
        # A pedestrian: tall, narrow box (low aspect).
        Case("pedestrian", "junk", car_worlds,
             [(0, 0, 60, 170)] * n, 0.9, 0.6),
        # A cyclist: ~square box and a sub-car footprint.
        Case("cyclist", "junk", car_worlds,
             [(0, 0, 90, 100)] * n, 0.9, 0.7),
    ]
    return cases


def evaluate(case, thresholds):
    """Run the real estimate + gate on one case. Returns (status, reason, result)
    where status is 'ok' (counted), 'rejected' (gate), or 'dropped' (no speed)."""
    cam = _gate(thresholds)
    r = speed_mod.estimate(case.track, SPEED_CFG)
    if r is None:
        return "dropped", "no speed (too short/slow/fast to time)", None
    aspect = SpeedCamera._aspect_ratio(case.track)
    area_cv = SpeedCamera._area_cv(case.track)
    status, reason = cam._classify_reading(
        r, case.span_m, aspect, case.on_road_frac, area_cv)
    return status, reason, r


def run_battery(enforce):
    thresholds = THRESHOLDS[enforce]
    cases = build_battery()
    print(f"\nSynthetic residual battery  (gate = '{enforce}')")
    print("=" * 100)
    print(f"{'case':20} {'truth':5} {'verdict':8} {'straight':>8} {'fwd':>5} "
          f"{'accel':>7} {'areaCV':>7}  gate / reason")
    print("-" * 100)
    residual = []          # junk that was COUNTED (leaked through)
    false_neg = []         # cars that were rejected/dropped
    for c in cases:
        status, reason, r = evaluate(c, thresholds)
        counted = status == "ok"
        if c.kind == "junk" and counted:
            residual.append(c.name)
        if c.kind == "car" and not counted:
            false_neg.append(c.name)
        straight = f"{r.straightness * 100:6.0f}%" if r else "     --"
        fwd = f"{r.monotonicity * 100:4.0f}%" if r else "   --"
        accel = f"{r.max_accel_mps2:7.0f}" if r else "     --"
        acv = f"{area_cv:6.2f}" if (area_cv := SpeedCamera._area_cv(c.track)) is not None else "    --"
        verdict = {"ok": "COUNT", "rejected": "reject", "dropped": "drop"}[status]
        short = reason.split(" — ")[0].split(" (")[0][:44] if reason else "counted"
        print(f"{c.name:20} {c.kind:5} {verdict:8} {straight:>8} {fwd:>5} "
              f"{accel:>7} {acv:>7}  {short}")
    print("-" * 100)
    cars = sum(1 for c in cases if c.kind == "car")
    junk = sum(1 for c in cases if c.kind == "junk")
    print(f"\n  {junk} junk cases: {len(residual)} leaked through "
          f"(residual){' -> ' + ', '.join(residual) if residual else ''}")
    print(f"  {cars} car cases:  {len(false_neg)} wrongly rejected "
          f"(false negatives){' -> ' + ', '.join(false_neg) if false_neg else ''}")
    if not residual and not false_neg:
        print("\n  RESULT: geometry-only holds the line on this battery. The "
              "documented\n          adversaries all die to physics; no classifier "
              "needed for them.")
    return residual, false_neg


# ------------------------------------------------------------------ real clips
def _truth_from_name(name):
    return "car" if re.search(r"\d+mph", name) else "unknown"


def run_clips(clips_dir, calib_path, labels_path=None, enforce="proposed",
              maxfr=200):
    import cv2
    from speedkam.calibration import Calibration
    from speedkam.detector import MotionDetector
    from speedkam.tracker import Tracker

    labels = {}
    if labels_path:
        for line in open(labels_path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                nm, _, kind = line.partition(" ")
                labels[nm.strip()] = kind.strip().lower()

    calib = Calibration.load(calib_path)
    if calib is None:
        print(f"could not load calibration {calib_path}", file=sys.stderr)
        return 2
    thresholds = THRESHOLDS[enforce]
    det_cfg = {"min_area": 1500, "max_area": 500000, "history": 400,
               "var_threshold": 40, "detect_scale": 1.0, "detect_shadows": False,
               "morph_kernel": 5, "min_hits": 3}

    clips = sorted(glob.glob(os.path.join(clips_dir, "*.mp4")))
    if not clips:
        print(f"no .mp4 clips in {clips_dir}", file=sys.stderr)
        return 2

    print(f"\nReal-clip residual  (gate = '{enforce}', calib {calib_path})")
    print("=" * 100)
    print(f"{'clip':44} {'truth':7} {'verdict':8}  reason")
    print("-" * 100)
    residual = false_neg = graded = 0
    for path in clips:
        name = os.path.basename(path)
        truth = labels.get(name) or _truth_from_name(name)
        detector = MotionDetector(det_cfg)
        tracker = Tracker({"max_match_distance": 120, "max_missed": 12}, min_hits=3)
        cam = _gate(thresholds)
        cap = cv2.VideoCapture(path)
        fr = 0
        finished_all = []
        frame_wh = None
        while fr < maxfr:
            ok, frame = cap.read()
            if not ok:
                break
            if frame_wh is None:
                frame_wh = (frame.shape[1], frame.shape[0])
            t = fr / 30.0
            dets, _ = detector.detect(frame)
            pts = [d.ground_point for d in dets]
            world = [tuple(p) for p in calib.image_to_world(pts)] if pts else []
            _, finished = tracker.update(dets, world, t)
            finished_all.extend(finished)
            fr += 1
        # Flush tracks still open at end of clip.
        for tr in tracker.tracks.values():
            if tr.confirmed:
                finished_all.append(tr)
        cap.release()

        # Grade the best (longest) confirmed track in the clip.
        best = max(finished_all, key=lambda tr: len(tr.samples), default=None)
        if best is None or frame_wh is None:
            verdict, reason = "drop", "no confirmed track"
            r = None
        else:
            r = speed_mod.estimate(best, {**SPEED_CFG, "min_samples": 4},
                                   frame_wh)
            if r is None:
                verdict, reason = "drop", "no speed"
            else:
                aspect = SpeedCamera._aspect_ratio(best)
                area_cv = SpeedCamera._area_cv(best)
                span = None
                status, reason = cam._classify_reading(r, span, aspect, None, area_cv)
                verdict = "COUNT" if status == "ok" else "reject"
        if truth != "unknown":
            graded += 1
            if truth == "car" and verdict != "COUNT":
                false_neg += 1
            if truth != "car" and verdict == "COUNT":
                residual += 1
        short = (reason.split(" — ")[0].split(" (")[0][:44]) if reason else "counted"
        print(f"{name:44} {truth:7} {verdict:8}  {short}")
    print("-" * 100)
    print(f"\n  graded {graded} labelled clips: {false_neg} cars wrongly rejected, "
          f"{residual} non-cars leaked through.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Measure the geometry-gate residual")
    ap.add_argument("--enforce", choices=list(THRESHOLDS), default="proposed",
                    help="threshold set: 'proposed' (new gates ON) or 'shipped'")
    ap.add_argument("--clips", default=None, help="dir of event .mp4s to grade")
    ap.add_argument("--calibration", default=None,
                    help="calibration.json matching the clips (real-clip mode)")
    ap.add_argument("--labels", default=None,
                    help="optional '<basename> car|junk' per line")
    ap.add_argument("--maxfr", type=int, default=200)
    args = ap.parse_args()

    if args.clips:
        if not args.calibration:
            print("--clips requires --calibration", file=sys.stderr)
            return 2
        return run_clips(args.clips, args.calibration, args.labels,
                         args.enforce, args.maxfr)

    residual, false_neg = run_battery(args.enforce)
    # Also show the shipped set for contrast when running the default.
    if args.enforce == "proposed":
        run_battery("shipped")
    return 0 if not residual and not false_neg else 1


if __name__ == "__main__":
    raise SystemExit(main())
