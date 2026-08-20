# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Drive-by calibration: known-speed passes must reconstruct a homography that
then reads back the correct speed.

We fake a side-on camera with a synthetic world->image homography, generate
constant-speed passes through it, run build_correspondences to recover
pixel->world pairs, fit a Calibration, and check that a held-out pass estimates
its true speed. This exercises the whole speed-from-time-and-known-speed idea
end to end without a real camera.
"""
import numpy as np
import pytest

from speedkam.calibration import Calibration
from speedkam import driveby, speed as speed_mod
from speedkam.tracker import Sample, Track

FRAME_WH = (1280, 720)

# A synthetic ground-plane homography mapping WORLD (metres) -> IMAGE (pixels),
# with real perspective foreshortening (the h[2] row is non-zero) so the test
# isn't a trivial affine scale. Chosen to keep a ~[0..20 m] x [0..8 m] road
# comfortably inside a 1280x720 frame.
H_W2I = np.array([
    [42.0,  6.0, 210.0],
    [ 0.0, 30.0, 470.0],
    [ 0.0, 0.0009, 1.0],
], dtype=np.float64)


def world_to_image(x, y):
    v = H_W2I @ np.array([x, y, 1.0])
    return v[0] / v[2], v[1] / v[2]


def make_pass(speed_mps, lane, lane_width_m, direction=+1, fps=30.0,
              x_from=-9.0, x_to=9.0, t0=100.0):
    """A constant-speed pass down one lane, as a (t, u, v) pixel trail.

    The car moves through world X at `speed_mps`; each frame we project its
    ground point to pixels. `direction` flips travel sense (X descending).
    """
    y = lane * lane_width_m
    span = abs(x_to - x_from)
    dur = span / speed_mps
    n = max(3, int(dur * fps))
    trail = []
    for i in range(n + 1):
        t = t0 + i / fps
        travelled = speed_mps * (t - t0)
        x = (x_from + travelled) if direction > 0 else (x_to - travelled)
        u, vpx = world_to_image(x, y)
        trail.append((t, u, vpx))
    return {"trail": trail, "speed_mps": speed_mps, "lane": lane}


def _track_from_pass(p):
    return Track(id=1, samples=[Sample(t, (u, vpx), None, (0, 0, 0, 0))
                                for t, u, vpx in p["trail"]], confirmed=True)


SPEED_CFG = {
    "min_samples": 4, "min_track_distance_m": 1.0,
    "min_speed_kmh": 1, "max_speed_kmh": 300,
    "direction_positive": "out", "direction_negative": "in",
    "measure_band": {"enabled": False},
}


def test_two_lane_passes_recover_speed():
    lane_w = 3.66
    passes = [
        make_pass(13.4, 0, lane_w, direction=+1),   # ~30 mph, lane 1, rightward
        make_pass(13.4, 0, lane_w, direction=-1),   # same lane, other way
        make_pass(13.4, 1, lane_w, direction=+1),   # lane 2 -> second Y
    ]
    img, world, info = driveby.build_correspondences(passes, FRAME_WH, lane_w)
    assert info["n_passes"] == 3
    assert len(info["lanes"]) == 2

    calib = Calibration(img, world)
    assert calib.reprojection_error() < 0.25

    # Held-out pass at a DIFFERENT speed; the calibration must read it back.
    test_speed = 8.9  # ~20 mph
    tp = make_pass(test_speed, 0, lane_w, direction=+1)
    track = _track_from_pass(tp)
    for s in track.samples:
        s.world = tuple(calib.image_to_world([list(s.ground_px)])[0])
    result = speed_mod.estimate(track, SPEED_CFG, FRAME_WH)
    assert result is not None
    assert result.speed_kmh == pytest.approx(test_speed * 3.6, rel=0.06)


def test_single_lane_is_rejected():
    passes = [make_pass(13.4, 0, 3.66, direction=+1),
              make_pass(13.4, 0, 3.66, direction=-1)]
    with pytest.raises(driveby.DriveByError):
        driveby.build_correspondences(passes, FRAME_WH, 3.66)


def test_too_few_passes_is_rejected():
    with pytest.raises(driveby.DriveByError):
        driveby.build_correspondences(
            [make_pass(13.4, 0, 3.66)], FRAME_WH, 3.66)


def test_different_speeds_are_mutually_consistent():
    """Passes at different speeds in the same lane must agree on X: the fitted
    world X of a pixel should be independent of the speed used to record it."""
    lane_w = 3.66
    passes = [
        make_pass(13.4, 0, lane_w, direction=+1),
        make_pass(6.7, 0, lane_w, direction=+1),    # half speed, same lane
        make_pass(13.4, 1, lane_w, direction=+1),
    ]
    img, world, _ = driveby.build_correspondences(passes, FRAME_WH, lane_w)
    calib = Calibration(img, world)
    # Low reprojection error only holds if the two same-lane, different-speed
    # passes produced consistent world coordinates for overlapping pixels.
    assert calib.reprojection_error() < 0.25


def test_parked_blob_is_ignored():
    """A near-stationary 'pass' (tiny pixel sweep) is dropped, not fitted."""
    lane_w = 3.66
    good = [make_pass(13.4, 0, lane_w, direction=+1),
            make_pass(13.4, 1, lane_w, direction=+1)]
    # A blob that barely moves: a few frames clustered at one pixel column.
    parked = {"trail": [(50.0 + i / 30.0, 640.0 + i * 0.1, 500.0)
                        for i in range(10)],
              "speed_mps": 13.4, "lane": 0}
    img, world, info = driveby.build_correspondences(
        good + [parked], FRAME_WH, lane_w)
    assert info["n_passes"] == 2  # parked one filtered out
