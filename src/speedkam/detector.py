# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Vehicle detection by background subtraction (MOG2).

This is deliberately lightweight so it runs in real time on a Raspberry Pi with
no accelerator. It learns the static background (empty road) and flags moving
blobs. For a private road with occasional traffic this is robust and cheap.

If you later want class-aware detection (car vs. person vs. dog) you can swap
this module for a YOLO/MobileNet detector behind the same `detect()` interface
-- everything downstream (tracker, speed, recorder) stays the same.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Detection:
    bbox: tuple          # (x, y, w, h)
    centroid: tuple      # (cx, cy) in pixels
    ground_point: tuple  # (gx, gy) bottom-centre of box, ~tire contact line
    area: float


class MotionDetector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.bg = cv2.createBackgroundSubtractorMOG2(
            history=cfg["history"],
            varThreshold=cfg["var_threshold"],
            detectShadows=cfg["detect_shadows"],
        )
        k = cfg["morph_kernel"]
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        # Detection can run on a DOWNSCALED copy of each frame -- MOG2 +
        # morphology + findContours all cost roughly one op per pixel, so
        # halving each dimension quarters the work and is the single biggest
        # win for FPS on a Raspberry Pi. All coordinates and areas below are
        # scaled back UP to full-resolution pixels before we hand them out, so
        # the calibration homography, min_area/max_area and the annotator are
        # completely unaware detection ever saw a smaller frame.
        scale = float(cfg.get("detect_scale", 1.0) or 1.0)
        self.scale = scale if 0.0 < scale <= 1.0 else 1.0

    def detect(self, frame):
        if self.scale < 1.0:
            small = cv2.resize(frame, None, fx=self.scale, fy=self.scale,
                               interpolation=cv2.INTER_AREA)
        else:
            small = frame

        mask = self.bg.apply(small)
        # Shadows are painted 127 by MOG2; drop them to keep only hard motion.
        # (Only meaningful when detect_shadows is on -- with it off, MOG2 skips
        # the per-pixel shadow test entirely, which is cheaper.)
        _, mask = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self.kernel, iterations=2)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        # Map detection-space pixels back to full-resolution pixels. Areas scale
        # by the square of the linear factor.
        inv = 1.0 / self.scale
        area_scale = inv * inv
        detections = []
        for c in contours:
            area = cv2.contourArea(c) * area_scale
            if area < self.cfg["min_area"] or area > self.cfg["max_area"]:
                continue
            x, y, w, h = cv2.boundingRect(c)
            x, y, w, h = x * inv, y * inv, w * inv, h * inv
            detections.append(
                Detection(
                    bbox=(x, y, w, h),
                    centroid=(x + w / 2.0, y + h / 2.0),
                    ground_point=(x + w / 2.0, y + h),  # bottom-centre
                    area=float(area),
                )
            )
        return detections, mask
