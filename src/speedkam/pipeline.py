# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Main processing pipeline: capture -> detect -> track -> speed -> record.

Can be driven two ways:
  * run() with a cv2 preview window (desktop, run.py), or
  * run(frame_callback=..., stop_event=...) headless, publishing each annotated
    frame to a callback (used by the web dashboard, serve.py).
Both share the exact same detection/tracking/speed logic.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime

import cv2

from . import annotate, speed as speed_mod
from .calibration import Calibration
from .capture import Camera
from .detector import MotionDetector
from .recognition import VehicleRecognizer
from .recorder import Recorder
from .retention import RetentionManager
from .state import RuntimeState
from .sync import SyncManager
from .tracker import Tracker


class SpeedCamera:
    def __init__(self, cfg):
        self.cfg = cfg
        self.camera = Camera(cfg["camera"])
        self.detector = MotionDetector(cfg["detection"])
        self.tracker = Tracker(cfg["tracker"], min_hits=cfg["detection"]["min_hits"])

        self._calib_lock = threading.Lock()
        self.calibration = Calibration.load(cfg["speed"]["calibration_file"])
        if self.calibration is None:
            print("[SpeedKam] No calibration found -> DETECTION-ONLY mode "
                  "(no speed). Calibrate to enable speed.")
        else:
            err = self.calibration.reprojection_error()
            print(f"[SpeedKam] Calibration loaded (mean reprojection error "
                  f"{err:.2f} m across {len(self.calibration.image_points)} points).")

        self.recorder = None
        if cfg["recording"]["enabled"]:
            self.recorder = Recorder(
                cfg["recording"], cfg["logging"], fps_hint=cfg["camera"]["fps"]
            )

        # Optional off-site backup mirror.
        self.sync = None
        backup = cfg.get("backup", {})
        # Full mirror: back up EVERY counted pass off-site (row + snapshot),
        # not just captured (above-threshold) clips. Makes the remote a complete
        # historical record that survives local retention trimming media.
        self._mirror_all = bool(backup.get("mirror_all"))
        if backup.get("enabled"):
            if backup.get("url") and backup.get("secret"):
                self.sync = SyncManager(
                    backup, cfg["recording"]["output_dir"], cfg["logging"]["csv_file"]
                )
            else:
                print("[SpeedKam] backup.enabled but url/secret missing -> "
                      "off-site backup disabled.")

        # Optional best-effort vehicle attribute recognition (type/color/...).
        self.recognizer = VehicleRecognizer(cfg.get("recognition", {}))

        # Local + remote media rotation so storage doesn't fill up.
        self.retention = RetentionManager(
            cfg.get("retention", {}),
            cfg["recording"]["output_dir"],
            sync=self.sync,
            remote_retention_days=backup.get("remote_retention_days", 0),
        )

        self.units = cfg["speed"]["display_units"]
        self.limit_kmh = cfg["speed"]["speed_limit_kmh"]
        self._last_result_text = "Ready"
        self._last_over = False

        # SpeedKapture threshold (display units). Seeded from config, then
        # overridable live from the dashboard and persisted across restarts.
        self.state = RuntimeState(
            cfg["recording"].get("state_file", "captures/runtime.json"),
            {"speedkapture_threshold":
                float(cfg["recording"].get("speedkapture_threshold", 0) or 0),
             # Camera mounting: selects which measure_band preset is active.
             # Dashboard-toggleable (parallel = side-on, head_on = receding).
             "orientation": speed_mod.normalize_orientation(
                 (cfg["speed"].get("measure_band") or {}).get("orientation")),
             # Last off-site settings revision we've applied (see RemoteControl).
             "remote_rev": None},
        )

        # Optional pull-based remote control: check in with the off-site host and
        # adopt any settings changed on its dashboard. Reuses the backup host.
        self.remote = None
        control = cfg.get("control", {})
        if control.get("enabled") and backup.get("url") and backup.get("secret"):
            from .remotecontrol import RemoteControl
            self.remote = RemoteControl(
                control, backup["url"], backup["secret"], self,
                verify_tls=backup.get("verify_tls", True),
                timeout=backup.get("timeout", 15),
            )
        elif control.get("enabled"):
            print("[SpeedKam] control.enabled but backup url/secret missing -> "
                  "remote control disabled.")

        # --- live stats, read by the web dashboard -------------------------
        self.last_event = None        # dict describing the most recent reading
        self.total_count = 0
        self.speeder_count = 0
        self.current_fps = 0.0
        self.running = False
        self._fps_times = deque(maxlen=30)

        # Actual frame size (w, h), used to resolve the center-band measurement
        # gate (fractions -> pixels). Seeded from config; refreshed per frame in
        # case the camera hands back a different resolution than requested.
        self.frame_wh = (cfg["camera"]["width"], cfg["camera"]["height"])

    # --------------------------------------------------------------- calibration
    def set_calibration(self, calibration):
        """Hot-swap the calibration (called by the web recalibration flow)."""
        with self._calib_lock:
            self.calibration = calibration

    # --------------------------------------------------------------- SpeedKapture
    @property
    def speedkapture_threshold(self) -> float:
        """Minimum speed (display units) that triggers a saved+posted clip."""
        return float(self.state.get("speedkapture_threshold") or 0)

    def set_speedkapture_threshold(self, value) -> float:
        """Update the live capture threshold; persists across restarts."""
        v = max(0.0, float(value))
        self.state.set("speedkapture_threshold", v)
        return v

    def _should_capture(self, display_speed) -> bool:
        thr = self.speedkapture_threshold
        return thr <= 0 or display_speed > thr

    # --------------------------------------------------------------- orientation
    @property
    def orientation(self) -> str:
        """Active camera mounting: 'parallel' (side-on) or 'head_on' (receding)."""
        return speed_mod.normalize_orientation(self.state.get("orientation"))

    def set_orientation(self, value) -> str:
        """Switch the active measure_band preset; persists across restarts."""
        norm = speed_mod.normalize_orientation(value)
        self.state.set("orientation", norm)
        return norm

    def _active_band(self):
        """The measure_band resolved for the current orientation (for drawing)."""
        return speed_mod.resolve_band(
            self.cfg["speed"].get("measure_band"), self.orientation)

    def _display_speed(self, result) -> float:
        return result.speed_mph if self.units == "mph" else result.speed_kmh

    def _world_points(self, detections):
        with self._calib_lock:
            calib = self.calibration
        if calib is None:
            return [None] * len(detections)
        if not detections:
            return []
        pts = [d.ground_point for d in detections]
        return [tuple(p) for p in calib.image_to_world(pts)]

    # ---------------------------------------------------------------------- run
    def run(self, frame_callback=None, stop_event=None):
        """Process frames until the stream ends, 'q' is pressed, or stop is set.

        frame_callback(raw_frame, annotated_frame) -- optional, called per frame
            (used by the web UI). When provided, no cv2 window is opened.
        stop_event -- optional threading.Event to request shutdown.
        """
        show = self.cfg["display"]["show_window"] and frame_callback is None
        draw_debug = self.cfg["display"]["draw_debug"]
        want_view = show or frame_callback is not None
        frames = 0
        t_start = time.monotonic()
        self.running = True
        if self.sync is not None:
            self.sync.start()
        if self.remote is not None:
            self.remote.start()
        self.retention.start()
        where = "window" if show else "web/headless"
        print(f"[SpeedKam] Running ({where}). Ctrl+C to stop.")

        try:
            cam_down_logged = False
            while True:
                if stop_event is not None and stop_event.is_set():
                    break

                # Camera not open (never opened, or disconnected mid-run): keep
                # the node alive -- the web dashboard and fleet heartbeat, both
                # already started above, stay up -- and retry. A loose CSI cable
                # must not brick a remote node; it just shows "camera down".
                if not self.camera.opened:
                    self.current_fps = 0.0
                    if self.camera.reopen():
                        print(f"[SpeedKam] camera connected ({self.camera.backend}).")
                        cam_down_logged = False
                    else:
                        if not cam_down_logged:
                            print("[SpeedKam] camera unavailable "
                                  f"({self.camera.open_error}) -- serving dashboard "
                                  "without video; retrying every 3s.")
                            cam_down_logged = True
                        if self._wait(stop_event, 3.0):
                            break
                        continue

                t, frame = self.camera.read()
                if frame is None:
                    if self.camera.offline:
                        print("[SpeedKam] End of video stream.")
                        break
                    # A live camera returned nothing -> treat as a disconnect and
                    # fall into the reopen/retry path above, don't kill the loop.
                    self.current_fps = 0.0
                    self.camera.mark_closed()
                    continue

                frames += 1
                self._tick_fps()
                self.frame_wh = (frame.shape[1], frame.shape[0])

                detections, _ = self.detector.detect(frame)
                world = self._world_points(detections)
                active, finished = self.tracker.update(detections, world, t)

                if self.recorder is not None:
                    self.recorder.push(t, frame)

                for tr in finished:
                    self._finalize(tr)

                if want_view:
                    view = frame.copy()
                    if draw_debug:
                        annotate.draw_zone(view, self.calibration)
                        annotate.draw_measure_band(view, self._active_band())
                        annotate.draw_tracks(view, active, self.units)
                    annotate.draw_hud(view, self._last_result_text, self._last_over)
                    if frame_callback is not None:
                        frame_callback(frame, view)
                    if show:
                        cv2.imshow("SpeedKam", view)
                        if cv2.waitKey(1) & 0xFF == ord("q"):
                            break
        except KeyboardInterrupt:
            print("\n[SpeedKam] Interrupted.")
        finally:
            self.running = False
            if self.sync is not None:
                self.sync.stop()
            if self.remote is not None:
                self.remote.stop()
            self.retention.stop()
            self.camera.release()
            if show:
                cv2.destroyAllWindows()
            dur = time.monotonic() - t_start
            if dur > 0:
                print(f"[SpeedKam] Processed {frames} frames in {dur:.1f}s "
                      f"({frames / dur:.1f} FPS).")

    @staticmethod
    def _wait(stop_event, seconds):
        """Sleep, but wake immediately if a stop is requested. Returns True if
        the caller should stop."""
        if stop_event is not None:
            return stop_event.wait(seconds)
        time.sleep(seconds)
        return False

    def _tick_fps(self):
        now = time.monotonic()
        self._fps_times.append(now)
        if len(self._fps_times) >= 2:
            span = self._fps_times[-1] - self._fps_times[0]
            if span > 0:
                self.current_fps = (len(self._fps_times) - 1) / span

    # ----------------------------------------------------------------- finalize
    def _finalize(self, track):
        result = None
        with self._calib_lock:
            calibrated = self.calibration is not None
        if calibrated:
            result = speed_mod.estimate(track, self.cfg["speed"], self.frame_wh,
                                        orientation=self.orientation)

        if result is None:
            # No speed (uncalibrated / too-short track): nothing to count.
            self._last_result_text = f"#{track.id}: (no speed)"
            self._last_over = False
            return

        over = result.speed_kmh > self.limit_kmh
        display_speed = self._display_speed(result)
        capture = self._should_capture(display_speed)

        # Best-effort attributes (type/make/model/year/color) -- run for EVERY
        # counted pass, even ones below the SpeedKapture threshold.
        attrs = self._recognize(track)

        self._last_result_text = (
            f"#{track.id}: {result.display(self.units)} {result.direction}"
            + ("  SPEEDING" if over else "")
            + ("" if capture else "  (below SpeedKapture)")
        )
        self._last_over = over
        tag = "!!" if over else "  "
        extra = self._attr_label(attrs)
        print(f"[SpeedKam] {tag} vehicle #{track.id}: {result.display(self.units)} "
              f"({result.direction}, {result.distance_m:.1f} m, "
              f"conf={result.confidence}){extra}"
              + ("" if capture else "  [not captured]"))

        # Save a clip only when above the SpeedKapture threshold; always log the
        # row so counts + attributes are recorded even for uncaptured passes.
        clip_name = snap_name = None
        if self.recorder is not None:
            if capture:
                clip, snap = self.recorder.save_media(
                    track.id, result, self.units, self.limit_kmh,
                    self.cfg["recording"]["burn_overlay"],
                )
                clip_name = clip.name if clip else None
                snap_name = snap.name if snap else None
                if clip:
                    print(f"[SpeedKam]    saved {clip.name}")
            elif self.cfg["recording"].get("always_snapshot"):
                # Below SpeedKapture: no clip, but keep a JPEG so a deferred
                # recognition worker can still fill in type/make/model later.
                snap = self.recorder.save_snapshot_only(
                    track.id, result, self.units, self.limit_kmh)
                snap_name = snap.name if snap else None
            self.recorder.log_row(track.id, result, self.units, attrs,
                                  clip_name, snap_name, captured=capture)

        self.total_count += 1
        if over:
            self.speeder_count += 1
        self.last_event = {
            "track_id": track.id,
            "speed_kmh": round(result.speed_kmh, 1),
            "speed_mph": round(result.speed_mph, 1),
            "direction": result.direction,
            "confidence": result.confidence,
            "distance_m": round(result.distance_m, 1),
            "over_limit": over,
            "captured": capture,
            "vehicle_type": attrs.get("vehicle_type"),
            "make": attrs.get("make"),
            "model": attrs.get("model"),
            "year": attrs.get("year"),
            "color": attrs.get("color"),
            "time": datetime.now().isoformat(timespec="seconds"),
            "clip": clip_name,
            "snapshot": snap_name,
        }
        # Mirror off-site: captured events always; every counted pass when
        # backup.mirror_all is on (so the remote is a full historical record).
        if self.sync is not None and (capture or self._mirror_all):
            self.sync.enqueue(self.last_event)

    # --------------------------------------------------------------- recognition
    def _recognize(self, track):
        """Pick a representative buffered frame + bbox and run the recognizer."""
        from .recognition import EMPTY
        if not self.recognizer.active or self.recorder is None or not track.samples:
            return dict(EMPTY)
        sample = track.samples[len(track.samples) // 2]  # near mid-pass
        frame = self.recorder.frame_at(sample.t)
        if frame is None:
            return dict(EMPTY)
        return self.recognizer.recognize(frame, sample.bbox)

    @staticmethod
    def _attr_label(attrs):
        bits = [attrs.get("color"), attrs.get("vehicle_type"),
                attrs.get("make"), attrs.get("model"), attrs.get("year")]
        text = " ".join(str(b) for b in bits if b)
        return f"  {text}" if text else ""
