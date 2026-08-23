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


# --------------------------------------------------------- road-region gate
# A road strip like the real reference node: a thin horizontal band of clicked
# points near the middle of a 1456x1088 frame (x 518..1411, y 758..825).
ROAD_IMG = [[518, 758], [1411, 758], [1411, 825], [518, 825], [900, 790]]
ROAD_WORLD = [[0, 0], [10, 0], [10, 3], [0, 3], [5, 1.5]]
FRAME = (1456, 1088)


def _road_calib():
    return Calibration(ROAD_IMG, ROAD_WORLD)


def test_on_road_side_car_on_road():
    c = _road_calib()
    # A car's ground point squarely on the road band.
    assert c.on_road_side([[900, 790]], FRAME)[0]


def test_on_road_side_distant_car_above_strip_kept():
    c = _road_calib()
    # A receding car rides "above" the clicked strip (smaller y). The far edge is
    # intentionally open, so it must still count as on-road.
    assert c.on_road_side([[900, 690]], FRAME)[0]


def test_on_road_side_foreground_pedestrian_rejected():
    c = _road_calib()
    # Feet in the near foreground (larger y than the road's near edge) -- the
    # two-kids-on-the-lawn failure. Must read off-road.
    assert not c.on_road_side([[900, 900]], FRAME)[0]


def test_on_road_side_foreground_kids_real_coords_rejected():
    c = _road_calib()
    # The actual failing track sat at ground ~(205..570, 843..863): left of the
    # road and below its near edge. Every one of those must be off-road.
    pts = [[548, 863], [480, 860], [382, 860], [283, 863], [228, 863]]
    assert not c.on_road_side(pts, FRAME).any()


def test_on_road_side_beside_road_rejected():
    c = _road_calib()
    # Well off to the right of the road (e.g. far verge) -- off-road.
    assert not c.on_road_side([[1480, 790]], FRAME)[0]
