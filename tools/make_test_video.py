#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate a synthetic road video + matching calibration to validate SpeedKam.

We define a known ground-plane homography and drive a "car" along the road at a
KNOWN speed, projecting it into the image each frame. Running SpeedKam on the
resulting video should recover that speed -- an end-to-end check of the whole
detect -> track -> homography -> speed chain without any hardware.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

W, H = 1280, 720
FPS = 30
SPEED_MPH = 30.0
SPEED_MS = SPEED_MPH / 2.2369362920544  # ~13.41 m/s

# World<->image correspondences (a 20 m x 4 m road patch seen in perspective).
WORLD = [(0.0, 0.0), (20.0, 0.0), (20.0, 4.0), (0.0, 4.0)]
IMAGE = [(300, 680), (540, 300), (740, 300), (980, 680)]


def main():
    world = np.array(WORLD, dtype=np.float64)
    image = np.array(IMAGE, dtype=np.float64)
    H_img2world, _ = cv2.findHomography(image, world)
    H_world2img, _ = cv2.findHomography(world, image)

    # Save calibration exactly as tools/calibrate.py would.
    calib = {
        "image_points": image.tolist(),
        "world_points": world.tolist(),
        "H": H_img2world.tolist(),
        "meta": {"units": "meters", "synthetic": True},
    }
    Path("test_calibration.json").write_text(json.dumps(calib, indent=2))

    def w2i(X, Y):
        p = cv2.perspectiveTransform(
            np.array([[[X, Y]]], dtype=np.float64), H_world2img
        )
        return p[0, 0]

    # Static textured background so MOG2 has a stable model and the car stands out.
    rng = np.random.default_rng(0)
    background = (rng.integers(60, 110, (H, W, 3))).astype(np.uint8)
    cv2.rectangle(background, (250, 300), (1000, 690), (90, 90, 95), -1)  # road

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter("test_road.mp4", fourcc, FPS, (W, H))

    warmup = 45   # frames of empty road for background learning
    trailing = 20  # empty frames so the track finalises
    x0, x1 = 2.0, 18.0
    duration = (x1 - x0) / SPEED_MS
    n_car = int(duration * FPS)

    for _ in range(warmup):
        out.write(background.copy())

    for i in range(n_car):
        frame = background.copy()
        X = x0 + SPEED_MS * (i / FPS)
        gx, gy = w2i(X, 2.0)                 # ground contact (road centre line)
        left = w2i(X, 1.0)
        right = w2i(X, 3.0)
        width = int(abs(right[0] - left[0])) + 20
        height = int(width * 0.7)
        x, y = int(gx - width / 2), int(gy - height)
        cv2.rectangle(frame, (x, y), (x + width, int(gy)), (30, 30, 200), -1)
        cv2.rectangle(frame, (x + 8, y + 6), (x + width - 8, y + height // 2),
                      (120, 160, 220), -1)  # windshield
        out.write(frame)

    for _ in range(trailing):
        out.write(background.copy())

    out.release()
    total = warmup + n_car + trailing
    print(f"Wrote test_road.mp4 ({total} frames, {total / FPS:.1f}s).")
    print(f"Ground-truth speed: {SPEED_MPH:.1f} mph ({SPEED_MS:.2f} m/s).")
    print("Wrote test_calibration.json.")
    print("\nNow run:")
    print("  python run.py --config config.test.yaml --source test_road.mp4 --no-display")


if __name__ == "__main__":
    main()
