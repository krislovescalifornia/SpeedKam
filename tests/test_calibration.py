# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Homography calibration: a known pixels->metres mapping must round-trip, and
save/load must preserve it exactly."""
import pytest

from speedkam.calibration import Calibration

# A pure scale: 1 pixel == 1 cm (0.01 m). Non-collinear, exact correspondences,
# so the fitted homography should reproduce the world points essentially exactly.
SCALE = 0.01
IMG = [[0, 0], [100, 0], [100, 100], [0, 100], [50, 50]]
WORLD = [[x * SCALE, y * SCALE] for x, y in IMG]


def test_reprojection_error_near_zero():
    c = Calibration(IMG, WORLD)
    assert c.reprojection_error() < 1e-6


def test_image_to_world_round_trip():
    c = Calibration(IMG, WORLD)
    # An interior point not in the calibration set.
    (x, y), = c.image_to_world([[80, 20]])
    assert x == pytest.approx(0.80, abs=1e-6)
    assert y == pytest.approx(0.20, abs=1e-6)


def test_needs_at_least_four_points():
    with pytest.raises(ValueError):
        Calibration([[0, 0], [1, 1], [2, 2]], [[0, 0], [1, 1], [2, 2]])


def test_save_load_preserves_transform(tmp_path):
    c = Calibration(IMG, WORLD, meta={"units": "meters", "source": "test"})
    path = tmp_path / "calib.json"
    c.save(path)

    loaded = Calibration.load(path)
    assert loaded is not None
    assert loaded.meta["source"] == "test"
    # Same mapping on a fresh point.
    a = c.image_to_world([[33, 66]])
    b = loaded.image_to_world([[33, 66]])
    assert b == pytest.approx(a, abs=1e-9)


def test_load_missing_file_returns_none(tmp_path):
    assert Calibration.load(tmp_path / "does_not_exist.json") is None
