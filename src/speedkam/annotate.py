# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Overlay drawing for the live view and burned-in clip annotations."""
from __future__ import annotations

import cv2

GREEN = (0, 200, 0)
RED = (0, 0, 255)
YELLOW = (0, 220, 220)
WHITE = (255, 255, 255)


def draw_tracks(frame, tracks, units):
    draw_track_boxes(
        frame,
        [(tr.id, tr.last_bbox, tr.last_ground) for tr in tracks],
    )


def draw_track_boxes(frame, items):
    """Draw track boxes from plain (id, bbox, ground) snapshots.

    Takes immutable tuples rather than live Track objects so the drawing can run
    on a different thread (the preview encoder) than the tracker, without racing
    on track state.
    """
    for tid, bbox, ground in items:
        x, y, w, h = [int(v) for v in bbox]
        cv2.rectangle(frame, (x, y), (x + w, y + h), GREEN, 2)
        cv2.putText(
            frame, f"#{tid}", (x, max(0, y - 8)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, GREEN, 2,
        )
        gx, gy = [int(v) for v in ground]
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
