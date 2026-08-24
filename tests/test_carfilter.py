# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Car-only gating: the shape (aspect-ratio) check that rejects pedestrians and
cyclists a side-on camera would otherwise time, and the count-once-per-drive-by
dedupe."""
import time
from dataclasses import dataclass

from speedkam.pipeline import SpeedCamera
from speedkam.tracker import Sample, Track


@dataclass
class R:
    distance_m: float = 6.0
    direction: str = "Eastbound"
    straightness: float = 1.0
    monotonicity: float = 1.0
    max_accel_mps2: float = 0.0


class FakeState:
    def __init__(self, d):
        self.d = dict(d)

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


def _cam(**thresholds):
    base = {"max_track_distance_m": 45.0, "min_vehicle_span_m": 1.0,
            "min_vehicle_aspect": 1.1, "dedupe_seconds": 3.0,
            "min_on_road_frac": 0.6, "min_straightness": 0.80,
            "max_area_cv": 0.90}
    base.update(thresholds)
    cam = SpeedCamera.__new__(SpeedCamera)
    cam.state = FakeState(base)
    cam._last_count = None
    return cam


def _track(boxes):
    return Track(id=1, samples=[Sample(t=i * 0.1, ground_px=(0, 0), world=(0, 0),
                                       bbox=b) for i, b in enumerate(boxes)])


# ------------------------------------------------------------- aspect ratio
def test_aspect_ratio_median():
    # wide car boxes -> ~2.5
    t = _track([(0, 0, 200, 80), (0, 0, 250, 100), (0, 0, 240, 96)])
    assert round(SpeedCamera._aspect_ratio(t), 1) == 2.5


def test_wide_car_shape_passes():
    cam = _cam()
    # a car-shaped box (w:h 2.5) with a real footprint clears every gate
    t = _track([(0, 0, 200, 80)] * 5)
    status, _ = cam._classify_reading(R(distance_m=6.0), span_m=2.0,
                                      aspect=SpeedCamera._aspect_ratio(t))
    assert status == "ok"


def test_tall_pedestrian_shape_rejected():
    cam = _cam()
    # a walking person's box is tall (w:h ~0.4) -- rejected even with a large
    # (homography-inflated) span that would otherwise pass the size gate.
    t = _track([(0, 0, 60, 170), (0, 0, 64, 175)])
    status, reason = cam._classify_reading(R(distance_m=8.0), span_m=3.0,
                                           aspect=SpeedCamera._aspect_ratio(t))
    assert status == "rejected"
    assert "taller than a car" in reason


def test_aspect_gate_disabled_when_zero():
    cam = _cam(min_vehicle_aspect=0)
    t = _track([(0, 0, 60, 170)])
    status, _ = cam._classify_reading(R(distance_m=6.0), span_m=2.0,
                                      aspect=SpeedCamera._aspect_ratio(t))
    assert status == "ok"


# ------------------------------------------------------- road-region gate
def test_off_road_foreground_rejected():
    cam = _cam()
    # Car-shaped, real footprint, plausible distance -- but the track was mostly
    # in the foreground (the two-kids-on-the-lawn failure). Rejected on location
    # alone, regardless of shape.
    t = _track([(0, 0, 200, 80)] * 5)
    status, reason = cam._classify_reading(
        R(distance_m=6.0), span_m=2.0,
        aspect=SpeedCamera._aspect_ratio(t), on_road_frac=0.15)
    assert status == "rejected"
    assert "off the calibrated road" in reason


def test_on_road_car_passes():
    cam = _cam()
    t = _track([(0, 0, 200, 80)] * 5)
    status, _ = cam._classify_reading(
        R(distance_m=6.0), span_m=2.0,
        aspect=SpeedCamera._aspect_ratio(t), on_road_frac=0.95)
    assert status == "ok"


def test_road_gate_skipped_when_uncalibrated():
    cam = _cam()
    # on_road_frac None (no calibration -> no road plane) must not reject.
    t = _track([(0, 0, 200, 80)] * 5)
    status, _ = cam._classify_reading(
        R(distance_m=6.0), span_m=2.0,
        aspect=SpeedCamera._aspect_ratio(t), on_road_frac=None)
    assert status == "ok"


def test_road_gate_disabled_when_zero():
    cam = _cam(min_on_road_frac=0)
    t = _track([(0, 0, 200, 80)] * 5)
    status, _ = cam._classify_reading(
        R(distance_m=6.0), span_m=2.0,
        aspect=SpeedCamera._aspect_ratio(t), on_road_frac=0.0)
    assert status == "ok"


# ------------------------------------------------------- straightness gate
def test_wandering_track_rejected():
    cam = _cam()
    # Car-shaped and car-sized, but the world path wandered: net displacement was
    # only 40% of the traversed length -- the wind-in-the-trees / noise-blob
    # signature. Rejected on motion physics alone, whatever the blob's shape.
    t = _track([(0, 0, 200, 80)] * 5)
    status, reason = cam._classify_reading(
        R(distance_m=6.0, straightness=0.40), span_m=2.0,
        aspect=SpeedCamera._aspect_ratio(t))
    assert status == "rejected"
    assert "path wandered" in reason


def test_straight_pass_accepted():
    cam = _cam()
    t = _track([(0, 0, 200, 80)] * 5)
    status, _ = cam._classify_reading(
        R(distance_m=6.0, straightness=0.98), span_m=2.0,
        aspect=SpeedCamera._aspect_ratio(t))
    assert status == "ok"


def test_straightness_gate_disabled_when_zero():
    cam = _cam(min_straightness=0)
    t = _track([(0, 0, 200, 80)] * 5)
    status, _ = cam._classify_reading(
        R(distance_m=6.0, straightness=0.05), span_m=2.0,
        aspect=SpeedCamera._aspect_ratio(t))
    assert status == "ok"


# ------------------------------------------------------- monotonicity gate
def test_reversing_track_rejected():
    cam = _cam(min_monotonicity=0.75)
    # Car-shaped, car-sized, plausibly straight -- but only half its steps moved
    # forward: it reversed repeatedly, the swaying-foliage signature.
    t = _track([(0, 0, 200, 80)] * 5)
    status, reason = cam._classify_reading(
        R(distance_m=6.0, monotonicity=0.5), span_m=2.0,
        aspect=SpeedCamera._aspect_ratio(t))
    assert status == "rejected"
    assert "path reversed" in reason


def test_forward_track_accepted():
    cam = _cam(min_monotonicity=0.75)
    t = _track([(0, 0, 200, 80)] * 5)
    status, _ = cam._classify_reading(
        R(distance_m=6.0, monotonicity=1.0), span_m=2.0,
        aspect=SpeedCamera._aspect_ratio(t))
    assert status == "ok"


def test_monotonicity_gate_disabled_by_default():
    # min_monotonicity defaults to 0 (off) in the base _cam thresholds, so even a
    # heavily reversing track is not rejected on this signal.
    cam = _cam()
    t = _track([(0, 0, 200, 80)] * 5)
    status, _ = cam._classify_reading(
        R(distance_m=6.0, monotonicity=0.1), span_m=2.0,
        aspect=SpeedCamera._aspect_ratio(t))
    assert status == "ok"


# ------------------------------------------------------- acceleration gate
def test_teleporting_track_rejected():
    cam = _cam(max_accel_mps2=20.0)
    t = _track([(0, 0, 200, 80)] * 5)
    status, reason = cam._classify_reading(
        R(distance_m=6.0, max_accel_mps2=250.0), span_m=2.0,
        aspect=SpeedCamera._aspect_ratio(t))
    assert status == "rejected"
    assert "impossible acceleration" in reason


def test_smooth_speed_accepted():
    cam = _cam(max_accel_mps2=20.0)
    t = _track([(0, 0, 200, 80)] * 5)
    status, _ = cam._classify_reading(
        R(distance_m=6.0, max_accel_mps2=5.0), span_m=2.0,
        aspect=SpeedCamera._aspect_ratio(t))
    assert status == "ok"


def test_accel_gate_disabled_by_default():
    cam = _cam()
    t = _track([(0, 0, 200, 80)] * 5)
    status, _ = cam._classify_reading(
        R(distance_m=6.0, max_accel_mps2=9999.0), span_m=2.0,
        aspect=SpeedCamera._aspect_ratio(t))
    assert status == "ok"


# ------------------------------------------------------- area-coherence gate
def test_area_cv_helper_smooth_vs_flicker():
    smooth = _track([(0, 0, 200, 100), (0, 0, 210, 100), (0, 0, 220, 100)])
    flicker = _track([(0, 0, 40, 40), (0, 0, 300, 200), (0, 0, 30, 30)])
    assert SpeedCamera._area_cv(smooth) < 0.2
    assert SpeedCamera._area_cv(flicker) > 0.9


def test_flickering_blob_rejected():
    cam = _cam()
    t = _track([(0, 0, 200, 80)] * 5)
    status, reason = cam._classify_reading(
        R(distance_m=6.0), span_m=2.0,
        aspect=SpeedCamera._aspect_ratio(t), area_cv=1.4)
    assert status == "rejected"
    assert "flickered" in reason


def test_smooth_area_accepted():
    cam = _cam()
    t = _track([(0, 0, 200, 80)] * 5)
    status, _ = cam._classify_reading(
        R(distance_m=6.0), span_m=2.0,
        aspect=SpeedCamera._aspect_ratio(t), area_cv=0.2)
    assert status == "ok"


def test_area_cv_gate_disabled_when_zero():
    cam = _cam(max_area_cv=0)
    t = _track([(0, 0, 200, 80)] * 5)
    status, _ = cam._classify_reading(
        R(distance_m=6.0), span_m=2.0,
        aspect=SpeedCamera._aspect_ratio(t), area_cv=5.0)
    assert status == "ok"


# ------------------------------------------------------------- dedupe
def test_duplicate_same_direction_within_window():
    cam = _cam(dedupe_seconds=3.0)
    cam._last_count = {"t": time.monotonic(), "direction": "Eastbound",
                       "speed_kmh": 40.0}
    reason = cam._is_duplicate(R(direction="Eastbound"))
    assert reason and "duplicate" in reason


def test_not_duplicate_opposite_direction():
    cam = _cam(dedupe_seconds=3.0)
    cam._last_count = {"t": time.monotonic(), "direction": "Eastbound",
                       "speed_kmh": 40.0}
    assert cam._is_duplicate(R(direction="Westbound")) is None


def test_not_duplicate_after_window():
    cam = _cam(dedupe_seconds=3.0)
    cam._last_count = {"t": time.monotonic() - 10.0, "direction": "Eastbound",
                       "speed_kmh": 40.0}
    assert cam._is_duplicate(R(direction="Eastbound")) is None


def test_dedupe_disabled_when_zero():
    cam = _cam(dedupe_seconds=0)
    cam._last_count = {"t": time.monotonic(), "direction": "Eastbound",
                       "speed_kmh": 40.0}
    assert cam._is_duplicate(R(direction="Eastbound")) is None
