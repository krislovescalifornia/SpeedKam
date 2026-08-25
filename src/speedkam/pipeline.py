# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Main processing pipeline: capture -> detect -> track -> speed -> record.

Can be driven two ways:
  * run() with a cv2 preview window (desktop, run.py), or
  * run(frame_callback=..., stop_event=...) headless, publishing each annotated
    frame to a callback (used by the web dashboard, serve.py).
Both share the exact same detection/tracking/speed logic.

Speed is the two-line crossing-time estimator (speed.estimate): a car's speed
is a per-direction calibrated distance over the time it takes to cross between
two fixed image columns. It uses raw pixel x + timestamps -- no homography, no
lens undistortion. False positives (cyclists, pedestrians, wind-blown foliage,
noise blobs) are thrown out by cheap PIXEL-ONLY gates: car-shape aspect, pixel
width, bounding-box area coherence, and a count-once drive-by dedupe.
"""
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from . import annotate, speed as speed_mod
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

        # SpeedKapture threshold (display units), the road's speed limit (km/h),
        # and the pixel-only false-positive gate thresholds. All seeded from
        # config, then overridable live from either dashboard (LAN or off-site
        # fleet) and persisted per node across restarts in the runtime state file.
        self.state = RuntimeState(
            cfg["recording"].get("state_file", "captures/runtime.json"),
            {"speedkapture_threshold":
                float(cfg["recording"].get("speedkapture_threshold", 0) or 0),
             # "My Road Speed Limit": what counts as over-limit / SPEEDING.
             # Stored in km/h (the internal unit); the UI edits it in whatever
             # display_units the node uses and converts on the way in/out.
             "speed_limit_kmh": float(cfg["speed"]["speed_limit_kmh"]),
             # --- false-positive auto-reject envelope (all PIXEL-ONLY) --------
             # Car-shape gate: minimum bbox aspect ratio (width/height). On a
             # side-on camera a car's box is WIDE (~2-3) while a walking person's
             # is TALL (~0.3-0.5) and a side-on cyclist ~square (~0.9).
             "min_vehicle_aspect":
                 float(cfg["speed"].get("min_vehicle_aspect", 0) or 0),
             # Size gate: minimum bbox pixel WIDTH for a real car. A car is a big
             # object even in the far lane; a dog/bike/pedestrian is small. Paired
             # with the aspect (shape) check, nothing that isn't a car clears both.
             "min_car_width_px":
                 float(cfg["speed"].get("min_car_width_px", 0) or 0),
             # Area-coherence gate: max coefficient of variation (std/mean) of the
             # bbox area across the pass. A vehicle's silhouette changes size
             # smoothly (low CV); noise and wind-blown foliage flicker (high CV).
             "max_area_cv":
                 float(cfg["speed"].get("max_area_cv", 0) or 0),
             # Count each drive-by ONCE: a second confirmed pass in the same
             # direction within this many seconds is a fragmented re-detection.
             "dedupe_seconds":
                 float(cfg["speed"].get("dedupe_seconds", 0) or 0),
             # Last off-site settings revision we've applied (see RemoteControl).
             "remote_rev": None},
        )

        # The car-only false-positive gate is FIXED POLICY, not a live setting.
        # Re-assert the code values from config on every boot so a stale or
        # hand-edited runtime.json can never drift the filter, and so there is
        # no dashboard / HTTP / fleet-sync path left that can change it. The
        # numbers live in config.yaml (mirrored in config.py) -- edit them there.
        for _k in ("min_vehicle_aspect", "min_car_width_px",
                   "max_area_cv", "dedupe_seconds"):
            self.state.set(_k, float(cfg["speed"].get(_k, 0) or 0))

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

        # --- road-band detection ROI + audit ------------------------------
        # Restrict MOG2 to the strip of road that matters (big FPS win), OR just
        # AUDIT: run full-frame detection unchanged while recording whether every
        # counted car would have stayed inside a candidate ROI. Ships OFF; the
        # rollout proves coverage before ever cropping. See docs/roi-rollout-log.md.
        self._det_roi, self._roi_audit = self._configure_roi(cfg)
        # Observed vehicle envelope (fractions of the full frame) accumulated over
        # counted passes during audit, plus per-pass coverage stats. Persisted to
        # roi_audit.json so the correct band can be derived from real traffic.
        self._roi_env = None           # [x0,y0,x1,y1] fractions, min/max of ground pts
        self._roi_audit_passes = 0
        self._roi_audit_covered = 0    # passes with 100% of ground points inside cand.
        self._roi_audit_min_cov = 1.0  # worst single-pass coverage fraction seen

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
    def min_vehicle_aspect(self) -> float:
        """Min bbox aspect ratio (width/height) for a real vehicle. On a side-on
        camera a car's box is WIDE (w/h ~2-3) while a walking person's is TALL
        (~0.3-0.5) and a side-on cyclist ~square (~0.9) -- so this is the single
        most reliable 'is it car-shaped?' check, and (being pixel-only) a person
        close to the lens can't fool it. 0 disables the gate."""
        return float(self.state.get("min_vehicle_aspect") or 0)

    @property
    def min_car_width_px(self) -> float:
        """Min bbox pixel width for a real car. A car is a big object even in the
        far lane; a bike, dog or pedestrian is small. The size half of the car
        filter (paired with min_vehicle_aspect). 0 disables."""
        return float(self.state.get("min_car_width_px") or 0)

    @property
    def max_area_cv(self) -> float:
        """Max coefficient of variation (std/mean) of the bbox area across a pass.
        A vehicle's silhouette changes size smoothly with perspective (low CV);
        noise and swaying foliage flicker in size (high CV). Above this the track
        is auto-rejected. 0 disables the gate. See _area_cv."""
        return float(self.state.get("max_area_cv") or 0)

    @property
    def dedupe_seconds(self) -> float:
        """Count a drive-by ONCE: a second confirmed pass in the SAME direction
        finishing within this many seconds of the last counted one is treated as
        the same vehicle (a fragmented track) and rejected. 0 disables it."""
        return float(self.state.get("dedupe_seconds") or 0)

    # NOTE: the car-filter thresholds above are read-only at runtime by design.
    # They are fixed policy seeded from config on boot (see __init__); there is
    # deliberately no setter, so no dashboard or remote path can retune them.

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
    def _vehicle_width_px(track):
        """Median bbox WIDTH in pixels across the track. A car is a big object
        even in the far lane; a dog, bicycle or pedestrian is small. Pixel-only,
        so it's the size half of the car filter -- paired with the aspect (shape)
        check, it cleanly rejects everything that isn't a car. The median shrugs
        off the odd merged/split frame."""
        widths = [s.bbox[2] for s in track.samples
                  if len(s.bbox) == 4 and s.bbox[2] > 0]
        return float(np.median(widths)) if widths else None

    @staticmethod
    def _area_cv(track):
        """Coefficient of variation (std/mean) of the bbox area across the track,
        or None when there are too few samples to judge.

        A real vehicle's silhouette grows and shrinks smoothly as it crosses the
        scene, so its area has a low CV. Wind-blown foliage and sensor noise
        stitched into a track flicker wildly in size, so their CV is high. Pixel-
        only, so a foreground phantom can't hide from it. Needs a few samples to
        be meaningful."""
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

    def _classify_reading(self, result, aspect, area_cv=None, width_px=None):
        """Auto-reject anything that isn't a car so junk never pollutes the stats.
        The car filter is cheap PIXEL-ONLY checks -- a car is WIDE (aspect) and
        BIG (width) and its silhouette changes size smoothly (area CV); a person
        is tall, a dog/bike/tree is small or flickery -- plus the two-line
        crossing requirement (foliage that never traverses the road is never even
        timed). Returns (status, reason): "ok" for a real vehicle, else "rejected"
        with a human explanation (shown in the dashboards' bin)."""
        # Shape: a car is wider than it is tall on a side-on camera; a
        # pedestrian/dog-walker/cyclist is not. Pixel-only, so a person close to
        # the lens is still caught.
        min_aspect = self.min_vehicle_aspect
        if min_aspect > 0 and aspect is not None and aspect < min_aspect:
            kind = ("a person/pedestrian" if aspect < 0.8
                    else "a cyclist or pedestrian")
            return ("rejected",
                    f"shape {aspect:.1f}w:1h — taller than a car, likely {kind}")
        # Size: a car is a big object even in the far lane; a bike, dog or
        # pedestrian is small. Pixel width is the size half of the car filter --
        # with the aspect (shape) check above, nothing that isn't a car survives
        # both.
        min_width = self.min_car_width_px
        if min_width > 0 and width_px is not None and width_px < min_width:
            return ("rejected",
                    f"object only {width_px:.0f}px wide — smaller than a car "
                    f"(likely a bike, dog, or pedestrian)")
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
        return ("ok", "")

    def _display_speed(self, result) -> float:
        return result.speed_mph if self.units == "mph" else result.speed_kmh

    # ------------------------------------------------------ detection ROI
    def _configure_roi(self, cfg):
        """Resolve the road-band ROI + audit flags from config, fail-safe.

        Returns ``(det_roi, audit)`` where ``det_roi`` is ``(x0,y0,x1,y1)``
        fractions to crop detection to, or ``None`` for full-frame (the safe
        default). Also stashes the candidate band + frame size on self for the
        audit. NEVER raises and NEVER enables a band that fails validation -- a
        bad config degrades to full-frame detection, never to blind detection.
        """
        self._cam_wh = (float(cfg["camera"]["width"]), float(cfg["camera"]["height"]))
        self._roi_cand = None
        det_roi = None
        audit = False
        try:
            roi = (cfg.get("detection", {}) or {}).get("roi", {}) or {}
            audit = bool(roi.get("audit"))
            x0 = float(roi.get("x0", 0.0)); y0 = float(roi.get("y0", 0.0))
            x1 = float(roi.get("x1", 1.0)); y1 = float(roi.get("y1", 1.0))
            full = x0 <= 0.0 and y0 <= 0.0 and x1 >= 1.0 and y1 >= 1.0
            if not full and x1 > x0 and y1 > y0:
                self._roi_cand = (x0, y0, x1, y1)   # candidate for audit coverage
            if roi.get("enabled"):
                if full or not (x1 > x0 and y1 > y0):
                    print("[SpeedKam] detection ROI enabled but spans the whole "
                          "frame (or is empty) -- running full-frame (no-op).")
                elif not self._roi_contains_crossing(x0, x1):
                    W = self._cam_wh[0]
                    xa = float(cfg["speed"].get("x_a", 0)); xb = float(cfg["speed"].get("x_b", 0))
                    print("[SpeedKam] *** detection ROI REFUSED: band x "
                          f"[{x0 * W:.0f},{x1 * W:.0f}]px does not contain the "
                          f"crossing columns x_a={xa:.0f}/x_b={xb:.0f} -- speed "
                          "would break. Running full-frame instead. ***")
                else:
                    det_roi = (x0, y0, x1, y1)
                    W, H = self._cam_wh
                    print(f"[SpeedKam] detection ROI ON: x[{x0 * W:.0f},{x1 * W:.0f}] "
                          f"y[{y0 * H:.0f},{y1 * H:.0f}]px of {int(W)}x{int(H)} "
                          f"(~{(x1 - x0) * (y1 - y0) * 100:.0f}% of pixels).")
        except Exception as exc:  # noqa: BLE001 - never let ROI config crash a node
            print(f"[SpeedKam] detection ROI config error ({exc}); full-frame.")
            return None, False
        if audit:
            print("[SpeedKam] detection ROI AUDIT on: full-frame detection "
                  "unchanged; recording counted-car coverage + envelope.")
        return det_roi, audit

    def _roi_contains_crossing(self, x0_frac, x1_frac):
        """True when the band's x-span covers BOTH crossing columns x_a/x_b, so
        the crossing-time speed can still be measured inside the ROI."""
        W = self._cam_wh[0]
        xa = float(self.cfg["speed"].get("x_a", 0))
        xb = float(self.cfg["speed"].get("x_b", 0))
        lo, hi = min(xa, xb), max(xa, xb)
        return x0_frac * W <= lo and x1_frac * W >= hi

    def _roi_audit_pass(self, track):
        """Record a counted car against the ROI audit: grow the observed vehicle
        envelope (min/max ground point, as frame fractions) and, if a candidate
        band is configured, the fraction of this pass's ground points inside it.

        This is how we PROVE, on real traffic, that a candidate band would not
        have dropped any car before we ever enable the crop. Observational only;
        it never touches detection. Best-effort -- a failure here must not affect
        counting."""
        try:
            W, H = self._cam_wh
            pts = [(s.ground_px[0] / W, s.ground_px[1] / H)
                   for s in track.samples if len(s.ground_px) == 2]
            if not pts:
                return
            xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
            box = [min(xs), min(ys), max(xs), max(ys)]
            if self._roi_env is None:
                self._roi_env = box
            else:
                e = self._roi_env
                self._roi_env = [min(e[0], box[0]), min(e[1], box[1]),
                                 max(e[2], box[2]), max(e[3], box[3])]
            self._roi_audit_passes += 1
            if self._roi_cand is not None:
                cx0, cy0, cx1, cy1 = self._roi_cand
                inside = sum(1 for (px, py) in pts
                             if cx0 <= px <= cx1 and cy0 <= py <= cy1)
                cov = inside / len(pts)
                if cov >= 0.999:
                    self._roi_audit_covered += 1
                self._roi_audit_min_cov = min(self._roi_audit_min_cov, cov)
                if cov < 0.999:
                    print(f"[SpeedKam] ROI-AUDIT: car #{track.id} coverage "
                          f"{cov * 100:.0f}% -- {len(pts) - inside}/{len(pts)} "
                          "ground points OUTSIDE the candidate band.")
            self._write_roi_audit()
        except Exception as exc:  # noqa: BLE001
            print(f"[SpeedKam] ROI audit error (ignored): {exc}")

    def _write_roi_audit(self):
        """Persist the audit envelope + coverage so the correct band can be read
        off real traffic (and survives a restart)."""
        import json
        W, H = self._cam_wh
        env = self._roi_env
        # A safe recommended band = observed envelope padded, clamped to [0,1],
        # and always widened to include the crossing columns x_a/x_b.
        rec = None
        if env is not None:
            mx, my = 0.06, 0.10        # x pad 6%, y pad 10% (generous vertical)
            xa = float(self.cfg["speed"].get("x_a", 0)) / W
            xb = float(self.cfg["speed"].get("x_b", 0)) / W
            rec = [max(0.0, min(env[0] - mx, min(xa, xb) - 0.02)),
                   max(0.0, env[1] - my),
                   min(1.0, max(env[2] + mx, max(xa, xb) + 0.02)),
                   min(1.0, env[3] + my)]
        out = {
            "passes": self._roi_audit_passes,
            "observed_envelope_frac": env,
            "recommended_band_frac": rec,
            "candidate_band_frac": list(self._roi_cand) if self._roi_cand else None,
            "candidate_100pct_covered_passes": self._roi_audit_covered,
            "candidate_worst_coverage": (None if self._roi_audit_passes == 0
                                         else round(self._roi_audit_min_cov, 4)),
            "frame_wh": [int(W), int(H)],
        }
        try:
            path = Path(self.cfg["recording"]["output_dir"]) / "roi_audit.json"
            path.write_text(json.dumps(out, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001 - audit persistence is best-effort
            pass

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
            detections, _ = self.detector.detect(detect_frame, upscale=upscale,
                                                 roi=self._det_roi)
        else:
            detections, _ = self.detector.detect(frame, roi=self._det_roi)
        active, finished = self.tracker.update(detections, t)

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
        # The crossing-time engine measures speed between two image columns
        # (speed.estimate x_a/x_b) from raw pixels, so the live view is the raw
        # feed plus just the detection boxes (real) and the HUD status line.
        if overlay.get("draw_debug"):
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
    def _finalize(self, track):
        result = speed_mod.estimate(track, self.cfg["speed"])
        if result is None:
            # No speed (uncalibrated direction / too-short track / didn't cross
            # the measured stretch): nothing to count.
            self._last_result_text = f"#{track.id}: (no speed)"
            self._last_over = False
            return

        # Pixel-only geometry signals for the false-positive gates. Computed here
        # (on the detect thread, while the track object is fresh) and carried in
        # the job.
        job = {
            "track": track,
            "result": result,
            "aspect": self._aspect_ratio(track),
            "width_px": self._vehicle_width_px(track),
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
        aspect = job["aspect"]
        area_cv = job.get("area_cv")
        width_px = job.get("width_px")

        over = result.speed_kmh > self.limit_kmh
        display_speed = self._display_speed(result)

        # False-positive gates. Geometry (pixel shape/size/coherence) runs first;
        # it can REJECT. Then the YOLO gate is the decider for car-vs-not: it can
        # reject a geometrically-plausible phantom/pedestrian that geometry kept,
        # and it confirms real cars (filling vehicle_type). A geometry reject
        # stands regardless -- an object must clear BOTH to be counted.
        status, reason = self._classify_reading(result, aspect, area_cv, width_px)
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

        # Best-effort attributes (type/color) -- run for EVERY counted pass, even
        # ones below the SpeedKapture threshold. When the YOLO vote already ran,
        # reuse its vehicle_type instead of a second inference.
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
        aspect_txt = f", aspect={aspect:.2f}" if aspect is not None else ""
        width_txt = f", width={width_px:.0f}px" if width_px is not None else ""
        area_txt = f", area_cv={area_cv:.2f}" if area_cv is not None else ""
        vote_txt = ""
        if verdict is not None:
            vote_txt = (f", yolo={verdict.get('vehicle_frames', 0)}/"
                        f"{verdict.get('frames', 0)}veh")
        print(f"[SpeedKam] {tag} vehicle #{track.id}: {result.display(self.units)} "
              f"({result.direction}, {result.distance_m:.1f} m{aspect_txt}"
              f"{width_txt}{area_txt}{vote_txt}, "
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
            # ROI audit: record this real, counted car's ground-point envelope so
            # we can prove a candidate band would not have dropped it. No effect
            # on detection -- observational only.
            if self._roi_audit:
                self._roi_audit_pass(track)
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
