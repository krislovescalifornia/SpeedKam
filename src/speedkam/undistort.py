# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Optional lens undistortion, applied to every captured frame.

A wide-angle lens bends straight lines (barrel distortion). That bending
corrupts the ground-plane homography most toward the frame edges -- the very
edges the center-band gate throws away. Undistorting the frame first
straightens the image so the pinhole homography is valid across MORE of the
frame, letting you widen (or even drop) the measurement band.

IMPORTANT: undistortion changes pixel coordinates, so it must be applied
CONSISTENTLY at calibration time and at run time. It lives in the Camera, so
tools/calibrate.py, the live pipeline and the web calibrator all see undistorted
frames automatically -- but you must (re)build the calibration after enabling or
changing these settings, or the homography will be measured in the wrong space.

Intrinsics: supply fx/fy/cx/cy if you have them (a checkerboard calibration),
otherwise they are derived from a horizontal field-of-view guess and the frame
size. Distortion coefficients follow OpenCV order: [k1, k2, p1, p2, k3], where
k1/k2/k3 are radial (k1 dominates; negative = barrel) and p1/p2 tangential.
"""
from __future__ import annotations

import math

import cv2
import numpy as np


class Undistorter:
    def __init__(self, cfg):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled"))
        self.cfg = cfg
        self.dist = np.array(cfg.get("dist_coeffs") or [0.0, 0.0, 0.0, 0.0, 0.0],
                             dtype=np.float64)
        self.alpha = float(cfg.get("alpha", 0.0))
        self._maps = None            # (map1, map2), built lazily at first frame
        self._size = None            # (w, h) the maps were built for

    def _build(self, w, h):
        cfg = self.cfg
        fx, fy = cfg.get("fx"), cfg.get("fy")
        cx, cy = cfg.get("cx"), cfg.get("cy")
        if not fx:
            fov = float(cfg.get("fov_deg", 70.0))
            fx = (w / 2.0) / math.tan(math.radians(fov) / 2.0)
        fy = fy or fx
        cx = w / 2.0 if cx is None else cx
        cy = h / 2.0 if cy is None else cy
        K = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], dtype=np.float64)
        # alpha=0 crops to only valid pixels; alpha=1 keeps all (black borders).
        new_K, _ = cv2.getOptimalNewCameraMatrix(K, self.dist, (w, h),
                                                  self.alpha, (w, h))
        self._maps = cv2.initUndistortRectifyMap(
            K, self.dist, None, new_K, (w, h), cv2.CV_16SC2
        )
        self._size = (w, h)

    def apply(self, frame):
        if not self.enabled or frame is None:
            return frame
        h, w = frame.shape[:2]
        if self._maps is None or self._size != (w, h):
            self._build(w, h)
        return cv2.remap(frame, self._maps[0], self._maps[1], cv2.INTER_LINEAR)
