# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Two-line crossing-time speed. A synthetic pass crossing the calibrated
columns at a known rate must read back the speed we put in, and the plausibility
gates must reject bad tracks."""
import pytest

from speedkam.speed import KMH_PER_MS, MPH_PER_MS, estimate, _crossing_seconds
from speedkam.tracker import Sample, Track

XA, XB = 1000, 450

# Minimal `speed` config, matching the config.DEFAULTS["speed"] keys estimate()
# reads. d_east_m / d_west_m are the calibrated distances of the x1000<->x450
# stretch for each direction.
BASE_CFG = {
    "min_samples": 6,
    "min_speed_kmh": 3,
    "max_speed_kmh": 200,
    "x_a": XA,
    "x_b": XB,
    "d_east_m": 5.0,
    "d_west_m": 4.0,
    "direction_positive": "Westbound",
    "direction_negative": "Eastbound",
}


def make_pass(direction="east", v_px=1100.0, n=16, dt=0.05):
    """A car crossing both calibrated columns at a constant pixel velocity.

    east  -> x decreasing (right-to-left): starts at 1100, crosses x_a=1000 then
             x_b=450. west -> x increasing: starts at 350, crosses x_b then x_a.
    With v_px=1100 the crossing time between the columns is exactly 550/1100 =
    0.5s, so speed = distance / 0.5.
    """
    if direction == "east":
        start, sign = 1100.0, -1.0
    else:
        start, sign = 350.0, 1.0
    samples = []
    for i in range(n):
        t = i * dt
        x = start + sign * v_px * t
        samples.append(Sample(t=t, ground_px=(x, 500), bbox=(0, 0, 20, 10)))
    return Track(id=1, samples=samples)


def test_eastbound_reads_back_exact_speed():
    # d_east_m=5.0 over a 0.5s crossing -> 10 m/s == 36 km/h.
    r = estimate(make_pass("east"), BASE_CFG)
    assert r is not None
    assert r.speed_kmh == pytest.approx(36.0, abs=1e-6)
    assert r.speed_mph == pytest.approx(10.0 * MPH_PER_MS, abs=1e-6)
    assert r.direction == "Eastbound"
    assert r.distance_m == pytest.approx(5.0, abs=1e-9)
    assert r.duration_s == pytest.approx(0.5, abs=1e-9)
    assert r.confidence == "ok"


def test_westbound_reads_back_and_picks_its_own_distance():
    # d_west_m=4.0 over 0.5s -> 8 m/s == 28.8 km/h.
    r = estimate(make_pass("west"), BASE_CFG)
    assert r is not None
    assert r.speed_kmh == pytest.approx(28.8, abs=1e-6)
    assert r.direction == "Westbound"
    assert r.distance_m == pytest.approx(4.0, abs=1e-9)


def test_kmh_mph_are_consistent():
    r = estimate(make_pass("east"), BASE_CFG)
    assert r.speed_mph / r.speed_kmh == pytest.approx(MPH_PER_MS / KMH_PER_MS,
                                                      rel=1e-9)


def test_uncalibrated_direction_returns_none(capsys):
    cfg = {**BASE_CFG, "d_east_m": None}
    assert estimate(make_pass("east"), cfg) is None
    # It should print the crossing time so a known-speed pass can be turned into
    # the distance.
    assert "CALIBRATE" in capsys.readouterr().out


def test_too_few_samples_returns_none():
    assert estimate(make_pass("east", n=4), BASE_CFG) is None


def test_pass_that_never_crosses_both_columns_returns_none():
    # A track loitering between the columns crosses neither x_a nor x_b fully.
    samples = [Sample(t=i * 0.1, ground_px=(700 + i, 500), bbox=(0, 0, 20, 10))
               for i in range(12)]
    assert estimate(Track(id=1, samples=samples), BASE_CFG) is None


def test_implausibly_fast_is_rejected():
    # A blur that crosses both columns in a couple of frames -> absurd speed.
    r = estimate(make_pass("east", v_px=100000.0, n=16, dt=0.05), BASE_CFG)
    assert r is None


def test_below_min_speed_is_rejected():
    cfg = {**BASE_CFG, "min_speed_kmh": 50}   # 36 km/h pass is below the floor
    assert estimate(make_pass("east"), cfg) is None


# --------------------------------------------------------------- crossing time
def test_crossing_seconds_linear_interpolation():
    import numpy as np
    # x goes 1100 -> 300 at 1000 px/s over the samples; crosses 1000 at t=0.1,
    # crosses 450 at t=0.65 -> dt = 0.55s.
    ts = np.array([i * 0.05 for i in range(20)], dtype=float)
    xs = 1100.0 - 1000.0 * ts
    assert _crossing_seconds(ts, xs, XA, XB) == pytest.approx(0.55, abs=1e-9)


def test_crossing_seconds_none_when_one_column_never_reached():
    import numpy as np
    ts = np.array([i * 0.05 for i in range(10)], dtype=float)
    xs = 1100.0 - 100.0 * ts          # only drops to ~1055, never reaches 1000
    assert _crossing_seconds(ts, xs, XA, XB) is None
