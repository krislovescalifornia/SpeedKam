# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Web dashboard: live view, recent clips, stats, and browser calibration.

A background thread runs the SpeedCamera pipeline and publishes each annotated
frame here; Flask serves an MJPEG live view, a REST API, the capture files, and
a click-to-calibrate page. This is what makes the headless Pi usable from a
phone/laptop -- browse to http://<pi-ip>:8080.
"""
from __future__ import annotations

import csv
import hmac
import io
import subprocess
import threading
import time
from pathlib import Path

import cv2
from datetime import date, timedelta

from flask import (Flask, Response, jsonify, request, send_file,
                   send_from_directory)

from .calibration import Calibration
from . import driveby
from .pipeline import SpeedCamera
from .recorder import CSV_COLUMNS

KMH_PER_MS = 3.6
MPH_PER_MS = 2.2369362920544

WEBUI_DIR = Path(__file__).parent / "webui"


class Runner:
    """Owns the pipeline thread and the latest frames for the web layer."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.speedcam = SpeedCamera(cfg)
        self.captures_dir = Path(cfg["recording"]["output_dir"]).resolve()
        self.csv_path = Path(cfg["logging"]["csv_file"]).resolve()
        self.calibration_file = cfg["speed"]["calibration_file"]

        self._latest_jpeg = None       # annotated, for the live stream
        self._latest_raw = None        # clean frame, for calibration snapshot
        self._last_frame_ts = None     # monotonic time of the last frame (freshness)
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread = None

        # Live preview runs entirely on ITS OWN thread (see _encode_loop): the
        # copy + annotate + JPEG-encode all happen here, off the capture/detect
        # loop, so on a multi-core Pi it uses an otherwise-idle core. _on_frame
        # just hands off the newest (raw frame, overlay snapshot) pair and bumps
        # a sequence so the encoder wakes; the encoder throttles to stream_fps,
        # then renders the annotated view via speedcam.render_preview.
        self._enc_item = None
        self._enc_cond = threading.Condition()
        self._enc_seq = 0
        self._enc_thread = None

        # Live-stream encode budgeting. JPEG-encoding every annotated frame at
        # camera resolution is a real cost on the capture core, so we cap the
        # preview to web.stream_fps: at most one encode per interval, regardless
        # of how fast the detection loop runs (the loop itself is never
        # throttled -- this only governs how often the preview image refreshes).
        self._last_encode_ts = 0.0
        web = cfg.get("web") or {}
        stream_fps = float(web.get("stream_fps", 10) or 0)
        self._stream_min_dt = (1.0 / stream_fps) if stream_fps > 0 else 0.0
        # The live preview doesn't need full camera resolution. Downscaling the
        # annotated frame to this width before JPEG-encoding is a big win on a
        # weak Pi: encoding 1280x720 costs ~34ms, but ~640 wide costs ~8ms.
        # 0 = encode at full resolution.
        self._stream_max_width = int(web.get("stream_max_width", 640) or 0)

        # Drive-by calibration session (see driveby.py). One session at a time,
        # which is fine for single-operator home use.
        self._db_lock = threading.Lock()
        self._db = {"armed": False, "since": 0.0, "passes": []}

    # ------------------------------------------------------------- lifecycle
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Preview encoder on its own core; started here so it shares the runner's
        # lifecycle and shuts down cleanly on stop().
        self._enc_thread = threading.Thread(target=self._encode_loop, daemon=True)
        self._enc_thread.start()

    def _run(self):
        self.speedcam.run(frame_callback=self._on_frame, stop_event=self._stop)

    def stop(self):
        self._stop.set()
        with self._enc_cond:      # wake the encoder so it can exit promptly
            self._enc_cond.notify_all()

    def _on_frame(self, raw, overlay):
        now = time.monotonic()
        # Runs ON the capture/detect thread, so it must stay cheap: publish
        # references only (no copy, no draw, no encode) and wake the encoder
        # thread. The dashboard's live/stale badge and calibration snapshot read
        # _latest_raw / _last_frame_ts; `overlay` is an immutable snapshot the
        # pipeline built for this frame, so handing it off here is race-free.
        with self._cond:
            self._latest_raw = raw
            self._last_frame_ts = now
        with self._enc_cond:
            self._enc_item = (raw, overlay)
            self._enc_seq += 1
            self._enc_cond.notify()

    def _encode_loop(self):
        """Render + JPEG-encode the live preview off the capture thread.

        Waits for the newest (raw frame, overlay) pair, throttles to
        web.stream_fps, then does the copy + annotate (render_preview) + resize +
        encode here -- all of it off the capture/detect loop. Intermediate frames
        are dropped on purpose; the preview only needs the latest image.
        """
        last_seen = 0
        while not self._stop.is_set():
            with self._enc_cond:
                self._enc_cond.wait_for(
                    lambda: self._enc_seq != last_seen or self._stop.is_set(),
                    timeout=1.0)
                if self._stop.is_set():
                    break
                item = self._enc_item
                last_seen = self._enc_seq
            if item is None:
                continue
            now = time.monotonic()
            if self._stream_min_dt and (now - self._last_encode_ts) < self._stream_min_dt:
                continue  # honor stream_fps; the next frame re-triggers us
            self._last_encode_ts = now
            raw, overlay = item
            preview = self.speedcam.render_preview(raw, overlay)
            mw = self._stream_max_width
            if mw and preview.shape[1] > mw:
                scale = mw / preview.shape[1]
                preview = cv2.resize(preview, None, fx=scale, fy=scale,
                                     interpolation=cv2.INTER_LINEAR)
            ok, buf = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ok:
                continue
            with self._cond:
                self._latest_jpeg = buf.tobytes()
                self._cond.notify_all()

    # --------------------------------------------------------------- streams
    def mjpeg(self):
        boundary = b"--frame"
        while not self._stop.is_set():
            with self._cond:
                self._cond.wait(timeout=1.0)
                frame = self._latest_jpeg
            if frame is None:
                continue
            yield (boundary + b"\r\nContent-Type: image/jpeg\r\n"
                   + f"Content-Length: {len(frame)}\r\n\r\n".encode()
                   + frame + b"\r\n")

    def snapshot(self):
        with self._cond:
            raw = self._latest_raw
        if raw is None:
            return None
        ok, buf = cv2.imencode(".jpg", raw, [cv2.IMWRITE_JPEG_QUALITY, 90])
        return buf.tobytes() if ok else None

    # ------------------------------------------------------------------ data
    def status(self):
        sc = self.speedcam
        calib = sc.calibration
        backup = (sc.sync.status() if getattr(sc, "sync", None)
                  else {"enabled": False})
        retention = (sc.retention.status() if getattr(sc, "retention", None)
                     else {"enabled": False})
        # Seconds since the live view last got a frame. None = none yet; a value
        # that keeps climbing means the camera/pipeline has stalled (an empty
        # road still produces frames, so a growing age is a real freeze, not a
        # quiet street). The dashboard turns this into a live/stale badge.
        with self._cond:
            ts = self._last_frame_ts
        frame_age = None if ts is None else round(time.monotonic() - ts, 1)
        return {
            "running": sc.running,
            "camera_ok": bool(getattr(sc.camera, "opened", True)),
            "frame_age": frame_age,
            "power_controls": bool((self.cfg.get("web") or {})
                                   .get("allow_power_control", True)),
            "calibrated": calib is not None,
            "calibration_points": (len(calib.image_points) if calib else 0),
            "reprojection_error_m": (round(calib.reprojection_error(), 3)
                                     if calib else None),
            "camera_source": self.cfg["camera"]["source"],
            "fps": round(sc.current_fps, 1),
            "units": sc.units,
            "speed_limit_kmh": sc.limit_kmh,
            "total_count": sc.total_count,
            "speeder_count": sc.speeder_count,
            "last_event": sc.last_event,
            "backup": backup,
            "retention": retention,
            "recognition": bool(getattr(sc, "recognizer", None)
                                and sc.recognizer.active),
            "speedkapture_threshold": sc.speedkapture_threshold,
            "orientation": sc.orientation,
            "measure_band": sc._active_band(),
        }

    def set_speedkapture(self, value):
        return self.speedcam.set_speedkapture_threshold(value)

    def set_orientation(self, value):
        return self.speedcam.set_orientation(value)

    # -------------------------------------------------------------- event data
    def _all_rows(self):
        """All CSV rows as raw dicts (oldest first)."""
        if not self.csv_path.exists():
            return []
        with self.csv_path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    @staticmethod
    def _in_range(row, date_from, date_to):
        day = (row.get("wall_time") or "")[:10]  # YYYY-MM-DD
        if date_from and day < date_from:
            return False
        if date_to and day > date_to:
            return False
        return True

    def _shape(self, r):
        return {
            "time": r.get("wall_time"),
            "track_id": r.get("track_id"),
            "speed_kmh": _f(r.get("speed_kmh")),
            "speed_mph": _f(r.get("speed_mph")),
            "direction": r.get("direction"),
            "confidence": r.get("confidence"),
            "distance_m": _f(r.get("distance_m")),
            "vehicle_type": r.get("vehicle_type") or None,
            "make": r.get("make") or None,
            "model": r.get("model") or None,
            "year": r.get("year") or None,
            "color": r.get("color") or None,
            "captured": r.get("captured") in ("1", 1, True, "True"),
            "clip": r.get("clip") or None,
            "snapshot": r.get("snapshot") or None,
        }

    # ------------------------------------------------------------- summaries
    def summary(self):
        """Vehicle counts for today / this week / this month, with a direction
        and colour/type breakdown. Counts every logged pass (captured or not)."""
        rows = self._all_rows()
        today = date.today()
        week_start = today - timedelta(days=today.weekday())  # Monday
        month_prefix = today.strftime("%Y-%m")

        periods = {"today": [], "week": [], "month": []}
        for r in rows:
            day = (r.get("wall_time") or "")[:10]
            if not day:
                continue
            try:
                d = date.fromisoformat(day)
            except ValueError:
                continue
            if d == today:
                periods["today"].append(r)
            if d >= week_start:
                periods["week"].append(r)
            if day[:7] == month_prefix:
                periods["month"].append(r)
        return {k: self._describe(v) for k, v in periods.items()}

    @staticmethod
    def _describe(rows):
        directions, colors, types = {}, {}, {}
        for r in rows:
            _tally(directions, r.get("direction"))
            _tally(colors, r.get("color"))
            _tally(types, r.get("vehicle_type"))
        return {"count": len(rows), "directions": directions,
                "colors": colors, "types": types}

    def events(self, limit=10, date_from=None, date_to=None):
        rows = [r for r in self._all_rows() if self._in_range(r, date_from, date_to)]
        rows = rows[::-1]  # most recent first
        if limit:
            rows = rows[:limit]
        return [self._shape(r) for r in rows]

    def _top_raw(self, limit=10, date_from=None, date_to=None):
        rows = [r for r in self._all_rows() if self._in_range(r, date_from, date_to)]
        rows.sort(key=lambda r: _f(r.get("speed_kmh")) or -1, reverse=True)
        return rows[:limit]

    def top(self, limit=10, date_from=None, date_to=None):
        """The fastest recorded events (optionally within a date range)."""
        return [self._shape(r) for r in self._top_raw(limit, date_from, date_to)]

    def export_csv(self, date_from=None, date_to=None, rows=None):
        """Return CSV text for a set of rows (filtered events, or a given list)."""
        if rows is None:
            rows = [r for r in self._all_rows()
                    if self._in_range(r, date_from, date_to)][::-1]
        cols = CSV_COLUMNS
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") for c in cols])
        return buf.getvalue()

    # ------------------------------------------------------------- calibrate
    def recalibrate(self, image_points, world_points, source="web"):
        calib = Calibration(image_points, world_points, meta={"units": "meters",
                                                              "source": source})
        calib.save(self.calibration_file)
        self.speedcam.set_calibration(calib)
        return calib.reprojection_error()

    # ---------------------------------------------------------- drive-by calib
    @staticmethod
    def _kmh_to_mps(value, units):
        """A user-entered display speed (mph or km/h) -> metres/second."""
        v = float(value)
        if v <= 0:
            raise ValueError("speed must be positive")
        return v / MPH_PER_MS if units == "mph" else v / KMH_PER_MS

    def _db_summary(self):
        """Public view of the session (metadata only; no trails or thumb bytes).

        Every pass that crossed the frame while recording is listed -- yours and
        any passing traffic alike. The operator selects which are theirs
        client-side; build() uses only the selected ids."""
        passes = [{
            "id": p["id"],
            "n": p["n"],
            "direction": p["direction"],       # "→" (right) or "←" (left)
            "wall": p["wall"],
            "has_thumb": p["thumb"] is not None,
        } for p in self._db["passes"]]
        return {"armed": self._db["armed"], "passes": passes, "count": len(passes)}

    def driveby_start(self):
        self.speedcam.set_pass_thumbs(True)
        with self._db_lock:
            self._db = {"armed": True, "since": time.monotonic(), "passes": []}
            return self._db_summary()

    def driveby_stop(self):
        self.speedcam.set_pass_thumbs(False)
        with self._db_lock:
            self._db["armed"] = False
            return self._db_summary()

    def driveby_reset(self):
        with self._db_lock:
            self._db["passes"] = []
            self._db["since"] = time.monotonic()
            return self._db_summary()

    def driveby_remove(self, pass_id):
        with self._db_lock:
            self._db["passes"] = [p for p in self._db["passes"]
                                  if p["id"] != pass_id]
            return self._db_summary()

    def driveby_poll(self):
        """Adopt every pass finished since the last poll. Captures all traffic;
        the operator sorts out which are theirs later. Returns the session
        summary plus how many new passes were just added."""
        with self._db_lock:
            if not self._db["armed"]:
                return {**self._db_summary(), "new": 0}
            since = self._db["since"]
        added = 0
        for p in self.speedcam.passes_since(since):
            trail = p["trail"]
            direction = "→" if trail[-1][1] >= trail[0][1] else "←"
            entry = {"id": p["id"], "trail": trail, "thumb": p.get("thumb"),
                     "direction": direction, "n": len(trail),
                     "wall": p.get("wall", "")}
            with self._db_lock:
                if (self._db["armed"] and p["finish_t"] > self._db["since"]
                        and all(e["id"] != p["id"] for e in self._db["passes"])):
                    self._db["passes"].append(entry)
                    self._db["since"] = p["finish_t"]
                    added += 1
        with self._db_lock:
            return {**self._db_summary(), "new": added}

    def driveby_thumb(self, pass_id):
        with self._db_lock:
            for p in self._db["passes"]:
                if p["id"] == pass_id:
                    return p["thumb"]
        return None

    def driveby_build(self, speed_value, units, selected_ids, lane_width_m):
        """Calibrate from ONLY the operator-selected passes, all driven at the
        same known speed. Lane is inferred from travel direction: on a two-way
        street the two directions are the two lanes, which supplies the second
        (across-road) axis the homography needs."""
        sel = set(selected_ids or [])
        with self._db_lock:
            chosen = [p for p in self._db["passes"] if p["id"] in sel]
        if len(chosen) < 2:
            raise driveby.DriveByError(
                "Select at least 2 of your own passes to calibrate from.")
        if len({p["direction"] for p in chosen}) < 2:
            raise driveby.DriveByError(
                "All selected passes go the same way. Select passes from BOTH "
                "directions (some → and some ←) -- otherwise every point "
                "lies on one line and no homography exists.")
        speed_mps = self._kmh_to_mps(speed_value, units)
        passes = [{"trail": p["trail"], "speed_mps": speed_mps,
                   "lane": 0 if p["direction"] == "→" else 1} for p in chosen]
        img, world, info = driveby.build_correspondences(
            passes, self.speedcam.frame_wh, lane_width_m=float(lane_width_m))
        err = self.recalibrate(img, world, source="driveby")
        info["reprojection_error_m"] = round(err, 3)
        info["selected"] = len(chosen)
        return info


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _tally(counts, key):
    key = (key or "").strip()
    if key:
        counts[key] = counts.get(key, 0) + 1


