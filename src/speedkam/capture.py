# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Camera abstraction.

One interface, three backend selections:
  * "opencv"    -> cv2.VideoCapture (USB webcam, video file, or RTSP stream).
                   Works on Windows (your Logitech cam) and on the Pi.
  * "picamera2" -> Raspberry Pi native CSI camera (Global Shutter / Cam Module).
  * "auto"      -> poll on startup: use the CSI camera if one is attached,
                   otherwise fall back to the OpenCV source. This is what the
                   fleet image ships with, so one SD card works whether a node
                   has a CSI camera or a USB webcam plugged in.

read() returns (t, frame) where t is a monotonic timestamp (seconds) captured
as close to the frame grab as possible. We use REAL timestamps rather than a
nominal FPS because webcams deliver frames irregularly, and speed = distance /
time is only as good as that time.
"""
from __future__ import annotations

import sys
import time

import cv2

from .undistort import Undistorter


class Camera:
    def __init__(self, cfg):
        self.cfg = cfg
        self._configured_backend = cfg["backend"]
        self.backend = None            # resolved on open: 'opencv' | 'picamera2'
        # Optional lens undistortion, applied to every frame BEFORE anything
        # downstream sees it -- so calibration and detection share one geometry.
        self.undistorter = Undistorter(cfg.get("undistort"))
        self._picam = None
        self._cap = None
        # Offline video files must be timed by the file's own frame clock, not
        # wall-clock read time (we process faster than real time). Live sources
        # (webcam index, RTSP/HTTP stream) are timed by the monotonic clock.
        self._offline = False
        self._file_fps = float(cfg.get("fps", 30) or 30)
        self._frame_idx = 0
        # A camera that can't open must NOT crash the node: the web dashboard and
        # fleet heartbeat have to stay up so the fault is visible and the node
        # can be fixed remotely (a knocked-loose CSI cable shouldn't brick it).
        # Opening is therefore best-effort -- on failure we stay closed and the
        # run loop retries. `opened` reflects the current live state.
        self.opened = False
        self.open_error = None
        self._open()

    # ------------------------------------------------------------------- open
    def _open(self, quiet=False):
        """(Re)open the camera and set self.opened. Never raises: a failure just
        leaves the camera closed with `open_error` set, so the run loop can keep
        the node alive and retry."""
        backend = self._configured_backend
        if backend == "auto":
            backend = self._auto_detect_backend(quiet=quiet)
        self.backend = backend
        try:
            if backend == "picamera2":
                self._open_picamera2()
            else:
                self._open_opencv()
            self.opened = True
            self.open_error = None
        except Exception as exc:  # noqa: BLE001 - stay up + retry, never crash
            self.open_error = str(exc)
            self.opened = False
            self._picam = None
            self._cap = None
        return self.opened

    def reopen(self):
        """Drop any handle and try to open again (quietly). Returns `opened`."""
        self.mark_closed()
        return self._open(quiet=True)

    def mark_closed(self):
        """Release the handle and mark the camera closed (after a read failure or
        disconnect) so the run loop drops into its reopen/retry path."""
        try:
            self.release()
        except Exception:  # noqa: BLE001 - best effort; we're tearing it down
            pass
        self._cap = None
        self._picam = None
        self.opened = False

    @property
    def offline(self):
        """True for a local video file: its end means stop, whereas a live
        camera returning nothing means a disconnect to retry."""
        return self._offline

    # -------------------------------------------------------------------- auto
    def _auto_detect_backend(self, quiet=False):
        """Pick the camera that's actually attached, at startup.

        Prefer the native CSI camera (the recommended global-shutter sensor for
        a speed cam); fall back to an OpenCV source (USB webcam / file / stream).
        On the Windows dev box picamera2 simply isn't installed, so 'auto'
        cleanly resolves to the webcam there too -- one config works everywhere.

        `quiet` silences the chatter on retry attempts (this is polled every few
        seconds while a camera is unplugged).
        """
        try:
            from picamera2 import Picamera2  # type: ignore

            cams = Picamera2.global_camera_info()
            if cams:
                models = ", ".join(c.get("Model", "?") for c in cams) or "?"
                if not quiet:
                    print(f"[camera] auto: CSI camera present ({models}) -> picamera2")
                return "picamera2"
            if not quiet:
                print("[camera] auto: picamera2 available but no CSI camera attached")
        except Exception as exc:  # ImportError on dev box, or libcamera hiccup
            if not quiet:
                print(f"[camera] auto: no CSI camera ({exc})")
        if not quiet:
            print(f"[camera] auto: falling back to OpenCV source {self.cfg['source']!r}")
        return "opencv"

    # ------------------------------------------------------------------ opencv
    def _open_opencv(self):
        source = self.cfg["source"]
        api = 0
        if sys.platform.startswith("win") and self.cfg.get("windows_use_dshow", True):
            api = cv2.CAP_DSHOW
        # Integer index vs. path/URL.
        if isinstance(source, int) or (isinstance(source, str) and source.isdigit()):
            self._cap = cv2.VideoCapture(int(source), api)
        else:
            self._cap = cv2.VideoCapture(source)
            is_stream = isinstance(source, str) and source.lower().startswith(
                ("rtsp://", "http://", "https://", "udp://", "tcp://")
            )
            if not is_stream:
                # A local video file: play back on its own timeline.
                self._offline = True
                file_fps = self._cap.get(cv2.CAP_PROP_FPS)
                if file_fps and file_fps > 0:
                    self._file_fps = float(file_fps)

        if not self._cap.isOpened():
            raise RuntimeError(
                f"Could not open camera source {source!r}. "
                "Check the index/path and that no other app is using the camera."
            )

        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg["width"])
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg["height"])
        self._cap.set(cv2.CAP_PROP_FPS, self.cfg["fps"])
        exposure = self.cfg.get("manual_exposure", -1)
        if exposure is not None and exposure != -1:
            # 0.25 => manual mode on many UVC webcams via DirectShow/V4L2.
            self._cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
            self._cap.set(cv2.CAP_PROP_EXPOSURE, float(exposure))

    # --------------------------------------------------------------- picamera2
    def _open_picamera2(self):
        try:
            from picamera2 import Picamera2  # type: ignore
        except ImportError as exc:  # pragma: no cover - Pi only
            raise RuntimeError(
                "backend 'picamera2' requested but picamera2 is not installed. "
                "On Raspberry Pi OS: sudo apt install -y python3-picamera2"
            ) from exc
        self._picam = Picamera2()
        config = self._picam.create_video_configuration(
            main={"size": (self.cfg["width"], self.cfg["height"]), "format": "RGB888"}
        )
        self._picam.configure(config)
        self._picam.start()
        time.sleep(0.5)  # let auto-exposure settle

    # -------------------------------------------------------------------- read
    def read(self):
        """Return (t_monotonic, BGR frame) or (None, None) on failure/end.

        A camera that vanishes mid-stream (unplugged, undervoltage) raises here;
        we catch it, mark the camera closed, and return (None, None) so the run
        loop retries instead of crashing the node.
        """
        if not self.opened:
            return None, None
        try:
            if self._picam is not None:  # pragma: no cover - Pi only
                t = time.monotonic()
                rgb = self._picam.capture_array()
                frame = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                return t, self.undistorter.apply(frame)

            ok, frame = self._cap.read()
            if (not ok or frame is None) and self._offline and self.cfg.get("loop"):
                # Loop a video file (handy for demos/tests): rewind and continue.
                self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                self._frame_idx = 0
                ok, frame = self._cap.read()
            if not ok or frame is None:
                return None, None
            if self._offline:
                t = self._frame_idx / self._file_fps
                self._frame_idx += 1
            else:
                t = time.monotonic()
            return t, self.undistorter.apply(frame)
        except Exception:  # noqa: BLE001 - treat a mid-read failure as a drop
            self.mark_closed()
            return None, None

    @property
    def actual_size(self):
        if self._cap is not None:
            return (
                int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            )
        return (self.cfg["width"], self.cfg["height"])

    def release(self):
        if self._cap is not None:
            self._cap.release()
        if self._picam is not None:  # pragma: no cover - Pi only
            self._picam.stop()
