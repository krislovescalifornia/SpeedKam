# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Camera opening is best-effort: a missing/failing camera must NOT raise, so
the node's web dashboard + fleet heartbeat stay up and the run loop can retry."""
import numpy as np
import pytest

from speedkam import capture
from speedkam.detector import MotionDetector


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


def test_lores_size_from_detect_scale(monkeypatch):
    """A picamera2 node sizes its hardware detection stream from detect_scale;
    default (1.0) means no lores stream."""
    monkeypatch.setattr(capture.cv2, "VideoCapture", lambda *a: FakeCap(opened=True))
    cam = capture.Camera(CFG, detect_scale=0.5)          # 320x240 -> 160x120
    assert cam._lores_size == (160, 120)
    assert capture.Camera(CFG, detect_scale=1.0)._lores_size is None


def test_lores_disabled_when_undistort_on(monkeypatch):
    """The lores frame isn't undistorted, so it must not be used when lens
    undistortion is enabled (its coordinates wouldn't match the calibration)."""
    monkeypatch.setattr(capture.cv2, "VideoCapture", lambda *a: FakeCap(opened=True))
    cfg = dict(CFG, undistort={"enabled": True, "dist_coeffs": [0, 0, 0, 0, 0]})
    assert capture.Camera(cfg, detect_scale=0.5)._lores_size is None


def test_detect_upscale_maps_to_full_resolution():
    """Detection on a hardware-downscaled frame reports full-resolution
    coordinates via the upscale factor (no software resize)."""
    cfg = {"min_area": 1500, "max_area": 500000, "history": 50, "var_threshold": 40,
           "detect_shadows": False, "morph_kernel": 5, "detect_scale": 0.5}
    det = MotionDetector(cfg)
    lw, lh = 640, 360
    for _ in range(30):
        det.detect(np.zeros((lh, lw), np.uint8), upscale=2.0)   # prime background
    f = np.zeros((lh, lw), np.uint8)
    f[200:240, 400:460] = 255                                   # lores-space blob
    dets, _ = det.detect(f, upscale=2.0)
    assert len(dets) == 1
    x, y, w, h = dets[0].bbox
    assert (round(x), round(y), round(w), round(h)) == (800, 400, 120, 80)
