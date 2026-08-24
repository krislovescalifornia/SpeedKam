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


def estimate(track, cfg, frame_wh=None, orientation=None) -> SpeedResult | None:
    samples = [s for s in track.samples if s.world is not None]
    band = resolve_band(cfg.get("measure_band"), orientation)
    samples = _in_band(samples, band, frame_wh)
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

    # Reported speed: Theil-Sen slope of cumulative path length s vs time t --
    # the median of all pairwise slopes. Outlier-robust and, unlike the old
    # min(regression, median) rule, free of any systematic low bias.
    v = _theil_sen_slope(t, s)

    # Confidence cross-check ONLY: median per-segment instantaneous speed. If it
    # disagrees wildly with Theil-Sen the pass is flagged low-confidence, but the
    # reported value is never pulled toward the smaller estimator.
    dt = np.diff(t)
    good = dt > 1e-6
    v_med = float(np.median(seg[good] / dt[good])) if np.any(good) else v
    confidence = "ok"
    if v_med > 0 and v > 0 and (max(v, v_med) / max(min(v, v_med), 1e-6)) > 1.5:
        confidence = "low"

    kmh = v * KMH_PER_MS
    if kmh < cfg["min_speed_kmh"] or kmh > cfg["max_speed_kmh"]:
        return None

    # Direction: sign of net displacement along the dominant world axis.
    net = xy[-1] - xy[0]
    dominant = net[0] if abs(net[0]) >= abs(net[1]) else net[1]
    direction = (
        cfg["direction_positive"] if dominant >= 0 else cfg["direction_negative"]
    )

    # Straightness: net (straight-line) displacement over the traversed path
    # length. ~1.0 for a real vehicle; low for wandering foliage/noise. total_dist
    # is >= min_track_distance_m here (we returned above otherwise), so the ratio
    # is well-conditioned.
    net_disp = float(np.linalg.norm(net))
    straightness = net_disp / total_dist if total_dist > 1e-6 else 1.0

    # Monotonic progress: project each world point onto the net-travel direction
    # and measure the fraction of steps that advance forward along it. A real
    # vehicle never reverses (~1.0); swaying foliage / stitched noise oscillates.
    if net_disp > 1e-6 and len(xy) >= 2:
        u = net / net_disp
        proj = xy @ u
        dproj = np.diff(proj)
        monotonicity = float(np.mean(dproj > 0)) if dproj.size else 1.0
    else:
        monotonicity = 1.0

    # Peak frame-to-frame acceleration: differentiate the per-segment
    # instantaneous speed. A real vehicle changes speed smoothly; a phantom that
    # jumps between noise blobs spikes this. 0.0 when there aren't two good
    # segments to difference.
    max_accel = 0.0
    if np.count_nonzero(good) >= 2:
        v_inst = seg[good] / dt[good]
        dt_good = dt[good]
        dv = np.diff(v_inst)
        acc = np.abs(dv) / np.maximum(dt_good[1:], 1e-6)
        max_accel = float(np.max(acc)) if acc.size else 0.0

    return SpeedResult(
        speed_kmh=kmh,
        speed_mph=v * MPH_PER_MS,
        direction=direction,
        distance_m=total_dist,
        duration_s=duration,
        n_samples=len(samples),
        confidence=confidence,
        peak_index=len(samples) // 2,
        straightness=straightness,
        monotonicity=monotonicity,
        max_accel_mps2=max_accel,
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
