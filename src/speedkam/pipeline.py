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

import queue
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
        # YOLO gate worker (Phase 14): when the recognizer is a real on-node
        # classifier, the per-pass car-vs-not vote runs on this thread instead of
        # the detection loop, so a burst of traffic can't stall detection while
        # ~vote_frames inferences run. Created in run() only when the gate is
        # active; None means "decide inline" (geometry-only nodes).
        self._recog_q = None
        self._recog_thread = None

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
             # Road-region gate: min fraction of a track's ground points that
             # must fall on the calibrated road surface (else it's foreground /
             # off-road junk). 0 disables. See _on_road_fraction / on_road_side.
             "min_on_road_frac":
                 float(cfg["speed"].get("min_on_road_frac", 0) or 0),
             # Trajectory-quality gates (deterministic motion physics): a real
             # vehicle tracks in a near-straight line (min_straightness) and its
             # blob area changes smoothly, not in a flicker (max_area_cv). These
             # catch wind-blown foliage / noise phantoms that beat the shape and
             # size proxies. Either 0 disables. See _classify_reading / _area_cv.
             "min_straightness":
                 float(cfg["speed"].get("min_straightness", 0) or 0),
             "max_area_cv":
                 float(cfg["speed"].get("max_area_cv", 0) or 0),
             # Motion-physics gates: a real vehicle only moves forward
             # (min_monotonicity) and changes speed smoothly (max_accel_mps2).
             # Both default 0 (off) until measured; see config.DEFAULTS["speed"].
             "min_monotonicity":
                 float(cfg["speed"].get("min_monotonicity", 0) or 0),
             "max_accel_mps2":
                 float(cfg["speed"].get("max_accel_mps2", 0) or 0),
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

        # --- low-light gate ------------------------------------------------
        # In the dark the motion detector tracks headlight glare and sensor
        # noise, inventing phantom "vehicles" at absurd speeds (90-170 km/h).
        # Rather than pollute the data, pause detection/recording once the scene
        # goes dark and resume automatically at dawn -- driven purely by measured
        # frame brightness, with hysteresis so dusk/dawn don't flap it. Read by
        # the dashboards so the operator sees WHY it's idle.
        self._lg = cfg.get("light_gate", {}) or {}
        self.paused_low_light = False
        self.scene_brightness = None   # last measured mean luma (0-255) or None
        self._lg_since = None          # monotonic time the pending flip began

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
    def min_on_road_frac(self) -> float:
        """Min fraction of a track's ground points that must sit on the
        calibrated road surface for it to count as a road vehicle. Below it the
        object spent most of the pass in the foreground / off to the side and is
        auto-rejected. 0 disables the gate (also skipped when uncalibrated --
        there's no road plane to test against)."""
        return float(self.state.get("min_on_road_frac") or 0)

    @property
    def min_straightness(self) -> float:
        """Min ratio of straight-line (net) displacement to traversed path length
        for a real vehicle pass. A car tracks straight along the road (~1.0);
        wind-blown foliage / a bug on the lens / a noise blob wanders, so its net
        displacement is only a fraction of its path length. Below this the track
        is auto-rejected. 0 disables the gate. See SpeedResult.straightness."""
        return float(self.state.get("min_straightness") or 0)

    @property
    def max_area_cv(self) -> float:
        """Max coefficient of variation (std/mean) of the bbox area across a pass.
        A vehicle's silhouette changes smoothly with perspective (low CV); noise
        and swaying foliage flicker in size (high CV). Above this the track is
        auto-rejected. 0 disables the gate. See _area_cv."""
        return float(self.state.get("max_area_cv") or 0)

    @property
    def min_monotonicity(self) -> float:
        """Min fraction of a track's steps that must advance along the net
        direction of travel. A real vehicle only moves forward (~1.0); foliage /
        noise oscillates. Below this the track is auto-rejected. 0 disables the
        gate. See SpeedResult.monotonicity."""
        return float(self.state.get("min_monotonicity") or 0)

    @property
    def max_accel_mps2(self) -> float:
        """Max plausible frame-to-frame acceleration magnitude (m/s^2). A real
        vehicle changes speed smoothly; a phantom teleporting between noise blobs
        spikes it. Above this the track is auto-rejected. 0 disables the gate.
        See SpeedResult.max_accel_mps2."""
        return float(self.state.get("max_accel_mps2") or 0)

    @property
    def dedupe_seconds(self) -> float:
        """Count a drive-by ONCE: a second confirmed pass in the SAME direction
        finishing within this many seconds of the last counted one is treated as
        the same vehicle (a fragmented track) and rejected. 0 disables it."""
        return float(self.state.get("dedupe_seconds") or 0)

    def set_reject_thresholds(self, max_distance_m=None, min_span_m=None,
                              min_aspect=None, dedupe_seconds=None,
                              min_on_road_frac=None, min_straightness=None,
                              max_area_cv=None, min_monotonicity=None,
                              max_accel_mps2=None) -> dict:
        """Live-tune the auto-reject envelope; persists across restarts."""
        if max_distance_m is not None:
            self.state.set("max_track_distance_m", max(0.0, float(max_distance_m)))
        if min_span_m is not None:
            self.state.set("min_vehicle_span_m", max(0.0, float(min_span_m)))
        if min_aspect is not None:
            self.state.set("min_vehicle_aspect", max(0.0, float(min_aspect)))
        if dedupe_seconds is not None:
            self.state.set("dedupe_seconds", max(0.0, float(dedupe_seconds)))
        if min_on_road_frac is not None:
            # A fraction, so clamp to [0, 1].
            self.state.set("min_on_road_frac",
                           min(1.0, max(0.0, float(min_on_road_frac))))
        if min_straightness is not None:
            # Also a ratio in [0, 1].
            self.state.set("min_straightness",
                           min(1.0, max(0.0, float(min_straightness))))
        if max_area_cv is not None:
            self.state.set("max_area_cv", max(0.0, float(max_area_cv)))
        if min_monotonicity is not None:
            # A fraction, so clamp to [0, 1].
            self.state.set("min_monotonicity",
                           min(1.0, max(0.0, float(min_monotonicity))))
        if max_accel_mps2 is not None:
            self.state.set("max_accel_mps2", max(0.0, float(max_accel_mps2)))
        return {"max_track_distance_m": self.max_track_distance_m,
                "min_vehicle_span_m": self.min_vehicle_span_m,
                "min_vehicle_aspect": self.min_vehicle_aspect,
                "dedupe_seconds": self.dedupe_seconds,
                "min_on_road_frac": self.min_on_road_frac,
                "min_straightness": self.min_straightness,
                "max_area_cv": self.max_area_cv,
                "min_monotonicity": self.min_monotonicity,
                "max_accel_mps2": self.max_accel_mps2}

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

    @staticmethod
    def _area_cv(track):
        """Coefficient of variation (std/mean) of the bbox area across the track,
        or None when there are too few samples to judge.

        A real vehicle's silhouette grows and shrinks smoothly as it crosses the
        scene, so its area has a low CV. Wind-blown foliage and sensor noise
        stitched into a track flicker wildly in size, so their CV is high. Pixel-
        only (no homography), so a foreground phantom can't hide from it. Needs a
        few samples to be meaningful."""
        areas = [s.bbox[2] * s.bbox[3]
                 for s in track.samples
                 if len(s.bbox) == 4 and s.bbox[2] > 0 and s.bbox[3] > 0]
        if len(areas) < 3:
            return None
        a = np.asarray(areas, dtype=np.float64)
        mean = float(a.mean())
        if mean <= 0:
            return None
        return float(a.std() / mean)

    def _is_duplicate(self, result, now=None):
        """Is this pass a fragmented re-detection of the vehicle we just counted?
        Returns a reason string if so (same direction, within dedupe_seconds),
        else None. Keeps one drive-by from being counted twice when a track
        breaks and re-acquires. ``now`` defaults to the wall clock but callers on
        the recognition worker pass the pass's *finalize* time, so the vote's
        latency can't stretch the gap and defeat the dedupe."""
        window = self.dedupe_seconds
        last = self._last_count
        if window <= 0 or last is None:
            return None
        gap = (time.monotonic() if now is None else now) - last["t"]
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

    def _on_road_fraction(self, track):
        """Fraction of the track's ground points that sit on the calibrated road
        surface (see Calibration.on_road_side), or None when uncalibrated / the
        track has no samples -- in which case the road gate is skipped.

        This is the strongest 'is it a road vehicle?' signal we have without a
        neural classifier: a real car crosses the road, so nearly all of its
        ground points land on the road plane; a pedestrian standing in the
        foreground (or on the far sidewalk) spends the whole pass off it."""
        with self._calib_lock:
            calib = self.calibration
        if calib is None or not track.samples:
            return None
        pts = [s.ground_px for s in track.samples]
        try:
            margin = float(self.cfg["speed"].get("road_margin_frac", 0.03))
            mask = calib.on_road_side(pts, self.frame_wh, margin_frac=margin)
        except Exception:  # noqa: BLE001 - a bad calibration must not break the loop
            return None
        return float(np.mean(mask)) if len(mask) else None

    def _classify_reading(self, result, span_m, aspect, on_road_frac=None,
                          area_cv=None):
        """Auto-reject anything that isn't a car on the road so junk never
        pollutes the stats. Returns (status, reason): "ok" for a real vehicle,
        else "rejected" with a human explanation (shown in the dashboards' bin)."""
        # Road-region FIRST -- the most reliable check and the one that catches
        # what the shape/size proxies miss. Is the object actually ON the road,
        # or in the camera-side foreground (kerb/grass/sidewalk) / off to the
        # side? A foreground pedestrian's ground point maps through the road
        # homography to garbage and invents phantom speeds -- the exact failure
        # that logged two kids on the lawn as a 69 mph car. Majority vote over
        # the whole track, so a transient off-road sample or blob merge can't
        # flip it, and it doesn't care what shape the blob is.
        min_on_road = self.min_on_road_frac
        if (min_on_road > 0 and on_road_frac is not None
                and on_road_frac < min_on_road):
            return ("rejected",
                    f"off the calibrated road — only {on_road_frac * 100:.0f}% "
                    f"of the track was on the road surface (foreground/sidewalk, "
                    f"not a road vehicle)")
        # Straightness next: the strongest motion-physics check. A real vehicle
        # tracks in a near-straight line along the road, so its net displacement
        # is nearly the whole path it traversed (ratio ~1). Foliage swaying in the
        # wind, a bug crawling the lens, or a noise blob stitched into a "track"
        # wanders back and forth -- net displacement collapses to a fraction of
        # the path length. Invariant to blob shape, so it catches the wind-in-the-
        # trees phantom the aspect/size proxies can't. (0 or an undefined
        # straightness -> skipped.)
        min_straight = self.min_straightness
        straightness = getattr(result, "straightness", None)
        if (min_straight > 0 and straightness is not None
                and straightness < min_straight):
            return ("rejected",
                    f"path wandered — straight-line travel was only "
                    f"{straightness * 100:.0f}% of the {result.distance_m:.0f} m "
                    f"it tracked (a vehicle goes straight; likely foliage/noise)")
        # Monotonic progress: a real vehicle only ever moves forward down the
        # road, so nearly every step advances along its net direction of travel.
        # Foliage swaying or a noise blob stitched into a track reverses often, so
        # its forward fraction collapses toward ~0.5. Catches a path straight
        # enough to beat the straightness gate that still oscillates along its
        # axis. (0 or an undefined monotonicity -> skipped.)
        min_mono = self.min_monotonicity
        monotonicity = getattr(result, "monotonicity", None)
        if (min_mono > 0 and monotonicity is not None
                and monotonicity < min_mono):
            return ("rejected",
                    f"path reversed — only {monotonicity * 100:.0f}% of steps "
                    f"moved forward (a vehicle never backs up; likely "
                    f"foliage/noise)")
        # Acceleration bound: a real vehicle changes speed smoothly (a few m/s^2
        # even braking hard); a phantom teleporting between noise blobs implies an
        # impossible frame-to-frame jump. (0 or an undefined accel -> skipped.)
        max_acc = self.max_accel_mps2
        accel = getattr(result, "max_accel_mps2", None)
        if max_acc > 0 and accel is not None and accel > max_acc:
            return ("rejected",
                    f"impossible acceleration ({accel:.0f} m/s² frame-to-frame, "
                    f"max plausible {max_acc:.0f}) — teleporting track, "
                    f"not a vehicle")
        # Area coherence: a vehicle's silhouette changes size smoothly across the
        # scene; noise and wind-blown foliage flicker. A high coefficient of
        # variation in the bbox area is the flicker signature. Only fires on
        # egregious flicker, so it never rejects a real car. (0 or too-few-samples
        # -> skipped.)
        max_acv = self.max_area_cv
        if max_acv > 0 and area_cv is not None and area_cv > max_acv:
            return ("rejected",
                    f"blob size flickered ({area_cv * 100:.0f}% variation) — "
                    f"a vehicle's outline changes smoothly (likely foliage/noise)")
        # Shape next: a car is wider than it is tall on a side-on camera; a
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
        if self._gate_active():
            # Bounded queue: under a rare burst of back-to-back traffic that
            # outruns inference, _finalize commits the overflow inline
            # (geometry-only) rather than blocking detection -- see _finalize.
            self._recog_q = queue.Queue(maxsize=8)
            self._recog_thread = threading.Thread(
                target=self._recog_worker, name="speedkam-recog", daemon=True)
            self._recog_thread.start()
            print(f"[SpeedKam] YOLO gate ON (imgsz={self.recognizer.imgsz}, "
                  f"vote {self.recognizer.min_vehicle_frames}/"
                  f"{self.recognizer.vote_frames} frames) -- car-vs-not runs on "
                  f"a worker thread.")
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
            if self._recog_thread is not None:
                # Drain: let queued passes finish classifying so their rows land.
                try:
                    self._recog_q.put(None, timeout=1.0)
                except queue.Full:
                    pass
                self._recog_thread.join(timeout=10.0)
                self._recog_thread = None
                self._recog_q = None
            self.camera.release()
            if show:
                cv2.destroyAllWindows()
            dur = time.monotonic() - t_start
            if dur > 0:
                print(f"[SpeedKam] Processed {frames} frames in {dur:.1f}s "
                      f"({frames / dur:.1f} FPS).")

    # ------------------------------------------------------------ frame work
    # ------------------------------------------------------------- light gate
    @staticmethod
    def _measure_brightness(img):
        """Mean luma (0-255) of a frame -- averaged over whatever channels it
        has (BGR or a single-plane detection frame). cv2.mean is a fast C reduce,
        so this is negligible next to detection even at full resolution."""
        if img is None:
            return None
        m = cv2.mean(img)               # (c0, c1, c2, c3); unused channels are 0
        chans = img.shape[2] if img.ndim == 3 else 1
        return sum(m[:chans]) / max(1, chans)

    def _update_light_gate(self, t, brightness):
        """Advance the day/night state machine and return True while paused.

        Hysteresis + dwell: fall asleep only after brightness holds below
        ``sleep_below`` for ``dwell_seconds``, and wake only after it holds above
        ``wake_above`` for the same -- so a passing headlight or a dark truck
        can't toggle the gate, and dusk/dawn cross the dead-band once."""
        if not self._lg.get("enabled", True) or brightness is None:
            self.paused_low_light = False
            return False
        sleep_below = float(self._lg.get("sleep_below", 40))
        wake_above = float(self._lg.get("wake_above", 60))
        dwell = float(self._lg.get("dwell_seconds", 30))
        # Condition that would flip the current state, and the log line for it.
        if self.paused_low_light:
            pending = brightness > wake_above
        else:
            pending = brightness < sleep_below
        if not pending:
            self._lg_since = None
            return self.paused_low_light
        if self._lg_since is None:
            self._lg_since = t
        elif t - self._lg_since >= dwell:
            self.paused_low_light = not self.paused_low_light
            self._lg_since = None
            if self.paused_low_light:
                print(f"[SpeedKam] Light gate: scene too dark "
                      f"(brightness {brightness:.0f} < {sleep_below:.0f}) -- "
                      f"pausing detection until daylight.")
            else:
                print(f"[SpeedKam] Light gate: daylight restored "
                      f"(brightness {brightness:.0f} > {wake_above:.0f}) -- "
                      f"resuming detection.")
        return self.paused_low_light

    def _process_frame(self, t, frame, detect_frame, frame_callback,
                       draw_debug, show):
        """Detect -> track -> finalize one frame and publish the view.

        Shared by the single-threaded and parallel run paths. Does NOT push to
        the recorder (the caller owns that, so the parallel capture thread can
        record every frame while this may see only the latest). Returns False
        only when the desktop window asked to quit ('q'); True otherwise.
        """
        self.frame_wh = (frame.shape[1], frame.shape[0])

        # Low-light gate: measure scene brightness (cheap, on the tiny detection
        # frame) and, if it's too dark to work, skip detection/tracking entirely.
        # No detections => no tracks => no phantom night readings and no clips.
        # The preview still publishes so the live view shows the dark scene and a
        # "paused" banner; the gate wakes itself when light returns at dawn.
        self.scene_brightness = self._measure_brightness(
            detect_frame if detect_frame is not None else frame)
        if self._update_light_gate(t, self.scene_brightness):
            self._last_result_text = "PAUSED - low light (waiting for daylight)"
            self._last_over = False
            if show:
                view = self.render_preview(frame, self._overlay([], draw_debug))
                cv2.imshow("SpeedKam", view)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    return False
            elif frame_callback is not None:
                frame_callback(frame, self._overlay([], draw_debug))
            return True

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

        # Geometry signals for the false-positive gates. Computed here (on the
        # detect thread, while the track object is fresh) and carried in the job.
        job = {
            "track": track,
            "result": result,
            "span_m": self._vehicle_span_m(track),
            "aspect": self._aspect_ratio(track),
            "on_road_frac": self._on_road_fraction(track),
            "area_cv": self._area_cv(track),
            "brightness": self.scene_brightness,
            # Finalize time, so the recognition worker's dedupe/last-count timing
            # reflects when the pass ended, not when its (delayed) vote committed.
            "t_finalize": time.monotonic(),
        }

        # Grab the clip frames NOW, while the vehicle is still fresh in the ring
        # buffer. Media saving is deferred to _commit_reading, which for a gated
        # node runs on the recognition worker ~vote_frames inferences later -- by
        # then the shallow (~1-2s) buffer has rotated PAST the car, so a clip
        # encoded then is empty road. Holding the frame references here (cheap --
        # no copy) means the deferred encode still shows the vehicle. center_t is
        # the mid-pass time, so the snapshot is framed on the car, not the buffer
        # middle (which on a fast pass is empty).
        if self.recorder is not None and track.samples:
            center_t = track.samples[len(track.samples) // 2].t
            half = float(self.cfg["recording"].get("clip_seconds", 8)) / 2.0
            job["center_t"] = center_t
            job["clip_frames"] = self.recorder.grab_window(center_t, half)
        else:
            job["center_t"] = None
            job["clip_frames"] = None

        # When the YOLO gate is active, hand the pass to the worker thread so the
        # ~vote_frames inferences never stall the detection loop. The buffered
        # clip frames the vote reads live for ~1.5x clip_seconds, so a verdict a
        # second or two later still sees them. Under a burst that fills the queue,
        # commit inline (geometry-only) rather than block detection or drop it.
        if self._recog_q is not None:
            try:
                self._recog_q.put_nowait(job)
                return
            except queue.Full:
                print("[SpeedKam] recognition queue full -- committing "
                      f"#{track.id} on geometry alone (traffic burst).")
        self._commit_reading(job, verdict=None)

    # ------------------------------------------------------------ gate worker
    def _gate_active(self):
        """True when the on-node YOLO car-vs-not gate should run (real detector
        loaded, gate enabled, and a recorder to read pass frames from)."""
        r = self.recognizer
        return bool(getattr(r, "gate_enabled", False)
                    and getattr(r, "active", False)
                    and getattr(r, "can_classify", False)
                    and self.recorder is not None)

    def _recog_worker(self):
        """Pop finished passes, run the pass-level vote, then commit the reading.
        Runs off the detection loop so classification latency never throttles
        capture/detect. FIFO, so dedupe/counter ordering matches arrival order."""
        while True:
            job = self._recog_q.get()
            try:
                if job is None:
                    break
                verdict = None
                try:
                    verdict = self._yolo_pass(job["track"])
                except Exception as exc:  # noqa: BLE001 - never kill the worker
                    print(f"[SpeedKam] recognition vote failed: {exc}")
                try:
                    self._commit_reading(job, verdict)
                except Exception as exc:  # noqa: BLE001
                    print(f"[SpeedKam] commit failed for "
                          f"#{job['track'].id}: {exc}")
            finally:
                self._recog_q.task_done()

    def _yolo_pass(self, track):
        """Sample up to ``vote_frames`` frames spanning the finished track and
        tally a pass-level car-vs-not verdict. None when there's nothing to
        score (no recorder / no samples / no buffered frames)."""
        if self.recorder is None or not track.samples:
            return None
        from .recognition import _crop
        samples = track.samples
        n = max(1, self.recognizer.vote_frames)
        if len(samples) <= n:
            chosen = samples
        else:
            step = len(samples) / n
            chosen = [samples[min(len(samples) - 1, int(i * step))]
                      for i in range(n)]
        crops = []
        for s in chosen:
            frame = self.recorder.frame_at(s.t)
            if frame is None:
                continue
            crop = _crop(frame, s.bbox)
            if crop is not None:
                crops.append(crop)
        if not crops:
            return None
        return self._yolo_vote(crops)

    def _yolo_vote(self, crops):
        """Tally per-frame classify() results into a pass verdict dict. Pure
        (no I/O), so tests can drive it with a stubbed recognizer.classify."""
        v = {"frames": 0, "vehicle_frames": 0, "nuisance_frames": 0,
             "best_vehicle_label": None, "best_vehicle_conf": 0.0,
             "best_nuisance_label": None, "best_nuisance_conf": 0.0}
        for crop in crops:
            r = self.recognizer.classify(crop)
            v["frames"] += 1
            vc = float(r.get("vehicle_conf") or 0.0)
            nc = float(r.get("nuisance_conf") or 0.0)
            if vc > 0:
                v["vehicle_frames"] += 1
                if vc > v["best_vehicle_conf"]:
                    v["best_vehicle_conf"] = vc
                    v["best_vehicle_label"] = r.get("vehicle_label")
            elif nc > 0:
                v["nuisance_frames"] += 1
            if nc > v["best_nuisance_conf"]:
                v["best_nuisance_conf"] = nc
                v["best_nuisance_label"] = r.get("nuisance_label")
        return v

    def _yolo_gate(self, verdict, brightness):
        """Car-vs-not decision from a pass verdict. Returns (decision, info):
          ("keep", vehicle_label)   -- enough frames showed a vehicle
          ("reject", reason)        -- zero vehicle frames in good light (phantom
                                       / on-road pedestrian geometry can't catch)
          ("fallback", None)        -- unsure (too few frames, too dark, or the
                                       vote was inconclusive) -> geometry decides
        """
        if verdict is None or verdict.get("frames", 0) == 0:
            return ("fallback", None)
        k = max(1, self.recognizer.min_vehicle_frames)
        vf = verdict.get("vehicle_frames", 0)
        frames = verdict["frames"]
        if vf >= k:
            return ("keep", verdict.get("best_vehicle_label"))
        if vf == 0:
            # Zero vehicles across the whole pass. Trust this as a hard reject
            # only in good light -- in the dark YOLO is unreliable, so defer to
            # geometry (the low-light gate usually pauses detection first).
            floor = self.recognizer.min_reject_brightness
            if brightness is not None and floor > 0 and brightness < floor:
                return ("fallback", None)
            nlabel = verdict.get("best_nuisance_label")
            if nlabel:
                return ("reject",
                        f"classified as a {nlabel} "
                        f"({verdict.get('best_nuisance_conf', 0.0):.2f}) — "
                        f"no vehicle in {frames} frames, not a car")
            return ("reject",
                    f"no vehicle detected in {frames} frames — "
                    f"phantom (empty road)")
        # Nonzero but below the vote threshold: inconclusive.
        if self.recognizer.fallback == "reject":
            return ("reject",
                    f"only {vf}/{frames} frames showed a vehicle "
                    f"(need {k}) — likely not a car")
        if self.recognizer.fallback == "keep":
            return ("keep", verdict.get("best_vehicle_label"))
        return ("fallback", None)

    # --------------------------------------------------------------- commit
    def _commit_reading(self, job, verdict):
        """Apply the gates, then log/record/count/mirror a finished pass. Called
        inline (geometry-only nodes) or from the recognition worker (with a YOLO
        verdict). Everything after speed estimation lives here."""
        track = job["track"]
        result = job["result"]
        span_m = job["span_m"]
        aspect = job["aspect"]
        on_road_frac = job["on_road_frac"]
        area_cv = job.get("area_cv")

        over = result.speed_kmh > self.limit_kmh
        display_speed = self._display_speed(result)

        # False-positive gates. Geometry runs first (location + speed sanity); it
        # can REJECT. Then the YOLO gate is the decider for car-vs-not: it can
        # reject a geometrically-plausible phantom/pedestrian that geometry kept,
        # and it confirms real cars (filling vehicle_type). A geometry reject
        # stands regardless -- an object must clear BOTH to be counted.
        status, reason = self._classify_reading(result, span_m, aspect,
                                                on_road_frac, area_cv)
        yolo_type = None
        if status == "ok" and verdict is not None:
            decision, info = self._yolo_gate(verdict, job.get("brightness"))
            if decision == "reject":
                status, reason = "rejected", info
            elif decision == "keep":
                yolo_type = info
        # Count-once-per-drive-by: a real pass that repeats the last counted one
        # (same direction, within dedupe_seconds) is a fragmented re-detection of
        # the same vehicle -> drop it as a duplicate.
        if status == "ok":
            dup = self._is_duplicate(result, now=job.get("t_finalize"))
            if dup:
                status, reason = "rejected", dup
        rejected = status == "rejected"
        # A rejected reading can never be a "captured speeder"; keep a snapshot
        # only when we'd have kept one anyway, so the bin has something to show.
        capture = (not rejected) and self._should_capture(display_speed)

        # Best-effort attributes (type/make/model/year/color) -- run for EVERY
        # counted pass, even ones below the SpeedKapture threshold. When the YOLO
        # vote already ran, reuse its vehicle_type instead of a second inference.
        attrs = self._recognize(track, yolo_type=yolo_type)

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
        road_txt = (f", on_road={on_road_frac * 100:.0f}%"
                    if on_road_frac is not None else "")
        straight = getattr(result, "straightness", None)
        straight_txt = f", straight={straight * 100:.0f}%" if straight is not None else ""
        mono = getattr(result, "monotonicity", None)
        mono_txt = f", fwd={mono * 100:.0f}%" if mono is not None else ""
        accel = getattr(result, "max_accel_mps2", None)
        accel_txt = f", accel={accel:.0f}" if accel else ""
        area_txt = f", area_cv={area_cv:.2f}" if area_cv is not None else ""
        vote_txt = ""
        if verdict is not None:
            vote_txt = (f", yolo={verdict.get('vehicle_frames', 0)}/"
                        f"{verdict.get('frames', 0)}veh")
        print(f"[SpeedKam] {tag} vehicle #{track.id}: {result.display(self.units)} "
              f"({result.direction}, {result.distance_m:.1f} m{span_txt}"
              f"{aspect_txt}{road_txt}{straight_txt}{mono_txt}{accel_txt}"
              f"{area_txt}{vote_txt}, "
              f"conf={result.confidence}){extra}"
              + (f"  [REJECTED: {reason}]" if rejected
                 else ("" if capture else "  [not captured]")))

        # Save a clip only when above the SpeedKapture threshold AND not rejected;
        # always log the row so counts + attributes are recorded even for
        # uncaptured passes. Rejected readings still keep a snapshot for review.
        clip_name = snap_name = None
        clip_frames = job.get("clip_frames")
        center_t = job.get("center_t")
        if self.recorder is not None:
            if capture:
                clip, snap = self.recorder.save_media(
                    track.id, result, self.units, self.limit_kmh,
                    self.cfg["recording"]["burn_overlay"],
                    frames=clip_frames, center_t=center_t,
                )
                clip_name = clip.name if clip else None
                snap_name = snap.name if snap else None
                if clip:
                    print(f"[SpeedKam]    saved {clip.name}")
            elif rejected or self.cfg["recording"].get("always_snapshot"):
                # No clip, but keep a JPEG: for rejects so the bin is reviewable,
                # for sub-threshold passes so a deferred worker can enrich later.
                snap = self.recorder.save_snapshot_only(
                    track.id, result, self.units, self.limit_kmh,
                    frames=clip_frames, center_t=center_t)
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
            # the same vehicle can be deduped (count-once-per-drive-by). Keyed to
            # the finalize time so dedupe spacing is independent of vote latency.
            self._last_count = {"t": job.get("t_finalize") or time.monotonic(),
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
    def _recognize(self, track, yolo_type=None):
        """Representative-frame attributes (color always; vehicle_type from the
        YOLO vote when it already ran, else from the single-crop recognizer).

        ``yolo_type`` avoids a second inference: when the gate's pass-level vote
        already classified the vehicle, we reuse its label and only run the cheap
        OpenCV color pass here."""
        from .recognition import EMPTY, _crop, estimate_color
        if not self.recognizer.active or self.recorder is None or not track.samples:
            out = dict(EMPTY)
            if yolo_type:
                out["vehicle_type"] = yolo_type
            return out
        sample = track.samples[len(track.samples) // 2]  # near mid-pass
        frame = self.recorder.frame_at(sample.t)
        if frame is None:
            out = dict(EMPTY)
            if yolo_type:
                out["vehicle_type"] = yolo_type
            return out
        # Gate path: YOLO already ran in the vote. Reuse its type; just add color.
        if yolo_type is not None or self._recog_q is not None:
            out = dict(EMPTY)
            out["vehicle_type"] = yolo_type
            if self.recognizer.want_color:
                crop = _crop(frame, sample.bbox)
                try:
                    out["color"] = (estimate_color(crop)
                                    if crop is not None else None)
                except Exception:  # noqa: BLE001 - color is best-effort
                    pass
            return out
        return self.recognizer.recognize(frame, sample.bbox)

    @staticmethod
    def _attr_label(attrs):
        bits = [attrs.get("color"), attrs.get("vehicle_type"),
                attrs.get("make"), attrs.get("model"), attrs.get("year")]
        text = " ".join(str(b) for b in bits if b)
        return f"  {text}" if text else ""
