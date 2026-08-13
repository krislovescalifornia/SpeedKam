#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate a synthetic HEAD-ON road video for tuning the head_on band preset.

The companion to tools/make_sideon_video.py, for the other mounting: the camera
faces oncoming/receding traffic, looking DOWN the road at a shallow angle. A car
drives away from the camera at a known speed, so in the image it travels up the
centre toward the vanishing point and shrinks as it goes.

The error this exposes is NOT lens distortion -- it is perspective itself. Near
the camera the road has many pixels per metre; toward the vanishing point that
collapses toward zero. So the detector's inherent localisation uncertainty (just
integer-pixel bounding-box quantisation here) is worth centimetres up close but
many metres far away, and the recovered speed of a distant car is wildly
unreliable even though the calibration is exact. That is why a head-on camera
should measure only in a near/mid VERTICAL band and ignore the far top of frame
-- which tools/tune_measure_band.py quantifies here.

JITTER_PX below adds optional extra seeded sub-pixel noise on top of that
quantisation; it defaults to 0 so the clip is fully deterministic (repeatable
tuning). Perspective alone already amplifies the quantisation into a large
far-field error, so 0 is enough to demonstrate and tune the band.
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

W, H = 1280, 720
FPS = 30
CX, CY = W / 2.0, H / 2.0
FPIX = 800.0

SPEED_MPH = 30.0
SPEED_MS = SPEED_MPH / 2.2369362920544   # ~13.41 m/s

# Head-on camera: near one end of the road, elevated, looking DOWN it (optical
# axis roughly along the direction of travel = world +X) with a slight downtilt.
CAM_POS = (0.0, 3.5, 5.0)          # (X along road, Y across, Z up) metres
CAM_TARGET = (28.0, 3.5, 0.0)      # aim mid-road so near tarmac is in frame

LANE_Y = 2.5                       # the receding car's lane (metres across)
CAR_X0, CAR_X1 = 7.0, 62.0         # world X: near (bottom) -> far (top of frame)

# Seeded per-frame localisation jitter (pixels) standing in for detector noise.
JITTER_PX = 0.0


def look_at(C, T, up=(0.0, 0.0, 1.0)):
    C = np.array(C, float); T = np.array(T, float); up = np.array(up, float)
    f = T - C; f /= np.linalg.norm(f)
    r = np.cross(f, up); r /= np.linalg.norm(r)
    d = np.cross(f, r)
    R = np.stack([r, d, f], axis=0)
    return R, C


R, C = look_at(CAM_POS, CAM_TARGET)


def project(P):
    cam = R @ (np.asarray(P, float) - C)
    Xc, Yc, Zc = cam
    return CX + FPIX * Xc / Zc, CY + FPIX * Yc / Zc, Zc


def main():
    # --- Calibration: exact pinhole homography over a NEAR/MID rectangle -------
    # (What you can physically tape-measure. The far field is extrapolation --
    # geometrically exact here, but that is exactly where pixels run out.)
    world_pts = [(9.0, 1.0), (26.0, 1.0), (26.0, 6.0), (9.0, 6.0)]
    image_pts = [project((x, y, 0.0))[:2] for (x, y) in world_pts]
    world = np.array(world_pts, np.float64)
    image = np.array(image_pts, np.float64)
    H_img2world, _ = cv2.findHomography(image, world)

    Path("test_headon_calibration.json").write_text(
        json.dumps({
            "image_points": image.tolist(),
            "world_points": world.tolist(),
            "H": H_img2world.tolist(),
            "meta": {"units": "meters", "synthetic": True, "headon": True},
        }, indent=2),
        encoding="utf-8",
    )

    rng = np.random.default_rng(0)
    background = rng.integers(60, 110, (H, W, 3)).astype(np.uint8)
    road = np.array([
        project((CAR_X0 - 1, 0.0, 0.0))[:2],
        project((CAR_X1 + 5, 3.0, 0.0))[:2],
        project((CAR_X1 + 5, 4.0, 0.0))[:2],
        project((CAR_X0 - 1, 7.0, 0.0))[:2],
    ], np.int32)
    cv2.fillPoly(background, [road], (90, 90, 95))

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter("test_headon.mp4", fourcc, FPS, (W, H))

    warmup, trailing = 45, 20
    duration = (CAR_X1 - CAR_X0) / SPEED_MS
    n_car = int(duration * FPS)

    for _ in range(warmup):
        out.write(background.copy())

    ys, xs = [], []
    for i in range(n_car):
        frame = background.copy()
        X = CAR_X0 + SPEED_MS * (i / FPS)
        jx, jy = rng.normal(0, JITTER_PX, 2)
        gx, gy, _ = project((X, LANE_Y, 0.0))
        front = project((X + 2.1, LANE_Y, 0.0))
        back = project((X - 2.1, LANE_Y, 0.0))
        side = project((X, LANE_Y + 1.8, 0.0))
        length_px = int(abs(front[1] - back[1])) + int(0.9 * abs(side[0] - gx)) + 8
        width_px = int(abs(side[0] - gx) * 2) + 10
        gx += jx; gy += jy
        x0 = int(gx - width_px / 2)
        y0 = int(gy - length_px)
        cv2.rectangle(frame, (x0, y0), (x0 + width_px, int(gy)), (30, 30, 200), -1)
        cv2.rectangle(frame, (x0 + 5, y0 + 4),
                      (x0 + width_px - 5, y0 + length_px // 2), (120, 160, 220), -1)
        out.write(frame)
        xs.append(gx / W); ys.append(gy / H)

    for _ in range(trailing):
        out.write(background.copy())
    out.release()

    total = warmup + n_car + trailing
    Path("test_headon.meta.json").write_text(
        json.dumps({
            "truth_mph": SPEED_MPH, "truth_ms": SPEED_MS, "fps": FPS,
            "width": W, "height": H, "orientation": "head_on",
            "video": "test_headon.mp4", "calibration": "test_headon_calibration.json",
        }, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote test_headon.mp4 ({total} frames, {total / FPS:.1f}s).")
    print(f"Ground-truth speed: {SPEED_MPH:.1f} mph ({SPEED_MS:.2f} m/s).")
    print(f"Car image x: {min(xs):.2f}-{max(xs):.2f}  y: {min(ys):.2f}-{max(ys):.2f} "
          f"(near=bottom/high y -> far=top/low y).")
    print("Wrote test_headon_calibration.json + test_headon.meta.json.")
    print("\nNow tune the head_on band:")
    print("  python tools/tune_measure_band.py --meta test_headon.meta.json")


if __name__ == "__main__":
    main()
