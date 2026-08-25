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

    def detect(self, frame, upscale=None, roi=None):
        """Detect moving blobs and return (detections, mask).

        `frame` is what MOG2 runs on. Two ways to feed it a smaller frame (both
        quarter the work when halving each side):
          * software (default): pass the full-resolution frame and leave
            `upscale=None`; we downscale it here by `detect_scale`.
          * hardware: pass a frame that is ALREADY downscaled (e.g. a picamera2
            `lores` stream the ISP produced for free) and give `upscale` = the
            factor that maps its pixels back to full resolution. No software
            resize happens, which is the whole point on a weak CPU.
        Either way, all output coordinates/areas are in full-resolution pixels.

        `roi` optionally restricts MOG2 to a rectangular sub-window of the frame
        -- the road band -- as ``(x0, y0, x1, y1)`` FRACTIONS of the full frame
        (each 0..1). Running detection on that strip instead of the whole frame
        is a large CPU win (detection cost is ~linear in pixels), and on a
        parallel node it stops the detect loop from starving the capture thread.
        The crop offset is added back to every box BEFORE up-scaling, so output
        coordinates are IDENTICAL to a full-frame detection of the same blob --
        tracking and the crossing-time speed are completely unaware of the crop.
        `roi=None` (the default) is byte-for-byte the original full-frame path.
        Blobs whose ground point falls outside the band are simply not seen, so
        the band must be calibrated to contain vehicle tyre-lines across the full
        crossing span (validated by the pipeline before this is ever enabled).
        """
        if upscale is not None:
            small = frame                  # already at detection resolution
            inv = float(upscale)
        elif self.scale < 1.0:
            small = cv2.resize(frame, None, fx=self.scale, fy=self.scale,
                               interpolation=cv2.INTER_AREA)
            inv = 1.0 / self.scale
        else:
            small = frame
            inv = 1.0

        # Road-band crop. Fractions -> pixels in the SMALL (detection) frame, so
        # it's independent of detect_scale. The offset (ox, oy) is carried into
        # the box mapping below. A degenerate/empty crop falls back to no crop so
        # a bad config can never blind detection outright.
        ox = oy = 0
        if roi is not None:
            sh, sw = small.shape[:2]
            x0 = max(0, min(sw - 1, int(roi[0] * sw)))
            y0 = max(0, min(sh - 1, int(roi[1] * sh)))
            x1 = max(x0 + 1, min(sw, int(roi[2] * sw)))
            y1 = max(y0 + 1, min(sh, int(roi[3] * sh)))
            if (x1 - x0) < sw or (y1 - y0) < sh:
                small = small[y0:y1, x0:x1]
                ox, oy = x0, y0

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
        area_scale = inv * inv
        detections = []
        for c in contours:
            area = cv2.contourArea(c) * area_scale
            if area < self.cfg["min_area"] or area > self.cfg["max_area"]:
                continue
            x, y, w, h = cv2.boundingRect(c)
            # Add the crop offset back (in small-frame pixels) BEFORE up-scaling,
            # so a blob reports the same full-res box whether or not an ROI was
            # applied.
            x, y, w, h = (x + ox) * inv, (y + oy) * inv, w * inv, h * inv
            detections.append(
                Detection(
                    bbox=(x, y, w, h),
                    centroid=(x + w / 2.0, y + h / 2.0),
                    ground_point=(x + w / 2.0, y + h),  # bottom-centre
                    area=float(area),
                )
            )
        return detections, mask
