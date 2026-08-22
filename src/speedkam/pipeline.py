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
import numpy as np

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
        # Tell the camera the detection downscale so a picamera2 node can emit a
        # hardware "lores" detection stream at that size (see Camera). One knob
        # (detect_scale) drives detection resolution on both backends.
        self.camera = Camera(
            cfg["camera"],
            detect_scale=float(cfg["detection"].get("detect_scale", 1.0) or 1.0),
        )
        self.detector = MotionDetector(cfg["detection"])
        self.tracker = Tracker(cfg["tracker"], min_hits=cfg["detection"]["min_hits"])

        self._calib_lock = threading.Lock()
        # Short history of finished tracks' pixel trails, for the drive-by
        # calibrator (web.py). Populated in _finalize BEFORE the speed step, so
        # it works even while the node is still uncalibrated (which is exactly
        # when you'd be drive-by calibrating). Guarded by its own lock because
        # the pipeline thread writes it and the Flask thread reads it.
        self._recent_passes = deque(maxlen=40)
        self._passes_lock = threading.Lock()
        # When set (by the web drive-by session), also grab a small thumbnail of
        # each finished vehicle so the operator can pick their own truck out of a
        # list that includes random neighbour traffic. Off the rest of the time
        # so normal operation pays no JPEG-encode cost per pass.
        self._pass_thumbs = threading.Event()
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
        self._last_result_text = "Ready"
        self._last_over = False

        # SpeedKapture threshold (display units) + the road's speed limit (km/h).
        # Seeded from config, then overridable live from the dashboard (and the
        # off-site fleet dashboard) and persisted per-node across restarts.
        self.state = RuntimeState(
            cfg["recording"].get("state_file", "captures/runtime.json"),
            {"speedkapture_threshold":
                float(cfg["recording"].get("speedkapture_threshold", 0) or 0),
             # "My Road Speed Limit": what counts as over-limit / SPEEDING.
             # Stored in km/h (the internal unit); the UI edits it in whatever
             # display_units the node uses and converts on the way in/out.
             "speed_limit_kmh": float(cfg["speed"]["speed_limit_kmh"]),
             # Camera mounting: selects which measure_band preset is active.
             # Dashboard-toggleable (parallel = side-on, head_on = receding).
             "orientation": speed_mod.normalize_orientation(
                 (cfg["speed"].get("measure_band") or {}).get("orientation")),
             # False-positive auto-reject envelope (dashboard-tunable, persisted).
             # A reading whose vehicle "traveled" farther than max_track_distance_m
             # across the scene is a phantom track (noise/vegetation stitched into
             # a path); one whose ground footprint is narrower than
             # min_vehicle_span_m is sub-car-sized (a cyclist/pedestrian). Either
             # is auto-rejected: logged for review but kept out of every stat.
             # 0 disables that gate.
             "max_track_distance_m":
                 float(cfg["speed"].get("max_track_distance_m", 0) or 0),
             "min_vehicle_span_m":
                 float(cfg["speed"].get("min_vehicle_span_m", 0) or 0),
             # Car-shape gate: min bbox width/height (side-on cars are wide,
             # pedestrians/cyclists are not). And the count-once dedupe window.
             "min_vehicle_aspect":
                 float(cfg["speed"].get("min_vehicle_aspect", 0) or 0),
             "dedupe_seconds":
                 float(cfg["speed"].get("dedupe_seconds", 0) or 0),
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
        # Last COUNTED (real) pass, for the count-once-per-drive-by dedupe:
        # {"t": monotonic, "direction": str, "speed_kmh": float}.
        self._last_count = None
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

    # --------------------------------------------------------- My Road Speed Limit
    @property
    def limit_kmh(self) -> float:
        """The road's speed limit in km/h -- the line for 'over limit'/SPEEDING.
        Seeded from config, live-editable from either dashboard, and persisted."""
        return float(self.state.get("speed_limit_kmh") or 0)

    def set_speed_limit_kmh(self, value) -> float:
        """Update the live speed limit (km/h); persists across restarts."""
        v = max(0.0, float(value))
        self.state.set("speed_limit_kmh", v)
        return v

    # ------------------------------------------------------- false-positive gate
    @property
    def max_track_distance_m(self) -> float:
        """Max plausible across-scene travel (m); above it a reading is a phantom
        track and auto-rejected. 0 disables the gate."""
        return float(self.state.get("max_track_distance_m") or 0)

    @property
    def min_vehicle_span_m(self) -> float:
        """Min real-world ground footprint (m) for a real vehicle; below it the
        object is a cyclist/pedestrian and auto-rejected. 0 disables the gate."""
        return float(self.state.get("min_vehicle_span_m") or 0)

    @property
    def min_vehicle_aspect(self) -> float:
        """Min bbox aspect ratio (width/height) for a real vehicle. On a side-on
        camera a car's box is WIDE (w/h ~2-3) while a walking person's is TALL
        (~0.3-0.5) and a side-on cyclist ~square (~0.9) -- so this is the single
        most reliable 'is it car-shaped?' check, and (unlike the world-size gate)
        it needs no homography, so a person close to the lens can't fool it.
        0 disables the gate."""
        return float(self.state.get("min_vehicle_aspect") or 0)

    @property
    def dedupe_seconds(self) -> float:
        """Count a drive-by ONCE: a second confirmed pass in the SAME direction
        finishing within this many seconds of the last counted one is treated as
        the same vehicle (a fragmented track) and rejected. 0 disables it."""
        return float(self.state.get("dedupe_seconds") or 0)

    def set_reject_thresholds(self, max_distance_m=None, min_span_m=None,
                              min_aspect=None, dedupe_seconds=None) -> dict:
        """Live-tune the auto-reject envelope; persists across restarts."""
        if max_distance_m is not None:
            self.state.set("max_track_distance_m", max(0.0, float(max_distance_m)))
        if min_span_m is not None:
            self.state.set("min_vehicle_span_m", max(0.0, float(min_span_m)))
        if min_aspect is not None:
            self.state.set("min_vehicle_aspect", max(0.0, float(min_aspect)))
        if dedupe_seconds is not None:
            self.state.set("dedupe_seconds", max(0.0, float(dedupe_seconds)))
        return {"max_track_distance_m": self.max_track_distance_m,
                "min_vehicle_span_m": self.min_vehicle_span_m,
                "min_vehicle_aspect": self.min_vehicle_aspect,
                "dedupe_seconds": self.dedupe_seconds}

    @staticmethod
    def _aspect_ratio(track):
        """Median bbox aspect ratio (width/height) across the track's samples,
        or None. The median shrugs off the odd bad frame."""
        ratios = [s.bbox[2] / s.bbox[3]
                  for s in track.samples
                  if len(s.bbox) == 4 and s.bbox[2] > 0 and s.bbox[3] > 0]
        if not ratios:
            return None
        return float(np.median(ratios))

    def _is_duplicate(self, result):
        """Is this pass a fragmented re-detection of the vehicle we just counted?
        Returns a reason string if so (same direction, within dedupe_seconds),
        else None. Keeps one drive-by from being counted twice when a track
        breaks and re-acquires."""
        window = self.dedupe_seconds
        last = self._last_count
        if window <= 0 or last is None:
            return None
        gap = time.monotonic() - last["t"]
        if 0 <= gap <= window and result.direction == last["direction"]:
            return (f"duplicate — same {result.direction} vehicle counted "
                    f"{gap:.1f}s ago (one drive-by)")
        return None

    def _vehicle_span_m(self, track):
        """Approx real-world ground footprint of the tracked object (meters).

        Maps each sample's bbox bottom edge (its ground-contact line) through the
        homography and takes a robust high percentile (75th) of the per-frame
        widths. A car spans well over a metre; a cyclist or pedestrian well under
        one -- so this cleanly separates real vehicles from the bike/pedestrian
        passes that would otherwise be timed as speeders. The 75th percentile (not
        the max) ignores a single bad frame where two blobs merged. Returns None
        when uncalibrated or the track is too short to judge."""
        with self._calib_lock:
            calib = self.calibration
        if calib is None or len(track.samples) < 2:
            return None
        lefts, rights = [], []
        for s in track.samples:
            x, y, w, h = s.bbox
            if w <= 0 or h <= 0:
                continue
            lefts.append((x, y + h))
            rights.append((x + w, y + h))
        if len(lefts) < 2:
            return None
        try:
            wl = np.asarray(calib.image_to_world(lefts), dtype=np.float64)
            wr = np.asarray(calib.image_to_world(rights), dtype=np.float64)
        except Exception:  # noqa: BLE001 - a bad homography must not break the loop
            return None
        spans = np.linalg.norm(wl - wr, axis=1)
        spans = spans[np.isfinite(spans)]
        if spans.size == 0:
            return None
        return float(np.percentile(spans, 75))

    def _classify_reading(self, result, span_m, aspect):
        """Auto-reject anything that isn't a car so junk never pollutes the stats.
        Returns (status, reason): "ok" for a real vehicle, else "rejected" with a
        human explanation (shown in the dashboards' auto-reject bin)."""
        # Shape first: a car is wider than it is tall on a side-on camera; a
        # pedestrian/dog-walker/cyclist is not. Pixel-only, so a person close to
        # the lens (which fools the world-size gate) is still caught.
        min_aspect = self.min_vehicle_aspect
        if min_aspect > 0 and aspect is not None and aspect < min_aspect:
            kind = ("a person/pedestrian" if aspect < 0.8
                    else "a cyclist or pedestrian")
            return ("rejected",
                    f"shape {aspect:.1f}w:1h — taller than a car, likely {kind}")
        max_dist = self.max_track_distance_m
        if max_dist > 0 and result.distance_m > max_dist:
            return ("rejected",
                    f"traveled {result.distance_m:.0f} m across the scene "
                    f"(max plausible {max_dist:.0f} m) — phantom track")
        min_span = self.min_vehicle_span_m
        if min_span > 0 and span_m is not None and span_m < min_span:
            return ("rejected",
                    f"object ~{span_m:.1f} m wide — smaller than a vehicle "
                    f"(likely a cyclist or pedestrian)")
        return ("ok", "")

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
        frames = 0
        t_start = time.monotonic()
        self.running = True
        if self.sync is not None:
            self.sync.start()
        if self.remote is not None:
            self.remote.start()
        self.retention.start()
        where = "window" if show else "web/headless"
        mode = "parallel capture+process" if frame_callback is not None else "single-thread"
        print(f"[SpeedKam] Running ({where}, {mode}). Ctrl+C to stop.")

        try:
            if frame_callback is not None:
                # Headless/web: overlap camera capture with detection on separate
                # cores. The capture thread also feeds the recorder, so clips stay
                # gap-free even when detection drops frames under load.
                frames = self._run_parallel(frame_callback, draw_debug, stop_event)
            else:
                # Desktop window / pure headless: keep the simple single-threaded
                # loop (cv2.imshow must stay on this thread).
                frames = self._run_single(show, draw_debug, stop_event)
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

    # ------------------------------------------------------------ frame work
    def _process_frame(self, t, frame, detect_frame, frame_callback,
                       draw_debug, show):
        """Detect -> track -> finalize one frame and publish the view.

        Shared by the single-threaded and parallel run paths. Does NOT push to
        the recorder (the caller owns that, so the parallel capture thread can
        record every frame while this may see only the latest). Returns False
        only when the desktop window asked to quit ('q'); True otherwise.
        """
        self.frame_wh = (frame.shape[1], frame.shape[0])
        # Prefer the camera's hardware-downscaled detection frame (the picamera2
        # lores stream) when present -- detection runs on a tiny frame with no
        # software resize. Otherwise the detector downscales the full frame.
        if detect_frame is not None:
            upscale = frame.shape[1] / detect_frame.shape[1]
            detections, _ = self.detector.detect(detect_frame, upscale=upscale)
        else:
            detections, _ = self.detector.detect(frame)
        world = self._world_points(detections)
        active, finished = self.tracker.update(detections, world, t)

        for tr in finished:
            self._finalize(tr)

        # Preview: the process thread only takes a tiny race-safe snapshot of
        # what to draw; the actual copy + annotate is deferred to whoever renders
        # (the web encoder thread for headless, or inline for the desktop
        # window). This keeps the ~copy+draw cost off the capture/detect path.
        if show:
            view = self.render_preview(frame, self._overlay(active, draw_debug))
            cv2.imshow("SpeedKam", view)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                return False
        elif frame_callback is not None:
            frame_callback(frame, self._overlay(active, draw_debug))
        return True

    def _overlay(self, active, draw_debug):
        """Immutable snapshot of everything the live view needs to draw, taken on
        the process thread so a later render on another thread can't race the
        tracker. Cheap: just reads a few numbers per active track."""
        return {
            "draw_debug": draw_debug,
            "tracks": [(tr.id, tuple(tr.last_bbox), tuple(tr.last_ground))
                       for tr in active],
            "text": self._last_result_text,
            "over": self._last_over,
        }

    def render_preview(self, raw, overlay):
        """Build the annotated live-view frame from a raw frame + overlay
        snapshot. Runs on the CALLER's thread -- the web encoder thread for the
        headless path -- so the capture/detect loop never pays the copy+draw."""
        view = raw.copy()
        if overlay.get("draw_debug"):
            with self._calib_lock:
                calib = self.calibration
            annotate.draw_zone(view, calib)
            annotate.draw_measure_band(view, self._active_band())
            annotate.draw_track_boxes(view, overlay.get("tracks", ()))
        annotate.draw_hud(view, overlay.get("text", ""), overlay.get("over", False))
        return view

    def _reopen_or_wait(self, stop_event, cam_down_logged):
        """Shared camera-down handling: retry the open, keep the node alive.

        Returns (still_down, cam_down_logged, should_stop). A loose CSI cable
        must not brick a remote node -- the dashboard/heartbeat stay up and we
        just report "camera down" and retry every 3s.
        """
        self.current_fps = 0.0
        if self.camera.reopen():
            print(f"[SpeedKam] camera connected ({self.camera.backend}).")
            return False, False, False
        if not cam_down_logged:
            print("[SpeedKam] camera unavailable "
                  f"({self.camera.open_error}) -- serving dashboard "
                  "without video; retrying every 3s.")
            cam_down_logged = True
        return True, cam_down_logged, self._wait(stop_event, 3.0)

    def _run_single(self, show, draw_debug, stop_event):
        """Single-threaded capture+process loop (desktop window / headless)."""
        frames = 0
        cam_down_logged = False
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if not self.camera.opened:
                _, cam_down_logged, stop = self._reopen_or_wait(
                    stop_event, cam_down_logged)
                if stop:
                    break
                continue
            t, frame = self.camera.read()
            if frame is None:
                if self.camera.offline:
                    print("[SpeedKam] End of video stream.")
                    break
                self.current_fps = 0.0
                self.camera.mark_closed()
                continue
            frames += 1
            self._tick_fps()
            if self.recorder is not None:
                self.recorder.push(t, frame)
            if not self._process_frame(t, frame, self.camera.detect_frame,
                                       frame_callback=None, draw_debug=draw_debug,
                                       show=show):
                break
        return frames

    def _run_parallel(self, frame_callback, draw_debug, stop_event):
        """Capture on one core, detect/track/finalize on another.

        The capture thread reads frames as fast as the sensor delivers, records
        every one, and publishes the newest to the process loop (latest wins --
        intermediate frames are dropped for detection only, never for the clip
        buffer). The process loop runs in this (the run) thread.
        """
        cond = threading.Condition()
        state = {"item": None, "seq": 0, "end": False}

        def capture_loop():
            cam_down_logged = False
            try:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        break
                    if not self.camera.opened:
                        _, cam_down_logged, stop = self._reopen_or_wait(
                            stop_event, cam_down_logged)
                        if stop:
                            break
                        continue
                    t, frame = self.camera.read()
                    if frame is None:
                        if self.camera.offline:
                            print("[SpeedKam] End of video stream.")
                            break
                        self.current_fps = 0.0
                        self.camera.mark_closed()
                        continue
                    # Record every frame here so clips never have gaps, even if
                    # detection can't keep up and skips some below.
                    if self.recorder is not None:
                        self.recorder.push(t, frame)
                    # detect_frame is a fresh array per read (picamera2 lores is
                    # ascontiguousarray'd; opencv path is None), so publishing the
                    # reference is safe -- the next read won't mutate this tuple.
                    with cond:
                        state["item"] = (t, frame, self.camera.detect_frame)
                        state["seq"] += 1
                        cond.notify()
            finally:
                with cond:
                    state["end"] = True
                    cond.notify()

        cap = threading.Thread(target=capture_loop, name="speedkam-capture",
                               daemon=True)
        cap.start()

        frames = 0
        last_seen = 0
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                with cond:
                    cond.wait_for(
                        lambda: state["seq"] != last_seen or state["end"]
                        or (stop_event is not None and stop_event.is_set()),
                        timeout=1.0)
                    if state["end"] and state["seq"] == last_seen:
                        break
                    item = state["item"]
                    last_seen = state["seq"]
                if item is None:
                    continue
                t, frame, detect_frame = item
                frames += 1
                self._tick_fps()
                self._process_frame(t, frame, detect_frame, frame_callback,
                                    draw_debug=draw_debug, show=False)
        finally:
            cap.join(timeout=2.0)
        return frames

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
    def set_pass_thumbs(self, enabled):
        """Toggle per-pass thumbnail capture (see _pass_thumbs)."""
        if enabled:
            self._pass_thumbs.set()
        else:
            self._pass_thumbs.clear()

    def _make_thumb(self, track):
        """A small JPEG crop of the vehicle near mid-pass, or None.

        Lets the drive-by UI show what each pass was, so the operator can tell
        their own truck from a neighbour's car. Best-effort: needs the recorder's
        frame buffer, and never raises into the pipeline loop.
        """
        if self.recorder is None or not track.samples:
            return None
        try:
            s = track.samples[len(track.samples) // 2]
            frame = self.recorder.frame_at(s.t)
            if frame is None:
                return None
            h, w = frame.shape[:2]
            x, y, bw, bh = (int(v) for v in s.bbox)
            pad = int(0.15 * max(bw, bh))
            x0, y0 = max(0, x - pad), max(0, y - pad)
            x1, y1 = min(w, x + bw + pad), min(h, y + bh + pad)
            crop = frame[y0:y1, x0:x1]
            if crop.size == 0:
                crop = frame
            tw = 200
            scale = tw / max(1, crop.shape[1])
            small = cv2.resize(crop, (tw, max(1, int(crop.shape[0] * scale))))
            ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 72])
            return buf.tobytes() if ok else None
        except Exception:  # noqa: BLE001 - a thumbnail must never break the loop
            return None

    def _record_pass(self, track):
        """Snapshot a finished track's ground-point pixel trail for drive-by
        calibration. Cheap; runs for every confirmed track regardless of
        calibration state. Adds a thumbnail only while a drive-by session asked
        for them."""
        trail = [(s.t, float(s.ground_px[0]), float(s.ground_px[1]))
                 for s in track.samples]
        if len(trail) < 2:
            return
        thumb = self._make_thumb(track) if self._pass_thumbs.is_set() else None
        with self._passes_lock:
            self._recent_passes.append({
                "finish_t": time.monotonic(),
                "wall": datetime.now().strftime("%H:%M:%S"),
                "id": track.id,
                "trail": trail,
                "thumb": thumb,
            })

    def passes_since(self, after_t):
        """All recorded passes that finished strictly after `after_t`, oldest
        first. The caller advances `after_t` past what it has consumed."""
        with self._passes_lock:
            fresh = [p for p in self._recent_passes if p["finish_t"] > after_t]
        fresh.sort(key=lambda p: p["finish_t"])
        return fresh

    def _finalize(self, track):
        # Record the raw pixel trail first -- the drive-by calibrator needs it
        # even for tracks that yield no speed (e.g. still uncalibrated).
        self._record_pass(track)
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

        # False-positive gate: is this a real CAR, or a phantom/cyclist/
        # pedestrian/dog? A rejected reading is logged (for the review bin) but
        # never counted, never flagged as a speeder, and never films a clip.
        span_m = self._vehicle_span_m(track)
        aspect = self._aspect_ratio(track)
        status, reason = self._classify_reading(result, span_m, aspect)
        # Count-once-per-drive-by: a real pass that repeats the last counted one
        # (same direction, within dedupe_seconds) is a fragmented re-detection of
        # the same vehicle -> drop it as a duplicate.
        if status == "ok":
            dup = self._is_duplicate(result)
            if dup:
                status, reason = "rejected", dup
        rejected = status == "rejected"
        # A rejected reading can never be a "captured speeder"; keep a snapshot
        # only when we'd have kept one anyway, so the bin has something to show.
        capture = (not rejected) and self._should_capture(display_speed)

        # Best-effort attributes (type/make/model/year/color) -- run for EVERY
        # counted pass, even ones below the SpeedKapture threshold.
        attrs = self._recognize(track)

        self._last_result_text = (
            f"#{track.id}: {result.display(self.units)} {result.direction}"
            + ("  REJECTED" if rejected
               else ("  SPEEDING" if over else "")
               + ("" if capture else "  (below SpeedKapture)"))
        )
        self._last_over = over and not rejected
        tag = "XX" if rejected else ("!!" if over else "  ")
        extra = self._attr_label(attrs)
        span_txt = f", span={span_m:.1f}m" if span_m is not None else ""
        aspect_txt = f", aspect={aspect:.2f}" if aspect is not None else ""
        print(f"[SpeedKam] {tag} vehicle #{track.id}: {result.display(self.units)} "
              f"({result.direction}, {result.distance_m:.1f} m{span_txt}"
              f"{aspect_txt}, conf={result.confidence}){extra}"
              + (f"  [REJECTED: {reason}]" if rejected
                 else ("" if capture else "  [not captured]")))

        # Save a clip only when above the SpeedKapture threshold AND not rejected;
        # always log the row so counts + attributes are recorded even for
        # uncaptured passes. Rejected readings still keep a snapshot for review.
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
            elif rejected or self.cfg["recording"].get("always_snapshot"):
                # No clip, but keep a JPEG: for rejects so the bin is reviewable,
                # for sub-threshold passes so a deferred worker can enrich later.
                snap = self.recorder.save_snapshot_only(
                    track.id, result, self.units, self.limit_kmh)
                snap_name = snap.name if snap else None
            self.recorder.log_row(track.id, result, self.units, attrs,
                                  clip_name, snap_name, captured=capture,
                                  status=status, review_reason=reason)

        # A rejected reading is junk: it must not touch the live counters.
        if not rejected:
            self.total_count += 1
            if over:
                self.speeder_count += 1
            # Remember this counted pass so the next fragmented re-detection of
            # the same vehicle can be deduped (count-once-per-drive-by).
            self._last_count = {"t": time.monotonic(),
                                "direction": result.direction,
                                "speed_kmh": result.speed_kmh}

        event = {
            "track_id": track.id,
            "speed_kmh": round(result.speed_kmh, 1),
            "speed_mph": round(result.speed_mph, 1),
            "direction": result.direction,
            "confidence": result.confidence,
            "distance_m": round(result.distance_m, 1),
            "duration_s": round(result.duration_s, 2),
            "n_samples": result.n_samples,
            "over_limit": over and not rejected,
            "captured": capture,
            "status": status,
            "review_reason": reason,
            "vehicle_type": attrs.get("vehicle_type"),
            "make": attrs.get("make"),
            "model": attrs.get("model"),
            "year": attrs.get("year"),
            "color": attrs.get("color"),
            "time": datetime.now().isoformat(timespec="seconds"),
            "clip": clip_name,
            "snapshot": snap_name,
        }
        # "Latest reading" tracks the last REAL vehicle -- a rejected phantom
        # shouldn't hijack the headline panel/heartbeat.
        if not rejected:
            self.last_event = event
        # Mirror off-site: captured events always; every pass (incl. rejects, so
        # the off-site bin + stats stay consistent) when backup.mirror_all is on.
        if self.sync is not None and (capture or self._mirror_all):
            self.sync.enqueue(event)

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
