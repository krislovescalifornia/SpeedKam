# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Car-only gating: the pixel-only checks that reject the pedestrians, cyclists,
dogs and wind-blown foliage a side-on camera would otherwise time -- car shape
(aspect ratio), car size (bbox width), area coherence (flicker) -- plus the
count-once-per-drive-by dedupe."""
import time
from dataclasses import dataclass

from speedkam.pipeline import SpeedCamera
from speedkam.tracker import Sample, Track


@dataclass
class R:
    distance_m: float = 6.0
    direction: str = "Eastbound"


class FakeState:
    def __init__(self, d):
        self.d = dict(d)

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


def _cam(**thresholds):
    base = {"min_vehicle_aspect": 1.4, "min_car_width_px": 200.0,
            "max_area_cv": 0.90, "dedupe_seconds": 3.0}
    base.update(thresholds)
    cam = SpeedCamera.__new__(SpeedCamera)
    cam.state = FakeState(base)
    cam._last_count = None
    return cam


def _track(boxes):
    return Track(id=1, samples=[Sample(t=i * 0.1, ground_px=(0, 0), bbox=b)
                                for i, b in enumerate(boxes)])


# ------------------------------------------------------------- aspect ratio
def test_aspect_ratio_median():
    # wide car boxes -> ~2.5
    t = _track([(0, 0, 200, 80), (0, 0, 250, 100), (0, 0, 240, 96)])
    assert round(SpeedCamera._aspect_ratio(t), 1) == 2.5


def test_wide_car_shape_passes():
    cam = _cam()
    t = _track([(0, 0, 300, 120)] * 5)   # car-shaped AND wide enough
    status, _ = cam._classify_reading(
        R(), aspect=SpeedCamera._aspect_ratio(t),
        width_px=SpeedCamera._vehicle_width_px(t))
    assert status == "ok"


def test_tall_pedestrian_shape_rejected():
    cam = _cam()
    # a walking person's box is tall (w:h ~0.4) -- rejected on shape.
    t = _track([(0, 0, 60, 170), (0, 0, 64, 175)])
    status, reason = cam._classify_reading(
        R(distance_m=8.0), aspect=SpeedCamera._aspect_ratio(t),
        width_px=SpeedCamera._vehicle_width_px(t))
    assert status == "rejected"
    assert "taller than a car" in reason


def test_aspect_gate_disabled_when_zero():
    cam = _cam(min_vehicle_aspect=0, min_car_width_px=0)
    t = _track([(0, 0, 60, 170)])
    status, _ = cam._classify_reading(
        R(), aspect=SpeedCamera._aspect_ratio(t),
        width_px=SpeedCamera._vehicle_width_px(t))
    assert status == "ok"


# --------------------------------------------------------------- car width
def test_vehicle_width_helper_median():
    t = _track([(0, 0, 200, 80), (0, 0, 260, 90), (0, 0, 240, 88)])
    assert SpeedCamera._vehicle_width_px(t) == 240.0


def test_narrow_object_rejected_on_width():
    cam = _cam()
    # A wide-enough SHAPE (square-ish) but only 120px across -- a dog or a bike,
    # not a car. Aspect passes; the size gate catches it.
    t = _track([(0, 0, 120, 60)] * 5)       # aspect 2.0 (>1.4), width 120 (<200)
    status, reason = cam._classify_reading(
        R(), aspect=SpeedCamera._aspect_ratio(t),
        width_px=SpeedCamera._vehicle_width_px(t))
    assert status == "rejected"
    assert "smaller than a car" in reason


def test_width_gate_disabled_when_zero():
    cam = _cam(min_car_width_px=0)
    t = _track([(0, 0, 120, 60)] * 5)
    status, _ = cam._classify_reading(
        R(), aspect=SpeedCamera._aspect_ratio(t),
        width_px=SpeedCamera._vehicle_width_px(t))
    assert status == "ok"


# ------------------------------------------------------- area-coherence gate
def test_area_cv_helper_smooth_vs_flicker():
    smooth = _track([(0, 0, 200, 100), (0, 0, 210, 100), (0, 0, 220, 100)])
    flicker = _track([(0, 0, 40, 40), (0, 0, 300, 200), (0, 0, 30, 30)])
    assert SpeedCamera._area_cv(smooth) < 0.2
    assert SpeedCamera._area_cv(flicker) > 0.9


def test_flickering_blob_rejected():
    cam = _cam()
    t = _track([(0, 0, 300, 120)] * 5)
    status, reason = cam._classify_reading(
        R(), aspect=SpeedCamera._aspect_ratio(t),
        width_px=SpeedCamera._vehicle_width_px(t), area_cv=1.4)
    assert status == "rejected"
    assert "flickered" in reason


def test_smooth_area_accepted():
    cam = _cam()
    t = _track([(0, 0, 300, 120)] * 5)
    status, _ = cam._classify_reading(
        R(), aspect=SpeedCamera._aspect_ratio(t),
        width_px=SpeedCamera._vehicle_width_px(t), area_cv=0.2)
    assert status == "ok"


def test_area_cv_gate_disabled_when_zero():
    cam = _cam(max_area_cv=0)
    t = _track([(0, 0, 300, 120)] * 5)
    status, _ = cam._classify_reading(
        R(), aspect=SpeedCamera._aspect_ratio(t),
        width_px=SpeedCamera._vehicle_width_px(t), area_cv=5.0)
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
