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


class FakeState:
    def __init__(self, d):
        self.d = dict(d)

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


def _cam(**thresholds):
    base = {"max_track_distance_m": 45.0, "min_vehicle_span_m": 1.0,
            "min_vehicle_aspect": 1.1, "dedupe_seconds": 3.0}
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
