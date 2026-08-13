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

    def detect(self, frame):
        mask = self.bg.apply(frame)
        # Shadows are painted 127 by MOG2; drop them to keep only hard motion.
        _, mask = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self.kernel, iterations=2)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        detections = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < self.cfg["min_area"] or area > self.cfg["max_area"]:
                continue
            x, y, w, h = cv2.boundingRect(c)
            detections.append(
                Detection(
                    bbox=(x, y, w, h),
                    centroid=(x + w / 2.0, y + h / 2.0),
                    ground_point=(x + w / 2.0, y + h),  # bottom-centre
                    area=float(area),
                )
            )
        return detections, mask