# ----------------------------------------------------------------------- auth
def auth_enabled(auth_cfg) -> bool:
    """Auth is active only when a non-empty password is configured."""
    return bool(str((auth_cfg or {}).get("password") or ""))


def _install_auth(app, auth_cfg):
    """Optionally gate the whole app behind HTTP Basic Auth.

    Off by default (no password set) so the LAN dashboard works as before. When
    a password is configured (put it in config.local.yaml, not config.yaml), the
    browser prompts once and then sends credentials for every request -- pages,
    APIs, the MJPEG stream, and /captures alike -- so no front-end changes are
    needed. Credentials are compared in constant time.
    """
    if not auth_enabled(auth_cfg):
        return
    username = str(auth_cfg.get("username") or "admin")
    password = str(auth_cfg.get("password"))

    @app.before_request
    def _require_auth():
        a = request.authorization
        if (a is not None and (a.type or "").lower() == "basic"
                and hmac.compare_digest(a.username or "", username)
                and hmac.compare_digest(a.password or "", password)):
            return None
        return Response("Authentication required.", 401,
                        {"WWW-Authenticate": 'Basic realm="SpeedKam"'})


# --------------------------------------------------------------------- Flask
def create_app(runner: Runner) -> Flask:
    app = Flask(__name__)
    _install_auth(app, (runner.cfg.get("web") or {}).get("auth"))

    @app.route("/")
    def index():
        return send_file(WEBUI_DIR / "dashboard.html")

    @app.route("/calibrate")
    def calibrate_page():
        return send_file(WEBUI_DIR / "calibrate.html")

    @app.route("/stream.mjpg")
    def stream():
        return Response(runner.mjpeg(),
                        mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.route("/api/snapshot.jpg")
    def snapshot():
        data = runner.snapshot()
        if data is None:
            return ("camera warming up", 503)
        return Response(data, mimetype="image/jpeg")

    @app.route("/api/status")
    def status():
        return jsonify(runner.status())

    @app.route("/api/summary")
    def summary():
        return jsonify(runner.summary())

    @app.route("/api/speedkapture", methods=["POST"])
    def speedkapture():
        data = request.get_json(force=True, silent=True) or {}
        val = data.get("threshold", request.form.get("threshold"))
        try:
            applied = runner.set_speedkapture(float(val))
        except (TypeError, ValueError):
            return jsonify({"ok": False,
                            "error": "threshold must be a number"}), 400
        return jsonify({"ok": True, "speedkapture_threshold": applied,
                        "units": runner.speedcam.units})

    @app.route("/api/orientation", methods=["POST"])
    def orientation():
        data = request.get_json(force=True, silent=True) or {}
        val = data.get("orientation", request.form.get("orientation"))
        applied = runner.set_orientation(val)
        return jsonify({"ok": True, "orientation": applied,
                        "measure_band": runner.speedcam._active_band()})

    @app.route("/api/power", methods=["POST"])
    def power():
        # Gracefully reboot / power off the whole Pi from the dashboard. Gated by
        # config, and by a sudoers drop-in (install-service.sh) that lets the
        # non-root service user run ONLY `systemctl reboot`/`poweroff`. `sudo -n`
        # is non-interactive, so a node missing that rule fails fast with a clear
        # message instead of hanging on a password prompt.
        if not (runner.cfg.get("web") or {}).get("allow_power_control", True):
            return jsonify({"ok": False,
                            "error": "power control is disabled in config"}), 403
        data = request.get_json(force=True, silent=True) or {}
        action = data.get("action", request.form.get("action"))
        cmd = {"reboot": "reboot", "shutdown": "poweroff"}.get(action)
        if not cmd:
            return jsonify({"ok": False,
                            "error": "action must be 'reboot' or 'shutdown'"}), 400
        try:
            r = subprocess.run(["sudo", "-n", "systemctl", cmd],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "command failed").strip()
            if "password is required" in msg or "a terminal is required" in msg:
                msg = ("the node hasn't granted power permission yet -- re-run "
                       "deploy/install-service.sh on it once.")
            return jsonify({"ok": False, "error": msg}), 500
        # systemctl has enqueued the transition; the box goes down momentarily.
        return jsonify({"ok": True, "action": action})

    @app.route("/api/events")
    def events():
        limit = request.args.get("limit", default=10, type=int)
        df = request.args.get("from") or None
        dt = request.args.get("to") or None
        return jsonify(runner.events(limit=limit, date_from=df, date_to=dt))

    @app.route("/api/top")
    def top():
        limit = request.args.get("limit", default=10, type=int)
        df = request.args.get("from") or None
        dt = request.args.get("to") or None
        return jsonify(runner.top(limit=limit, date_from=df, date_to=dt))

    def _csv_response(text, filename):
        return Response(text, mimetype="text/csv",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{filename}"'})

    @app.route("/api/export.csv")
    def export_csv():
        df = request.args.get("from") or None
        dt = request.args.get("to") or None
        name = "speedkam_events"
        if df or dt:
            name += f"_{df or 'start'}_to_{dt or 'end'}"
        return _csv_response(runner.export_csv(date_from=df, date_to=dt),
                             name + ".csv")

    @app.route("/api/top.csv")
    def top_csv():
        limit = request.args.get("limit", default=10, type=int)
        df = request.args.get("from") or None
        dt = request.args.get("to") or None
        rows = runner._top_raw(limit, df, dt)
        return _csv_response(runner.export_csv(rows=rows),
                             f"speedkam_top{limit}.csv")

    @app.route("/captures/<path:name>")
    def captures(name):
        return send_from_directory(runner.captures_dir, name)

    @app.route("/api/calibrate", methods=["POST"])
    def do_calibrate():
        data = request.get_json(force=True, silent=True) or {}
        img = data.get("image_points")
        world = data.get("world_points")
        if not img or not world or len(img) != len(world) or len(img) < 4:
            return jsonify({"ok": False,
                            "error": "Need >=4 matching image/world points."}), 400
        try:
            err = runner.recalibrate(img, world)
        except Exception as exc:  # noqa: BLE001 - report any geometry failure
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "reprojection_error_m": round(err, 3),
                        "points": len(img)})

    # -------------------------------------------------------- drive-by calib
    @app.route("/api/driveby/start", methods=["POST"])
    def driveby_start():
        return jsonify({"ok": True, **runner.driveby_start()})

    @app.route("/api/driveby/stop", methods=["POST"])
    def driveby_stop():
        return jsonify({"ok": True, **runner.driveby_stop()})

    @app.route("/api/driveby/reset", methods=["POST"])
    def driveby_reset():
        return jsonify({"ok": True, **runner.driveby_reset()})

    @app.route("/api/driveby/poll", methods=["POST"])
    def driveby_poll():
        return jsonify({"ok": True, **runner.driveby_poll()})

    @app.route("/api/driveby/remove", methods=["POST"])
    def driveby_remove():
        data = request.get_json(force=True, silent=True) or {}
        try:
            pid = int(data.get("id"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "id must be an integer"}), 400
        return jsonify({"ok": True, **runner.driveby_remove(pid)})

    @app.route("/api/driveby/pass/<int:pass_id>/thumb.jpg")
    def driveby_thumb(pass_id):
        data = runner.driveby_thumb(pass_id)
        if not data:
            return ("no thumbnail", 404)
        return Response(data, mimetype="image/jpeg")

    @app.route("/api/driveby/build", methods=["POST"])
    def driveby_build():
        data = request.get_json(force=True, silent=True) or {}
        lane_width = data.get("lane_width_m", driveby.DEFAULT_LANE_WIDTH_M)
        units = data.get("units") or runner.speedcam.units
        try:
            info = runner.driveby_build(data.get("speed"), units,
                                        data.get("selected"), lane_width)
        except driveby.DriveByError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001 - report any geometry failure
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, **info})

    return app
