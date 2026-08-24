# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Speed estimation from a track's metric samples.

Given (t, X, Y) samples in real-world meters, estimate the vehicle's speed.

  * Theil-Sen slope: the median of the pairwise slopes of cumulative path
    length s(t). This is the reported speed. It is outlier-robust (a breakdown
    point of ~29%: up to ~a third of the samples can be corrupt before it
    moves), so a couple of bad frames don't drag the reading -- and crucially,
    unlike the old min(regression, median) heuristic, it has no systematic
    LOW bias (that heuristic always picked the smaller of two estimators on any
    disagreement, so noise could only ever pull the speed down).
  * Median instantaneous: median of per-segment (dist/dt). Kept purely as a
    confidence cross-check -- if it disagrees wildly with Theil-Sen the reading
    is flagged low-confidence, but it never changes the reported value.

Alongside the speed we compute three deterministic trajectory-quality signals
the false-positive gate keys on -- straightness, monotonicity, and peak
frame-to-frame acceleration -- so a phantom (a bug on the lens, wind-blown
foliage, a noise blob) is rejected on physics, with no classifier.
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
    # Trajectory-quality signal for the false-positive gate: the ratio of
    # straight-line (net) displacement to the total traversed path length,
    # measured in world meters. A real vehicle tracks in a near-straight line
    # along the road, so this is ~1.0; a blob stitched out of noise, a bug
    # crawling the lens, or foliage swaying in the wind wanders back and forth,
    # so its net displacement is only a fraction of its path length (-> 0). It
    # is invariant to blob shape and needs no classifier. 1.0 when undefined
    # (degenerate/zero-length path), so it never rejects by default.
    straightness: float = 1.0
    # Monotonic-progress signal: the fraction of steps whose motion advances
    # ALONG the net direction of travel (world coords, projected onto the pass's
    # own axis). A real vehicle only ever moves forward down the road, so this is
    # ~1.0; foliage swaying or a noise blob stitched into a track reverses often,
    # so it collapses toward ~0.5. Complements straightness: a path can be fairly
    # straight yet still oscillate along its axis. 1.0 when undefined.
    monotonicity: float = 1.0
    # Peak frame-to-frame acceleration magnitude (m/s^2) across the pass. A real
    # vehicle's speed changes smoothly (a few m/s^2); a phantom that teleports
    # between noise blobs implies an impossible 0->fast in one frame, spiking
    # this. 0.0 when undefined (too few segments to differentiate).
    max_accel_mps2: float = 0.0

    def display(self, units):
        val = self.speed_mph if units == "mph" else self.speed_kmh
        unit = "mph" if units == "mph" else "km/h"
        return f"{val:.0f} {unit}"


def normalize_orientation(value):
    """Map free-form orientation labels onto the two canonical preset keys."""
    v = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return "head_on" if v in ("head_on", "headon", "head", "oncoming") else "parallel"


def resolve_band(band_cfg, orientation=None):
    """Flatten the measure_band config into a single {enabled, x/y bounds} band.

    Accepts two shapes:
      * NESTED (config form): {enabled, orientation, parallel:{...}, head_on:{...}}
        -- pick the preset for `orientation` (or the config's own default).
      * FLAT (what the tuner passes): {enabled, x_min, x_max, ...} -- returned
        as-is, so a directly-specified band still works.
    """
    band_cfg = band_cfg or {}
    preset = band_cfg.get("parallel") or band_cfg.get("head_on")
    if preset is None:
        return band_cfg  # already a flat band
    key = normalize_orientation(orientation or band_cfg.get("orientation"))
    chosen = band_cfg.get(key) or {}
    return {"enabled": band_cfg.get("enabled", False), **chosen}


def _in_band(samples, band, frame_wh):
    """Keep only samples whose ground point lies inside the central band.

    The band is defined in FRACTIONS of frame width/height, so it is
    resolution-independent. For a side-on (camera parallel to the road) view
    the pixels->meters mapping is most trustworthy dead-centre; foreshortening
    and lens distortion grow toward the left/right edges, so timing a car only
    across the central band avoids those corrupt samples.
    """
    if not band.get("enabled") or not frame_wh:
        return samples
    w, h = frame_wh
    x_lo = band.get("x_min", 0.0) * w
    x_hi = band.get("x_max", 1.0) * w
    y_lo = band.get("y_min", 0.0) * h
    y_hi = band.get("y_max", 1.0) * h
    return [
        s for s in samples
        if x_lo <= s.ground_px[0] <= x_hi and y_lo <= s.ground_px[1] <= y_hi
    ]


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


def estimate(track, cfg, frame_wh=None, orientation=None) -> SpeedResult | None:
    """Two-line crossing-time speed. A car crossing between image columns ``x_a``
    and ``x_b`` at a KNOWN speed fixes the real distance of that stretch -- one
    number per travel direction (``d_east_m`` / ``d_west_m``). Every other car's
    speed is then that distance over its own crossing time. Uses raw pixel x and
    timestamps only -- no homography, no undistortion. In this camera's view
    eastbound runs right-to-left (x decreasing)."""
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
        peak_index=len(samples) // 2,
        straightness=1.0,      # trajectory-quality gates retired with the homography
        monotonicity=1.0,
        max_accel_mps2=0.0,
    )


def _theil_sen_slope(t, s):
    """Median of the pairwise slopes (s_j - s_i) / (t_j - t_i) for all i < j.

    The Theil-Sen estimator of the slope of s vs t: robust to outliers with no
    systematic bias. ``t`` is strictly increasing (frame timestamps) and ``s`` is
    the cumulative arc length, so every usable pair has a positive dt. Returns
    0.0 when no pair has a positive dt (degenerate). n is small (a single pass),
    so the O(n^2) pair enumeration is cheap.
    """
    n = len(t)
    if n < 2:
        return 0.0
    i, j = np.triu_indices(n, k=1)
    dt = t[j] - t[i]
    ok = dt > 1e-9
    if not np.any(ok):
        return 0.0
    slopes = (s[j] - s[i])[ok] / dt[ok]
    return abs(float(np.median(slopes)))
