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


class Camera:
    def __init__(self, cfg):
        self.cfg = cfg
        self.backend = cfg["backend"]
        self._picam = None
        self._cap = None
        # Offline video files must be timed by the file's own frame clock, not
        # wall-clock read time (we process faster than real time). Live sources
        # (webcam index, RTSP/HTTP stream) are timed by the monotonic clock.
        self._offline = False
        self._file_fps = float(cfg.get("fps", 30) or 30)
        self._frame_idx = 0
        if self.backend == "auto":
            self.backend = self._auto_detect_backend()
        if self.backend == "picamera2":
            self._open_picamera2()
        else:
            self._open_opencv()

    # -------------------------------------------------------------------- auto
    def _auto_detect_backend(self):
        """Pick the camera that's actually attached, at startup.

        Prefer the native CSI camera (the recommended global-shutter sensor for
        a speed cam); fall back to an OpenCV source (USB webcam / file / stream).
        On the Windows dev box picamera2 simply isn't installed, so 'auto'
        cleanly resolves to the webcam there too -- one config works everywhere.
        """
        try:
            from picamera2 import Picamera2  # type: ignore

            cams = Picamera2.global_camera_info()
            if cams:
                models = ", ".join(c.get("Model", "?") for c in cams) or "?"
                print(f"[camera] auto: CSI camera present ({models}) -> picamera2")
                return "picamera2"
            print("[camera] auto: picamera2 available but no CSI camera attached")
        except Exception as exc:  # ImportError on dev box, or libcamera hiccup
            print(f"[camera] auto: no CSI camera ({exc})")
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
        """Return (t_monotonic, BGR frame) or (None, None) on failure/end."""
        if self._picam is not None:  # pragma: no cover - Pi only
            t = time.monotonic()
            rgb = self._picam.capture_array()
            return t, cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

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
        return t, frame

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
