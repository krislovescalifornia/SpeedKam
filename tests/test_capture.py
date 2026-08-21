# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Camera opening is best-effort: a missing/failing camera must NOT raise, so
the node's web dashboard + fleet heartbeat stay up and the run loop can retry."""
import numpy as np
import pytest

from speedkam import capture


CFG = {
    "backend": "opencv",
    "source": 0,
    "width": 320,
    "height": 240,
    "fps": 30,
    "windows_use_dshow": False,
    "manual_exposure": -1,
    "undistort": None,
}


class FakeCap:
    """Stand-in for cv2.VideoCapture with controllable open/read behaviour."""
    def __init__(self, opened=True, frames=None):
        self._opened = opened
        self._frames = list(frames or [])
        self.released = False

    def isOpened(self):
        return self._opened

    def set(self, *a):
        return True

    def read(self):
        if self._frames:
            return True, self._frames.pop(0)
        return False, None

    def release(self):
        self.released = True


def _frame():
    return np.zeros((240, 320, 3), dtype=np.uint8)


def test_open_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(capture.cv2, "VideoCapture", lambda *a: FakeCap(opened=False))
    cam = capture.Camera(CFG)                 # must not raise
    assert cam.opened is False
    assert cam.open_error                     # a reason is recorded
    assert cam.read() == (None, None)         # read is safe while closed


def test_reopen_recovers_when_camera_returns(monkeypatch):
    state = {"opened": False}
    monkeypatch.setattr(capture.cv2, "VideoCapture",
                        lambda *a: FakeCap(opened=state["opened"], frames=[_frame()]))
    cam = capture.Camera(CFG)
    assert cam.opened is False
    state["opened"] = True                    # camera "reconnected"
    assert cam.reopen() is True
    t, frame = cam.read()
    assert frame is not None and t is not None


def test_read_marks_closed_when_camera_vanishes(monkeypatch):
    class Boom(FakeCap):
        def read(self):
            raise OSError("device disappeared")
    monkeypatch.setattr(capture.cv2, "VideoCapture", lambda *a: Boom(opened=True))
    cam = capture.Camera(CFG)
    assert cam.opened is True
    assert cam.read() == (None, None)         # exception swallowed
    assert cam.opened is False                # and flagged for the retry loop


def test_successful_open_reads_frames(monkeypatch):
    monkeypatch.setattr(capture.cv2, "VideoCapture",
                        lambda *a: FakeCap(opened=True, frames=[_frame(), _frame()]))
    cam = capture.Camera(CFG)
    assert cam.opened is True and cam.backend == "opencv"
    assert cam.read()[1] is not None


class _Stub:
    """Minimal carrier so we can exercise the pure control-building logic
    without opening a real CSI camera (picamera2 is Pi-only)."""


def test_picamera2_controls_caps_frame_duration():
    """`fps` must be pushed to the sensor as a frame-duration cap so auto-
    exposure can't strangle the frame rate; auto exposure/gain stay on."""
    s = _Stub()
    s.cfg = {"fps": 30, "exposure_us": 0, "analogue_gain": 0}
    c = capture.Camera._picamera2_controls(s)
    assert c["FrameDurationLimits"] == (33333, 33333)   # 30 fps => 33.3 ms cap
    assert "ExposureTime" not in c and "AeEnable" not in c
    assert "AnalogueGain" not in c


def test_picamera2_controls_fixed_exposure_and_gain():
    s = _Stub()
    s.cfg = {"fps": 15, "exposure_us": 4000, "analogue_gain": 8.0}
    c = capture.Camera._picamera2_controls(s)
    assert c["FrameDurationLimits"] == (66666, 66666)
    assert c["ExposureTime"] == 4000 and c["AeEnable"] is False
    assert c["AnalogueGain"] == 8.0
