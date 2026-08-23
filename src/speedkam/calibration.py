# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Ground-plane calibration via homography.

The core idea of a fixed speed camera: the road surface is a flat plane. If we
know the real-world (metric) coordinates of a handful of points on that plane
and where they appear in the image, we can compute a homography H that maps any
image pixel on the road to real-world meters:

        [X, Y, 1]^T  ~  H . [u, v, 1]^T

Then a vehicle's ground-contact point (bottom-centre of its box) can be tracked
in meters, and speed = metric_distance / elapsed_time.

You produce the calibration once, from the mounted camera, using
tools/calibrate.py -- you click >=4 points on the road and type the tape-measured
X/Y (meters) for each. Accuracy of the calibration directly bounds accuracy of
the speed, so measure carefully and spread points along the whole zone.
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np


class Calibration:
    def __init__(self, image_points, world_points, meta=None):
        self.image_points = np.asarray(image_points, dtype=np.float64)
        self.world_points = np.asarray(world_points, dtype=np.float64)
        self.meta = meta or {}
        if len(self.image_points) < 4:
            raise ValueError("Homography needs at least 4 point correspondences.")
        H, mask = cv2.findHomography(
            self.image_points, self.world_points, method=cv2.RANSAC
        )
        if H is None:
            raise ValueError("findHomography failed; check that points are not collinear.")
        self.H = H
        self.inlier_mask = mask

    # ------------------------------------------------------------- transforms
    def image_to_world(self, pts):
        """Map Nx2 image points (pixels) to Nx2 world points (meters)."""
        pts = np.asarray(pts, dtype=np.float64).reshape(-1, 1, 2)
        world = cv2.perspectiveTransform(pts, self.H)
        return world.reshape(-1, 2)

    def reprojection_error(self):
        """Mean metric error (m) of the calibration points -- a quality check."""
        pred = self.image_to_world(self.image_points)
        return float(np.mean(np.linalg.norm(pred - self.world_points, axis=1)))

    # --------------------------------------------------------- road region gate
    def image_bounds(self):
        """(x_min, x_max, y_min, y_max) in pixels of the road points clicked
        during calibration -- the rectangle enclosing the measured road surface."""
        ip = self.image_points
        return (float(ip[:, 0].min()), float(ip[:, 0].max()),
                float(ip[:, 1].min()), float(ip[:, 1].max()))

    def on_road_side(self, pts, frame_wh=None, margin_frac=0.03):
        """Boolean mask over image points: which plausibly sit ON the drivable
        road, versus the camera-side FOREGROUND (kerb/grass/sidewalk in front of
        the road) or well off to either side.

        Only the NEAR edge (largest y, closest to the camera) and the two LATERAL
        edges are bounded. The FAR edge is deliberately left OPEN: calibration
        points are usually clicked across a narrow strip of road, yet a genuine
        vehicle legitimately rides "above" that strip (smaller y) as it recedes
        toward the vanishing point -- clamping the far side would reject real
        distant cars.

        This is the one check that separates a pedestrian in the foreground --
        whose ground point, mapped through a homography fitted only on the road
        plane, extrapolates to garbage and invents phantom speeds -- from a
        vehicle actually on the measured road. It is invariant to the blob's
        shape, so it catches what the aspect/size proxies miss (e.g. two people
        merged into one wide blob). Margins scale with the frame, so the gate is
        resolution-independent.
        """
        x0, x1, _, y1 = self.image_bounds()
        w, h = (frame_wh if frame_wh else (x1, y1))
        mx = margin_frac * float(w)
        my = margin_frac * float(h)
        pts = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
        return ((pts[:, 0] >= x0 - mx) & (pts[:, 0] <= x1 + mx)
                & (pts[:, 1] <= y1 + my))

    # ------------------------------------------------------------------- io
    def to_dict(self):
        return {
            "image_points": self.image_points.tolist(),
            "world_points": self.world_points.tolist(),
            "H": self.H.tolist(),
            "meta": self.meta,
        }

    def save(self, path):
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path):
        p = Path(path)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        obj = cls(data["image_points"], data["world_points"], data.get("meta"))
        if "H" in data:  # trust the stored H exactly
            obj.H = np.asarray(data["H"], dtype=np.float64)
        return obj
