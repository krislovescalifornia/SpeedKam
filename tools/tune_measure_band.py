#!/usr/bin/env python3
"""Tune the center-band measurement gate against a video of known speed.

It replays a video through the real detect -> track -> homography chain, takes
the vehicle's track, then sweeps candidate horizontal bands and reports the
recovered speed for each versus the known ground truth. Because a side-on
camera's lens distorts the frame edges, timing the car full-frame is biased;
narrowing the band trades a little baseline for accuracy. This finds the
WIDEST band (longest, most robust baseline) that still hits the error target.

Usage:
  python tools/make_sideon_video.py         # once, to create the test clip
  python tools/tune_measure_band.py         # reads test_sideon.meta.json

  # or point it at your own clip of a car at a known speed:
  python tools/tune_measure_band.py --video clip.mp4 \
      --calibration calibration.json --truth-mph 30 --target 1.5
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


def recovered_speed(track, cfg_speed, frame_wh, band):
    cfg = {**cfg_speed, "measure_band": band}
    r = speed_mod.estimate(track, cfg, frame_wh)
    return None if r is None else r.speed_mph


def position_profile(track, calibration, frame_wh):
    """Per-segment instantaneous speed vs. horizontal position (shows the edges)."""
    xs = [s.ground_px[0] / frame_wh[0] for s in track.samples if s.world is not None]
    ss = [s for s in track.samples if s.world is not None]
    rows = []
    for a, b in zip(ss, ss[1:]):
        dt = b.t - a.t
        if dt <= 0:
            continue
        dx = b.world[0] - a.world[0]
        dy = b.world[1] - a.world[1]
        v = (dx * dx + dy * dy) ** 0.5 / dt * MPH_PER_MS
        xf = (a.ground_px[0] + b.ground_px[0]) / 2 / frame_wh[0]
        rows.append((xf, v))
    return rows


def main():
    ap = argparse.ArgumentParser(description="Tune the center-band measurement gate")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--video", help="video file (default: from test_sideon.meta.json)")
    ap.add_argument("--calibration", help="calibration json (default: from meta)")
    ap.add_argument("--truth-mph", type=float, help="ground-truth speed (default: from meta)")
    ap.add_argument("--target", type=float, default=2.0,
                    help="max acceptable speed error, percent (default 2.0)")
    args = ap.parse_args()

    meta = {}
    meta_path = Path("test_sideon.meta.json")
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

    video = args.video or meta.get("video")
    calib_path = args.calibration or meta.get("calibration")
    truth = args.truth_mph if args.truth_mph is not None else meta.get("truth_mph")
    if not (video and calib_path and truth):
        raise SystemExit("Need --video, --calibration and --truth-mph "
                         "(or run tools/make_sideon_video.py first).")

    cfg = load_config(args.config)
    calibration = Calibration.load(calib_path)
    if calibration is None:
        raise SystemExit(f"Could not load calibration {calib_path!r}")

    track, frame_wh = collect_track(cfg, video, calibration)
    print(f"Track #{track.id}: {len(track.samples)} samples across the frame.")
    print(f"Ground truth: {truth:.1f} mph   (target error <= {args.target:.1f}%)\n")

    # 1) Where does the mapping go wrong? Instantaneous speed by x-position.
    prof = position_profile(track, calibration, frame_wh)
    print("Instantaneous speed by horizontal position (x = fraction of width):")
    for lo in [i / 10 for i in range(10)]:
        vals = [v for (xf, v) in prof if lo <= xf < lo + 0.1]
        if vals:
            m = sum(vals) / len(vals)
            err = (m - truth) / truth * 100
            bar = "#" * min(40, int(abs(err) * 2))
            print(f"  x {lo:.1f}-{lo+0.1:.1f}: {m:5.1f} mph  ({err:+5.1f}%)  {bar}")
    print()

    # 2) Sweep symmetric bands about the frame centre; find the widest that hits
    #    the target. Wider band = longer baseline, so prefer the widest that works.
    print("Band sweep (symmetric about centre):")
    print("  half-width   band          speed     error")
    results = []
    for hw in [0.50, 0.45, 0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10]:
        band = {"enabled": True, "x_min": round(0.5 - hw, 3),
                "x_max": round(0.5 + hw, 3), "y_min": 0.0, "y_max": 1.0}
        v = recovered_speed(track, cfg["speed"], frame_wh, band)
        if v is None:
            print(f"  +/-{hw:.2f}      "
                  f"[{band['x_min']:.2f},{band['x_max']:.2f}]   (too few samples)")
            continue
        err = (v - truth) / truth * 100
        ok = abs(err) <= args.target
        tag = "full-frame" if hw >= 0.5 else f"+/-{hw:.2f}   "
        print(f"  {tag}   [{band['x_min']:.2f},{band['x_max']:.2f}]   "
              f"{v:5.1f} mph  {err:+5.1f}%{'  <= target' if ok else ''}")
        results.append((hw, band, v, err))

    full = recovered_speed(track, cfg["speed"], frame_wh, {"enabled": False})
    print(f"\nFull-frame (gate off): {full:.1f} mph "
          f"({(full - truth) / truth * 100:+.1f}%)")

    if not results:
        raise SystemExit("No band produced a reading -- check detection/calibration.")

    passing = [r for r in results if abs(r[3]) <= args.target]
    if passing:
        # Widest band that still hits the target = longest, most robust baseline.
        hw, band, v, err = max(passing, key=lambda r: r[0])
        note = f"widest band within the {args.target:.1f}% target"
    else:
        # Nothing met it; recommend the most accurate band and say so plainly.
        hw, band, v, err = min(results, key=lambda r: abs(r[3]))
        note = (f"target {args.target:.1f}% NOT met by any band; this is the most "
                f"accurate one -- loosen the lens/calibration or the target")
    print(f"\nRECOMMENDED band ({note}):")
    print(f"  x_min: {band['x_min']}")
    print(f"  x_max: {band['x_max']}")
    print(f"  recovers {v:.1f} mph ({err:+.1f}%) vs {full:.1f} mph "
          f"({(full - truth) / truth * 100:+.1f}%) full-frame.")
    print("  Set these under speed.measure_band in config.yaml (enabled: true).")


if __name__ == "__main__":
    main()
