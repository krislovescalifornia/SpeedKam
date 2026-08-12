#!/usr/bin/env python3
"""Generate a synthetic SIDE-ON road video for tuning the center-band gate.

Unlike tools/make_test_video.py (a car receding down the road, straight up the
image centre), this mimics the real deployment the center-band gate is for: a
camera mounted PARALLEL to the road, so traffic crosses the frame left<->right.

Crucially it injects the error the gate exists to reject: LENS DISTORTION.
The scene is rendered through a pinhole camera, then a barrel-distortion remap
is applied to every frame -- exactly what a real wide-angle lens does. The
saved calibration is the ideal pinhole homography (i.e. a calibration measured
near the frame centre, where distortion is negligible). So:

  * near the centre the image->meters mapping is accurate,
  * toward the left/right edges the car appears displaced by the lens, so its
    recovered world position -- and thus its speed -- is biased.

Running the pipeline full-frame therefore gives a position-dependent speed
error; the center-band gate recovers the truth by timing the car only across
the low-distortion middle. tools/tune_measure_band.py measures that and picks
the band. This generator is the ground truth those numbers are checked against.
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

W, H = 1280, 720
FPS = 30
CX, CY = W / 2.0, H / 2.0
FPIX = 900.0                       # focal length (px) ~ 71 deg horizontal FOV

SPEED_MPH = 30.0
SPEED_MS = SPEED_MPH / 2.2369362920544   # ~13.41 m/s

# Barrel distortion strength (normalised radial poly: scale = 1 + k1 r^2 + k2 r^4).
# Negative k1 = barrel (wide-angle look). This is the whole point of the video;
# with k1 = 0 the pinhole calibration is exact everywhere and the gate is a no-op.
K1, K2 = -0.18, 0.05

# Side-on camera: mounted off to the side of the road, elevated, looking ACROSS
# it (optical axis perpendicular to the direction of travel = world +X).
CAM_POS = (20.0, -14.0, 4.0)       # (X along road, Y across, Z up) metres
CAM_TARGET = (20.0, 3.5, 0.0)      # aim at the road centreline, mid-span

LANE_Y = 2.0                       # the crossing car's lane (metres across)
CAR_X0, CAR_X1 = 11.0, 29.0        # world X where the car enters / leaves frame


def look_at(C, T, up=(0.0, 0.0, 1.0)):
    C = np.array(C, float); T = np.array(T, float); up = np.array(up, float)
    f = T - C; f /= np.linalg.norm(f)          # forward  = +Z_cam
    r = np.cross(f, up); r /= np.linalg.norm(r)  # right    = +X_cam
    d = np.cross(f, r)                           # down     = +Y_cam (image down)
    R = np.stack([r, d, f], axis=0)              # rows map world->camera
    return R, C


R, C = look_at(CAM_POS, CAM_TARGET)


def project(P):
    """Ideal pinhole projection of a world point -> (u, v, depth)."""
    cam = R @ (np.asarray(P, float) - C)
    Xc, Yc, Zc = cam
    return CX + FPIX * Xc / Zc, CY + FPIX * Yc / Zc, Zc


def distortion_maps():
    """Remap that turns an ideal pinhole render into a barrel-distorted frame.

    For each destination (distorted) pixel we find the ideal source pixel along
    the radial line -- a standard, monotonic single-lens barrel model. Good
    enough to stand in for a real lens's edge displacement.
    """
    xs, ys = np.meshgrid(np.arange(W, dtype=np.float32), np.arange(H, dtype=np.float32))
    nx = (xs - CX) / FPIX
    ny = (ys - CY) / FPIX
    r2 = nx * nx + ny * ny
    scale = 1.0 + K1 * r2 + K2 * r2 * r2
    map_x = (CX + FPIX * nx * scale).astype(np.float32)
    map_y = (CY + FPIX * ny * scale).astype(np.float32)
    return map_x, map_y


def main():
    # --- Calibration: ideal pinhole homography (centre-accurate) --------------
    # Four road-plane points spanning both lanes and most of the visible stretch.
    world_pts = [(13.0, 0.5), (27.0, 0.5), (27.0, 5.5), (13.0, 5.5)]
    image_pts = [project((x, y, 0.0))[:2] for (x, y) in world_pts]
    world = np.array(world_pts, np.float64)
    image = np.array(image_pts, np.float64)
    H_img2world, _ = cv2.findHomography(image, world)

    Path("test_sideon_calibration.json").write_text(
        json.dumps({
            "image_points": image.tolist(),
            "world_points": world.tolist(),
            "H": H_img2world.tolist(),
            "meta": {"units": "meters", "synthetic": True, "sideon": True},
        }, indent=2),
        encoding="utf-8",
    )

    # --- Static textured background so MOG2 has a stable model -----------------
    rng = np.random.default_rng(0)
    background = rng.integers(60, 110, (H, W, 3)).astype(np.uint8)
    # Fill the drivable road surface (a swept band of the plane) a flat grey.
    road = np.array([
        project((CAR_X0 - 2, -0.5, 0.0))[:2],
        project((CAR_X1 + 2, -0.5, 0.0))[:2],
        project((CAR_X1 + 2, 7.5, 0.0))[:2],
        project((CAR_X0 - 2, 7.5, 0.0))[:2],
    ], np.int32)
    cv2.fillPoly(background, [road], (90, 90, 95))

    map_x, map_y = distortion_maps()
    def distort(img):
        return cv2.remap(img, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter("test_sideon.mp4", fourcc, FPS, (W, H))

    warmup, trailing = 45, 20
    duration = (CAR_X1 - CAR_X0) / SPEED_MS
    n_car = int(duration * FPS)

    bg_distorted = distort(background)
    for _ in range(warmup):
        out.write(bg_distorted.copy())

    for i in range(n_car):
        frame = background.copy()
        X = CAR_X0 + SPEED_MS * (i / FPS)
        # Car footprint on the plane: ~4.2 m long (along X), ~1.8 m wide (along Y).
        gx, gy, _ = project((X, LANE_Y, 0.0))                 # ground contact
        front = project((X + 2.1, LANE_Y, 0.0))
        back = project((X - 2.1, LANE_Y, 0.0))
        far = project((X, LANE_Y + 1.8, 0.0))
        length_px = int(abs(front[0] - back[0])) + 10
        height_px = int(abs(far[1] - gy)) + int(0.35 * length_px)  # add body height
        x0 = int(gx - length_px / 2)
        y0 = int(gy - height_px)
        cv2.rectangle(frame, (x0, y0), (x0 + length_px, int(gy)), (30, 30, 200), -1)
        cv2.rectangle(frame, (x0 + 6, y0 + 5),
                      (x0 + length_px - 6, y0 + height_px // 2), (120, 160, 220), -1)
        out.write(distort(frame))

    for _ in range(trailing):
        out.write(bg_distorted.copy())

    out.release()

    total = warmup + n_car + trailing
    Path("test_sideon.meta.json").write_text(
        json.dumps({
            "truth_mph": SPEED_MPH,
            "truth_ms": SPEED_MS,
            "fps": FPS,
            "width": W, "height": H,
            "k1": K1, "k2": K2,
            "video": "test_sideon.mp4",
            "calibration": "test_sideon_calibration.json",
        }, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote test_sideon.mp4 ({total} frames, {total / FPS:.1f}s).")
    print(f"Ground-truth speed: {SPEED_MPH:.1f} mph ({SPEED_MS:.2f} m/s).")
    print(f"Barrel distortion:  k1={K1}, k2={K2} (edges displaced; centre clean).")
    print("Wrote test_sideon_calibration.json + test_sideon.meta.json.")
    print("\nNow tune the gate:")
    print("  python tools/tune_measure_band.py")


if __name__ == "__main__":
    main()
