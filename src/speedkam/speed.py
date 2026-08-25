# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Two-line crossing-time speed estimation.

A car crossing between two fixed image columns ``x_a`` and ``x_b`` at a KNOWN
speed fixes the real-world distance of that stretch of road -- one number per
travel direction (``d_east_m`` / ``d_west_m``), set once by driving past each way
at a known speed (the log prints the crossing time; ``d = known_mps * seconds``).
Every other car's speed is then that fixed distance over its own crossing time.

This uses raw pixel x and capture timestamps only -- no homography, no lens
undistortion, no trajectory-quality gates. It replaced an earlier ground-plane
homography engine; the false-positive filtering that remains (car-shape aspect,
pixel width, area coherence, drive-by dedupe) is all pixel-only and lives in the
pipeline, not here.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

KMH_PER_MS = 3.6
MPH_PER_MS = 2.2369362920544


@dataclass
class SpeedResult:
    speed_kmh: float
    speed_mph: float
    direction: str
    distance_m: float
    duration_s: float
    n_samples: int
    confidence: str          # "ok" | "low"

    def display(self, units):
        val = self.speed_mph if units == "mph" else self.speed_kmh
        unit = "mph" if units == "mph" else "km/h"
        return f"{val:.0f} {unit}"


def _crossing_seconds(ts, xs, xa, xb):
    """Seconds for the centre-x to travel between image columns xa and xb, by
    linear interpolation of the first crossing of each. None if it never crossed
    both (e.g. a car turning off, or noise that didn't traverse the stretch)."""
    def first_cross(xc):
        for i in range(1, len(xs)):
            if (xs[i - 1] - xc) * (xs[i] - xc) <= 0 and xs[i] != xs[i - 1]:
                f = (xc - xs[i - 1]) / (xs[i] - xs[i - 1])
                return ts[i - 1] + f * (ts[i] - ts[i - 1])
        return None
    ta, tb = first_cross(xa), first_cross(xb)
    if ta is None or tb is None:
        return None
    # float() so downstream speed/duration are plain Python floats, not numpy
    # scalars -- a numpy value propagates into `over = speed > limit` as a
    # numpy bool_, which then breaks jsonify() of the status/event payloads.
    return float(abs(tb - ta))


def estimate(track, cfg, frame_wh=None) -> SpeedResult | None:
    """Two-line crossing-time speed. Returns None when the track never traverses
    the measured stretch (not a road pass) or the reading is out of plausible
    bounds. When the pass's travel direction hasn't been calibrated yet, prints
    the crossing time so a known-speed pass can be turned into the distance, and
    returns None. In this camera's view eastbound runs right-to-left (x
    decreasing)."""
    samples = list(track.samples)
    if len(samples) < cfg["min_samples"]:
        return None
    ts = np.array([s.t for s in samples], dtype=np.float64)
    xs = np.array([s.ground_px[0] for s in samples], dtype=np.float64)
    xa = float(cfg.get("x_a", 1000)); xb = float(cfg.get("x_b", 450))
    dt = _crossing_seconds(ts, xs, xa, xb)
    if dt is None or dt <= 0:
        return None  # never traversed the measured stretch -> not a road pass

    east = xs[-1] < xs[0]
    direction = cfg["direction_negative"] if east else cfg["direction_positive"]
    D = cfg.get("d_east_m") if east else cfg.get("d_west_m")
    if not D:
        # Not calibrated for this direction yet. Emit the crossing time so a
        # known-speed pass can be turned into the distance: d = known_mps * dt.
        print(f"[SpeedKam] CALIBRATE {direction}: crossed x{xa:.0f}->{xb:.0f} in "
              f"{dt:.3f}s  (set d_{'east' if east else 'west'}_m = known_mps * "
              f"{dt:.3f})", flush=True)
        return None

    v = float(D) / dt                       # metres / second
    kmh = v * KMH_PER_MS
    if kmh < cfg["min_speed_kmh"] or kmh > cfg["max_speed_kmh"]:
        return None
    return SpeedResult(
        speed_kmh=kmh,
        speed_mph=v * MPH_PER_MS,
        direction=direction,
        distance_m=float(D),
        duration_s=dt,
        n_samples=len(samples),
        confidence="ok",
    )
