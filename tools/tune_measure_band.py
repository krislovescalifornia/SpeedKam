#!/usr/bin/env python3
"""Tune the measurement gate against a video of known speed.

It replays a video through the real detect -> track -> homography chain, takes
the vehicle's track, then sweeps candidate bands and reports the recovered speed
for each versus the known ground truth. It finds the most INCLUSIVE band (the
longest, most robust baseline) that still hits the error target.

The axis to sweep depends on how the camera is mounted:
  * parallel (side-on): the lens distorts the LEFT/RIGHT edges, so sweep a
    horizontal band symmetric about centre (x).
  * head_on (receding): perspective collapses pixels-per-metre toward the
    vanishing point, so far cars are unreliable -- sweep a near/mid VERTICAL
    band (y) that drops the far top of frame.
The axis is taken from the clip's meta ("orientation"), or forced with --axis.

Usage:
  python tools/make_sideon_video.py                            # side-on clip
  python tools/tune_measure_band.py                            # tunes parallel (x)

  python tools/make_headon_video.py                            # head-on clip
  python tools/tune_measure_band.py --meta test_headon.meta.json   # tunes head_on (y)

  # or your own clip of a car at a known speed:
  python tools/tune_measure_band.py --video clip.mp4 \
      --calibration calibration.json --truth-mph 30 --axis y
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from speedkam import speed as speed_mod          # noqa: E402
from speedkam.calibration import Calibration     # noqa: E402
from speedkam.capture import Camera              # noqa: E402
from speedkam.config import load_config          # noqa: E402
from speedkam.detector import MotionDetector     # noqa: E402
from speedkam.tracker import Tracker             # noqa: E402

MPH_PER_MS = 2.2369362920544

# Detection tuned for the synthetic clip (a small, high-contrast car). Override
# via --config if you're tuning against real footage with a real detector setup.
DET_OVERRIDES = {"min_area": 800, "history": 500, "min_hits": 2}

# A band must retain at least this many samples to be trusted. Perspective makes
# far-field readings noisy, so a lucky handful of samples can score well by
# chance; requiring a real baseline keeps the recommendation robust.
MIN_BAND_SAMPLES = 15

# Near cap for vertical (head_on) sweeps: keep [y_min, YCAP], drop the far top by
# raising y_min. YCAP < 1 also trims the very-near bottom where a car is clipped.
YCAP = 0.95


def collect_track(cfg, video, calibration):
    """Replay the video and return the longest confirmed track's samples."""
    cam_cfg = dict(cfg["camera"])
    cam_cfg.update({"backend": "opencv", "source": video, "loop": False})
    cam = Camera(cam_cfg)
    detector = MotionDetector({**cfg["detection"], **DET_OVERRIDES})
    tracker = Tracker(cfg["tracker"], min_hits={**cfg["detection"], **DET_OVERRIDES}["min_hits"])

    finished_all = []
    frame_wh = (cfg["camera"]["width"], cfg["camera"]["height"])
    try:
        while True:
            t, frame = cam.read()
            if frame is None:
                break
            frame_wh = (frame.shape[1], frame.shape[0])
            detections, _ = detector.detect(frame)
            pts = [d.ground_point for d in detections]
            world = ([tuple(p) for p in calibration.image_to_world(pts)]
                     if pts else [])
            _, finished = tracker.update(detections, world, t)
            finished_all.extend(finished)
    finally:
        cam.release()
    # Flush any track still open at end-of-file.
    finished_all.extend(tr for tr in tracker.tracks.values() if tr.confirmed)
    if not finished_all:
        raise SystemExit("No vehicle track found -- check detection/calibration.")
    return max(finished_all, key=lambda tr: len(tr.samples)), frame_wh


def eval_band(track, cfg_speed, frame_wh, band):
    """Return (speed_mph, n_samples) for a flat band, or (None, 0)."""
    r = speed_mod.estimate(track, {**cfg_speed, "measure_band": band}, frame_wh)
    return (None, 0) if r is None else (r.speed_mph, r.n_samples)


def candidates(axis):
    """Yield (label, span, band) sweep candidates for the given axis.

    span = the fraction of the frame the band spans along the swept axis; larger
    is a longer baseline, so among bands that hit the target we prefer the
    largest span.
    """
    if axis == "x":
        for hw in [0.50, 0.45, 0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10]:
            band = {"enabled": True, "x_min": round(0.5 - hw, 3),
                    "x_max": round(0.5 + hw, 3), "y_min": 0.0, "y_max": 1.0}
            label = "full-frame" if hw >= 0.5 else f"x +/-{hw:.2f}"
            yield label, round(2 * hw, 3), band
    else:  # y: keep [y_min, YCAP], raise y_min to drop the far/top region
        for y_min in [0.0, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65]:
            band = {"enabled": True, "x_min": 0.0, "x_max": 1.0,
                    "y_min": y_min, "y_max": YCAP}
            label = "full-frame" if y_min <= 0.0 else f"y {y_min:.2f}-{YCAP:.2f}"
            yield label, round(YCAP - y_min, 3), band


