# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Camera opening is best-effort: a missing/failing camera must NOT raise, so
the node's web dashboard + fleet heartbeat stay up and the run loop can retry."""
import numpy as np
import pytest

from speedkam import capture


# These exercise the synchronous open/reopen/grab CONTRACT, so they pin
# threaded=False for deterministic single-grab behaviour. The background reader
# (a live-camera optimization) is covered separately below with a source that
# yields frames endlessly, the way a real camera does.
CFG = {
    "backend": "opencv",
    "source": 0,
    "width": 320,
    "height": 240,
    "fps": 30,
    "windows_use_dshow": False,
    "manual_exposure": -1,
    "undistort": None,
    "threaded": False,
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


def test_threaded_reader_delivers_frames(monkeypatch):
    """With threaded capture on (the live-camera default), the background reader
    keeps handing the newest frame to read()."""
    class Live(FakeCap):
        def read(self):                       # a live camera never runs dry
            return True, _frame()
    monkeypatch.setattr(capture.cv2, "VideoCapture", lambda *a: Live(opened=True))
    cam = capture.Camera(dict(CFG, threaded=True))
    try:
        assert cam.opened is True
        for _ in range(5):
            t, frame = cam.read()
            assert frame is not None and t is not None
    finally:
        cam.release()


def test_threaded_reader_marks_closed_on_disconnect(monkeypatch):
    """A live disconnect (read starts failing) must propagate through the reader
    thread: read() reports a drop and the camera is flagged for the retry loop."""
    class Flaky(FakeCap):
        def __init__(self, **kw):
            super().__init__(**kw)
            self._n = 0

        def read(self):
            self._n += 1
            if self._n <= 2:
                return True, _frame()
            return False, None                # unplugged after 2 frames
    monkeypatch.setattr(capture.cv2, "VideoCapture", lambda *a: Flaky(opened=True))
    cam = capture.Camera(dict(CFG, threaded=True))
    try:
        # Drain until the reader observes the disconnect and read() reports None.
        for _ in range(50):
            if cam.read() == (None, None):
                break
        assert cam.opened is False
    finally:
        cam.release()
