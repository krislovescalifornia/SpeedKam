# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Speed estimation: a synthetic constant-velocity track must read back the
speed we put in, and the plausibility gates must reject bad tracks."""
import pytest

from speedkam.speed import (
    KMH_PER_MS,
    MPH_PER_MS,
    estimate,
    normalize_orientation,
    resolve_band,
    _in_band,
)
from speedkam.tracker import Sample, Track

# Minimal `speed` config section, matching config.DEFAULTS["speed"] keys that
# estimate() reads. No measure_band -> gating is a no-op.
BASE_CFG = {
    "min_track_distance_m": 3.0,
    "min_samples": 6,
    "min_speed_kmh": 3,
    "max_speed_kmh": 200,
    "direction_positive": "outbound",
    "direction_negative": "inbound",
}


def make_track(velocity_mps, n=12, dt=0.1, axis="x", sign=1):
    """A car moving in a straight line at a constant speed along one world axis.

    Ground pixels advance too (kept central so any band gate would keep them).
    """
    samples = []
    for i in range(n):
        t = i * dt
        d = sign * velocity_mps * t          # metres travelled along the axis
        world = (d, 0.0) if axis == "x" else (0.0, d)
        px = (500 + i, 500 + i)              # near frame centre, monotonic
        samples.append(Sample(t=t, ground_px=px, world=world, bbox=(0, 0, 10, 10)))
    return Track(id=1, samples=samples)


def test_constant_velocity_reads_back_exact_speed():
    # 10 m/s == 36.0 km/h == 22.369 mph
    r = estimate(make_track(10.0), BASE_CFG)
    assert r is not None
    assert r.speed_kmh == pytest.approx(36.0, abs=1e-6)
    assert r.speed_mph == pytest.approx(10.0 * MPH_PER_MS, abs=1e-6)
    assert r.confidence == "ok"
    assert r.direction == "outbound"
    assert r.n_samples == 12
    assert r.distance_m == pytest.approx(10.0 * 1.1, abs=1e-6)  # v * duration


def test_kmh_mph_are_consistent():
    r = estimate(make_track(15.0), BASE_CFG)
    assert r.speed_mph / r.speed_kmh == pytest.approx(MPH_PER_MS / KMH_PER_MS, rel=1e-9)


@pytest.mark.parametrize("sign,expected", [(1, "outbound"), (-1, "inbound")])
def test_direction_follows_net_displacement(sign, expected):
    r = estimate(make_track(12.0, sign=sign), BASE_CFG)
    assert r is not None
    assert r.direction == expected
    assert r.speed_kmh == pytest.approx(12.0 * KMH_PER_MS, abs=1e-6)  # sign-independent


def test_too_few_samples_returns_none():
    assert estimate(make_track(10.0, n=4), BASE_CFG) is None


def test_too_short_distance_returns_none():
    # 1 m/s over 1.1 s == 1.1 m travelled, below min_track_distance_m (3.0)
    assert estimate(make_track(1.0), BASE_CFG) is None


def test_implausibly_fast_is_rejected():
    # 100 m/s == 360 km/h, above max_speed_kmh (200)
    assert estimate(make_track(100.0), BASE_CFG) is None


def test_below_min_speed_is_rejected():
    cfg = {**BASE_CFG, "min_track_distance_m": 0.0, "min_speed_kmh": 20}
    # 2 m/s == 7.2 km/h, below the 20 km/h floor
    assert estimate(make_track(2.0), cfg) is None


# --------------------------------------------------------------- band gating
def test_normalize_orientation():
    for v in ("head_on", "head-on", "HEADON", "oncoming", " Head On "):
        assert normalize_orientation(v) == "head_on"
    for v in ("parallel", "side_on", "", None, "whatever"):
        assert normalize_orientation(v) == "parallel"


def test_resolve_band_picks_preset_for_orientation():
    nested = {
        "enabled": True,
        "orientation": "parallel",
        "parallel": {"x_min": 0.3, "x_max": 0.7, "y_min": 0.0, "y_max": 1.0},
        "head_on": {"x_min": 0.0, "x_max": 1.0, "y_min": 0.55, "y_max": 0.95},
    }
    p = resolve_band(nested, "parallel")
    assert p["enabled"] and p["x_min"] == 0.3 and p["x_max"] == 0.7
    h = resolve_band(nested, "head_on")
    assert h["enabled"] and h["y_min"] == 0.55 and h["y_max"] == 0.95
    # falls back to the config's own orientation when none is passed
    assert resolve_band(nested)["x_min"] == 0.3


def test_resolve_band_passes_flat_band_through():
    flat = {"enabled": True, "x_min": 0.1, "x_max": 0.9, "y_min": 0.0, "y_max": 1.0}
    assert resolve_band(flat) == flat


def test_in_band_keeps_only_central_samples():
    band = {"enabled": True, "x_min": 0.3, "x_max": 0.7, "y_min": 0.0, "y_max": 1.0}
    samples = [Sample(0, (px, 500), None, (0, 0, 0, 0)) for px in (100, 400, 650, 900)]
    kept = _in_band(samples, band, (1000, 1000))
    assert [s.ground_px[0] for s in kept] == [400, 650]  # 100 & 900 trimmed


def test_in_band_disabled_is_a_noop():
    samples = [Sample(0, (px, 500), None, (0, 0, 0, 0)) for px in (0, 999)]
    assert _in_band(samples, {"enabled": False}, (1000, 1000)) == samples
    assert _in_band(samples, {"enabled": True}, None) == samples  # no frame size