def position_profile(track, frame_wh, axis, truth):
    """Print instantaneous speed bucketed by position along the swept axis."""
    idx = 0 if axis == "x" else 1
    dim = frame_wh[idx]
    ss = [s for s in track.samples if s.world is not None]
    rows = []
    for a, b in zip(ss, ss[1:]):
        dt = b.t - a.t
        if dt <= 0:
            continue
        d = ((b.world[0] - a.world[0]) ** 2 + (b.world[1] - a.world[1]) ** 2) ** 0.5
        pos = (a.ground_px[idx] + b.ground_px[idx]) / 2 / dim
        rows.append((pos, d / dt * MPH_PER_MS))
    near_far = "x = fraction of width" if axis == "x" else \
        "y = fraction of height (low y = FAR/near vanishing point)"
    print(f"Instantaneous speed by position ({near_far}):")
    for lo in [i / 10 for i in range(10)]:
        vals = [v for (p, v) in rows if lo <= p < lo + 0.1]
        if vals:
            m = sum(vals) / len(vals)
            err = (m - truth) / truth * 100
            print(f"  {lo:.1f}-{lo+0.1:.1f}: {m:6.1f} mph  ({err:+6.1f}%)  "
                  f"{'#' * min(40, int(abs(err)))}")
    print()


def main():
    ap = argparse.ArgumentParser(description="Tune the measurement gate")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--meta", default="test_sideon.meta.json",
                    help="clip meta json (video/calibration/truth/orientation)")
    ap.add_argument("--video", help="video file (default: from --meta)")
    ap.add_argument("--calibration", help="calibration json (default: from --meta)")
    ap.add_argument("--truth-mph", type=float, help="ground-truth speed (default: from --meta)")
    ap.add_argument("--axis", choices=["x", "y", "auto"], default="auto",
                    help="sweep horizontal (x/parallel) or vertical (y/head_on)")
    ap.add_argument("--target", type=float, default=2.0,
                    help="max acceptable speed error, percent (default 2.0)")
    args = ap.parse_args()

    meta = {}
    if Path(args.meta).exists():
        meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))

    video = args.video or meta.get("video")
    calib_path = args.calibration or meta.get("calibration")
    truth = args.truth_mph if args.truth_mph is not None else meta.get("truth_mph")
    if not (video and calib_path and truth):
        raise SystemExit("Need --video, --calibration and --truth-mph "
                         "(or a --meta json from make_*_video.py).")

    axis = args.axis
    if axis == "auto":
        axis = "y" if speed_mod.normalize_orientation(
            meta.get("orientation")) == "head_on" else "x"

    cfg = load_config(args.config)
    calibration = Calibration.load(calib_path)
    if calibration is None:
        raise SystemExit(f"Could not load calibration {calib_path!r}")

    track, frame_wh = collect_track(cfg, video, calibration)
    orient = "head_on (vertical band)" if axis == "y" else "parallel (horizontal band)"
    print(f"Track #{track.id}: {len(track.samples)} samples.  Mounting: {orient}.")
    print(f"Ground truth: {truth:.1f} mph   (target error <= {args.target:.1f}%)\n")

    position_profile(track, frame_wh, axis, truth)

    print("Band sweep (most inclusive first):")
    print("  band              span   speed     error    samples")
    results = []
    for label, span, band in candidates(axis):
        v, n = eval_band(track, cfg["speed"], frame_wh, band)
        if v is None:
            print(f"  {label:15s}  {span:.2f}   (too few samples)")
            continue
        err = (v - truth) / truth * 100
        robust = n >= MIN_BAND_SAMPLES
        note = "  <= target" if abs(err) <= args.target and robust else \
               ("  (thin)" if not robust else "")
        print(f"  {label:15s}  {span:.2f}   {v:5.1f} mph  {err:+6.1f}%   "
              f"{n:3d}{note}")
        results.append((label, span, band, v, err, n))

    full = eval_band(track, cfg["speed"], frame_wh, {"enabled": False})[0]
    print(f"\nFull-frame (gate off): {full:.1f} mph "
          f"({(full - truth) / truth * 100:+.1f}%)")

    robust = [r for r in results if r[5] >= MIN_BAND_SAMPLES]
    if not robust:
        raise SystemExit("No band kept enough samples -- widen the sweep or clip.")
    passing = [r for r in robust if abs(r[4]) <= args.target]
    if passing:
        # Most inclusive (largest span) band that hits the target = best baseline.
        label, span, band, v, err, n = max(passing, key=lambda r: r[1])
        note = f"most inclusive band within the {args.target:.1f}% target"
    else:
        label, span, band, v, err, n = min(robust, key=lambda r: abs(r[4]))
        note = (f"target {args.target:.1f}% NOT met; this is the most accurate "
                f"robust band -- loosen the target, re-calibrate, or move the camera")

    keys = ("x_min", "x_max") if axis == "x" else ("y_min", "y_max")
    print(f"\nRECOMMENDED band for {'head_on' if axis == 'y' else 'parallel'} "
          f"({note}):")
    for k in keys:
        print(f"  {k}: {band[k]}")
    other = "y_min/y_max: 0.0/1.0 (full height)" if axis == "x" else \
            "x_min/x_max: 0.0/1.0 (full width)"
    print(f"  {other}")
    print(f"  recovers {v:.1f} mph ({err:+.1f}%) from {n} samples vs "
          f"{full:.1f} mph ({(full - truth) / truth * 100:+.1f}%) full-frame.")
    print(f"  Set these under speed.measure_band."
          f"{'head_on' if axis == 'y' else 'parallel'} in config.yaml.")


if __name__ == "__main__":
    main()
