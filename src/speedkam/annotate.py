# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Overlay drawing for the live view and burned-in clip annotations."""
from __future__ import annotations

import cv2
import numpy as np

GREEN = (0, 200, 0)
RED = (0, 0, 255)
YELLOW = (0, 220, 220)
WHITE = (255, 255, 255)
BLUE = (255, 160, 0)


def draw_zone(frame, calibration):
    """Outline the calibration points so you can see the measured road region."""
    if calibration is None:
        return
    pts = calibration.image_points.astype(int)
    for (x, y) in pts:
        cv2.circle(frame, (int(x), int(y)), 5, BLUE, -1)


def draw_measure_band(frame, band):
    """Dim the region OUTSIDE the center-band gate so you can see where speed
    is (and isn't) measured. No-op when the gate is disabled."""
    if not band or not band.get("enabled"):
        return
    h, w = frame.shape[:2]
    x_lo = int(band.get("x_min", 0.0) * w)
    x_hi = int(band.get("x_max", 1.0) * w)
    y_lo = int(band.get("y_min", 0.0) * h)
    y_hi = int(band.get("y_max", 1.0) * h)
    # Blend a dark overlay everywhere, then restore the in-band rectangle so the
    # measured strip stays bright and the excluded margins read as shaded.
    dimmed = cv2.addWeighted(frame, 0.55, np.zeros_like(frame), 0.45, 0)
    dimmed[y_lo:y_hi, x_lo:x_hi] = frame[y_lo:y_hi, x_lo:x_hi]
    frame[:] = dimmed
    cv2.rectangle(frame, (x_lo, y_lo), (x_hi, y_hi), YELLOW, 1)


def draw_tracks(frame, tracks, units):
    for tr in tracks:
        x, y, w, h = [int(v) for v in tr.last_bbox]
        cv2.rectangle(frame, (x, y), (x + w, y + h), GREEN, 2)
        cv2.putText(
            frame, f"#{tr.id}", (x, max(0, y - 8)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, GREEN, 2,
        )
        gx, gy = [int(v) for v in tr.last_ground]
        cv2.circle(frame, (gx, gy), 4, YELLOW, -1)


def draw_hud(frame, text, over_limit=False):
    color = RED if over_limit else WHITE
    cv2.rectangle(frame, (0, 0), (frame.shape[1], 40), (0, 0, 0), -1)
    cv2.putText(frame, text, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)


def draw_speed_banner(frame, result, limit_kmh, units):
    """Big result banner burned onto a saved clip/snapshot."""
    speeding = result.speed_kmh > limit_kmh
    color = RED if speeding else GREEN
    label = result.display(units)
    if speeding:
        label += "  SPEEDING"
    if result.confidence == "low":
        label += "  (low conf.)"
    h = frame.shape[0]
    cv2.rectangle(frame, (0, h - 46), (frame.shape[1], h), (0, 0, 0), -1)
    cv2.putText(
        frame, f"{label}   {result.direction}", (10, h - 14),
        cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 2,
    )
