# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""My Road Speed Limit: /api/speedlimit sets the over-limit line, entered in the
node's display units and stored internally in km/h. Exercises the real
Runner.set_speed_limit conversion against a stubbed camera."""
import pytest

from speedkam import web
from speedkam.pipeline import SpeedCamera
from speedkam.state import RuntimeState


class _FakeCam:
    """Stands in for SpeedCamera: records the km/h the limit is stored as."""
    def __init__(self, units="mph"):
        self.units = units
        self.stored_kmh = None

    def set_speed_limit_kmh(self, value):
        self.stored_kmh = float(value)
        return self.stored_kmh


def _runner(units="mph"):
    # Build a Runner without running its heavy __init__ (no camera/pipeline);
    # only the bits /api/speedlimit touches are needed.
    r = object.__new__(web.Runner)
    r.speedcam = _FakeCam(units)
    r.cfg = {"web": {}}
    return r


def _client(runner):
    return web.create_app(runner).test_client()


def test_mph_is_converted_to_kmh():
    r = _runner(units="mph")
    applied = r.set_speed_limit(25, "mph")
    assert applied == pytest.approx(25 * 1.609344)   # 40.2336
    assert r.speedcam.stored_kmh == pytest.approx(40.2336)


def test_kmh_units_pass_through():
    r = _runner(units="kmh")
    applied = r.set_speed_limit(40, "kmh")
    assert applied == pytest.approx(40.0)


def test_units_default_to_node_display_units():
    r = _runner(units="mph")
    r.set_speed_limit(30)                              # no explicit units
    assert r.speedcam.stored_kmh == pytest.approx(30 * 1.609344)


@pytest.mark.parametrize("bad", [0, -5])
def test_non_positive_is_rejected(bad):
    r = _runner()
    with pytest.raises(ValueError):
        r.set_speed_limit(bad, "mph")


def test_route_sets_limit_and_reports_kmh():
    r = _runner(units="mph")
    resp = _client(r).post("/api/speedlimit", json={"limit": 25})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["speed_limit_kmh"] == pytest.approx(40.2336)
    assert r.speedcam.stored_kmh == pytest.approx(40.2336)


def test_route_rejects_non_numeric():
    r = _runner()
    resp = _client(r).post("/api/speedlimit", json={"limit": "fast"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_route_rejects_zero():
    r = _runner()
    resp = _client(r).post("/api/speedlimit", json={"limit": 0})
    assert resp.status_code == 400
    assert r.speedcam.stored_kmh is None               # nothing stored


def test_pipeline_limit_is_state_backed_and_persists(tmp_path):
    # The over-limit line lives in RuntimeState so it survives restarts and can
    # be set live -- same mechanism as SpeedKapture / orientation.
    state_file = tmp_path / "runtime.json"
    cam = object.__new__(SpeedCamera)
    cam.state = RuntimeState(state_file, {"speed_limit_kmh": 30.0})

    assert cam.limit_kmh == pytest.approx(30.0)         # seeded default
    assert cam.set_speed_limit_kmh(40.2336) == pytest.approx(40.2336)
    assert cam.limit_kmh == pytest.approx(40.2336)      # read back live

    # A fresh state over the same file re-reads the persisted value.
    reloaded = RuntimeState(state_file, {"speed_limit_kmh": 30.0})
    assert reloaded.get("speed_limit_kmh") == pytest.approx(40.2336)


def test_pipeline_setter_clamps_negatives():
    cam = object.__new__(SpeedCamera)
    cam.state = RuntimeState("/nonexistent/never-written.json",
                             {"speed_limit_kmh": 30.0})
    assert cam.set_speed_limit_kmh(-10) == 0.0          # floored, never negative
