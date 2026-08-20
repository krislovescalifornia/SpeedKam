# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Drive-by auto-calibration: turn known-speed passes into a homography.

The manual flow (tools/calibrate.py, /calibrate) asks you to click road points
and tape-measure their metric X/Y. This module offers an alternative that needs
no tape measure: drive past the camera a few times at a *known, steady* speed
and let the geometry fall out.

The core identity is

        distance travelled = speed x elapsed time.

The tracker already records each vehicle's ground-contact point in pixels at
every frame (tracker.Sample.ground_px + .t). For a pass held at a constant known
speed v, the metre position ALONG the road of the pixel the car occupies at time
t is simply v * (t - t_ref). So every frame of the trail becomes one
pixel -> metres correspondence -- exactly what findHomography consumes.

Two things make the passes agree on a single world frame:

  * A shared origin cross-section. We pick one image column ``u_ref`` that every
    pass crosses and call the physical road cross-section under it X = 0. Each
    pass is anchored by the time it crosses u_ref, so a pixel maps to the same X
    no matter which pass (or which SPEED) produced it -- because
    v * (t - t_ref) reconstructs the pixel's true distance from that cross-section
    regardless of v. Passes at different speeds reinforce each other.

  * The across-road axis. One straight pass yields points on a single world line
    (constant Y) -- collinear, and findHomography rejects that (see
    calibration.py). So we require passes in at least TWO lanes; each lane is
    given a distinct Y = lane_index * lane_width. The lane width is only a rough
    metric scale for the across-road axis, which barely affects speed (that rides
    on X); its real job is to make the points non-collinear so a homography
    exists at all.

The output is an ``(image_points, world_points)`` pair fed straight into the
existing Calibration/recalibrate path -- no new calibration format, no changes
to the speed estimator.
"""
from __future__ import annotations

DEFAULT_LANE_WIDTH_M = 3.66      # ~12 ft, a typical residential travel lane
_MIN_TRAIL = 3                   # samples needed to define a pass
_MIN_PIXEL_SWEEP = 40.0          # a pass must move at least this many px in u
_MAX_POINTS_PER_PASS = 40        # downsample long trails so no pass dominates


class DriveByError(ValueError):
    """Raised when the recorded passes can't yield a valid homography."""


def _sweep(trail):
    us = [u for _, u, _ in trail]
    return max(us) - min(us)


def _downsample(trail, n):
    """Evenly thin a trail to at most n samples, keeping first and last."""
    if len(trail) <= n:
        return trail
    step = (len(trail) - 1) / (n - 1)
    idx = sorted({round(i * step) for i in range(n)})
    return [trail[i] for i in idx]


def _crossing_time(trail, u_ref):
    """Time at which the trail's u-coordinate crosses u_ref (linear interp).

    Returns None if the trail never crosses the column (u_ref outside its span).
    """
    for (t0, u0, _), (t1, u1, _) in zip(trail, trail[1:]):
        d0, d1 = u0 - u_ref, u1 - u_ref
        if d0 == 0.0:
            return t0
        if d0 * d1 < 0.0:                       # straddles the column
            frac = (u_ref - u0) / (u1 - u0)
            return t0 + frac * (t1 - t0)
    return None


def _prepare(passes):
    """Validate, sort, and downsample raw passes. Returns the usable ones."""
    prepared = []
    for p in passes:
        trail = sorted(p.get("trail", []), key=lambda s: s[0])
        if len(trail) < _MIN_TRAIL:
            continue
        speed = float(p.get("speed_mps") or 0.0)
        if speed <= 0.0:
            continue
        if _sweep(trail) < _MIN_PIXEL_SWEEP:    # car barely moved / parked blob
            continue
        prepared.append({
            "trail": _downsample(trail, _MAX_POINTS_PER_PASS),
            "speed_mps": speed,
            "lane": int(p.get("lane", 0)),
        })
    return prepared


def _pick_u_ref(prepared, frame_w):
    """A column crossed by every pass (mid of the shared u-overlap).

    Falls back to the frame centre if the passes share no common column, which
    for cars traversing a side-on frame effectively never happens.
    """
    lo = max(min(u for _, u, _ in p["trail"]) for p in prepared)
    hi = min(max(u for _, u, _ in p["trail"]) for p in prepared)
    if lo <= hi:
        return (lo + hi) / 2.0
    return (frame_w or 1280) / 2.0


def build_correspondences(passes, frame_wh, lane_width_m=DEFAULT_LANE_WIDTH_M):
    """Convert known-speed passes into (image_points, world_points, info).

    passes -- list of dicts, each:
        {"trail": [(t, u, v), ...],   # ground-point pixel trail, any order
         "speed_mps": float,          # the steady speed the pass was driven at
         "lane": int}                 # lane index; >=2 distinct values required

    Raises DriveByError with a plain-language reason if the passes can't produce
    a non-degenerate homography (too few usable passes, or only one lane).
    """
    frame_w = (frame_wh or (1280, 720))[0]
    prepared = _prepare(passes)
    if len(prepared) < 2:
        raise DriveByError(
            "Need at least 2 usable passes (each a steady drive-by that crosses "
            "the frame). Record more.")

    lanes = {p["lane"] for p in prepared}
    if len(lanes) < 2:
        raise DriveByError(
            "All passes are in one lane, so every point lies on a single line "
            "and no homography exists. Drive at least one pass in a second lane "
            "(e.g. the opposite direction) and record it as a different lane.")

    u_ref = _pick_u_ref(prepared, frame_w)
    image_points, world_points = [], []
    per_pass = []
    for p in prepared:
        trail = p["trail"]
        t_ref = _crossing_time(trail, u_ref)
        if t_ref is None:                       # shouldn't happen post _pick_u_ref
            t_ref = trail[len(trail) // 2][0]
        # Travel direction in pixels -> sign that makes X grow to image-right.
        dir_sign = 1.0 if trail[-1][1] >= trail[0][1] else -1.0
        y = p["lane"] * lane_width_m
        for t, u, v in trail:
            x = dir_sign * p["speed_mps"] * (t - t_ref)
            image_points.append([float(u), float(v)])
            world_points.append([x, y])
        per_pass.append({"lane": p["lane"], "n": len(trail),
                         "speed_mps": p["speed_mps"]})

    info = {
        "u_ref": round(u_ref, 1),
        "n_passes": len(prepared),
        "n_points": len(image_points),
        "lanes": sorted(lanes),
        "lane_width_m": lane_width_m,
        "passes": per_pass,
    }
    return image_points, world_points, info
