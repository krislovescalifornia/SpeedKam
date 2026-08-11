"""Speed estimation from a track's metric samples.

Given (t, X, Y) samples in real-world meters, estimate the vehicle's speed.
We use two independent estimators and prefer the robust one:

  * Regression estimate: fit cumulative path length s(t) = v*t + b by least
    squares. The slope v (m/s) is the speed; it naturally averages out
    per-frame jitter over the whole pass.
  * Median instantaneous: median of per-segment (dist/dt). A robust cross-check
    that rejects a few bad frames.

Reported speed is the regression value; if the two disagree wildly the reading
is marked low-confidence.
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
    peak_index: int          # sample index near mid-pass, for snapshot framing

    def display(self, units):
        val = self.speed_mph if units == "mph" else self.speed_kmh
        unit = "mph" if units == "mph" else "km/h"
        return f"{val:.0f} {unit}"


def estimate(track, cfg) -> SpeedResult | None:
    samples = [s for s in track.samples if s.world is not None]
    if len(samples) < cfg["min_samples"]:
        return None

    t = np.array([s.t for s in samples], dtype=np.float64)
    xy = np.array([s.world for s in samples], dtype=np.float64)
    t = t - t[0]

    # Cumulative arc length along the traversed path.
    seg = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    total_dist = float(s[-1])
    duration = float(t[-1])

    if total_dist < cfg["min_track_distance_m"] or duration <= 0:
        return None

    # Regression speed: slope of s vs t.
    A = np.vstack([t, np.ones_like(t)]).T
    slope, _ = np.linalg.lstsq(A, s, rcond=None)[0]
    v_reg = abs(float(slope))

    # Robust cross-check: median instantaneous.
    dt = np.diff(t)
    good = dt > 1e-6
    v_med = float(np.median(seg[good] / dt[good])) if np.any(good) else v_reg

    v = v_reg
    confidence = "ok"
    if v_med > 0 and (max(v_reg, v_med) / max(min(v_reg, v_med), 1e-6)) > 1.5:
        confidence = "low"
        v = min(v_reg, v_med)  # be conservative when estimators disagree

    kmh = v * KMH_PER_MS
    if kmh < cfg["min_speed_kmh"] or kmh > cfg["max_speed_kmh"]:
        return None

    # Direction: sign of net displacement along the dominant world axis.
    net = xy[-1] - xy[0]
    dominant = net[0] if abs(net[0]) >= abs(net[1]) else net[1]
    direction = (
        cfg["direction_positive"] if dominant >= 0 else cfg["direction_negative"]
    )

    return SpeedResult(
        speed_kmh=kmh,
        speed_mph=v * MPH_PER_MS,
        direction=direction,
        distance_m=total_dist,
        duration_s=duration,
        n_samples=len(samples),
        confidence=confidence,
        peak_index=len(samples) // 2,
    )
