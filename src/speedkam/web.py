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
import threading
from pathlib import Path

import cv2
from datetime import date, timedelta

from flask import (Flask, Response, jsonify, request, send_file,
                   send_from_directory)

from .calibration import Calibration
from .pipeline import SpeedCamera
from .recorder import CSV_COLUMNS

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
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------- lifecycle
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        self.speedcam.run(frame_callback=self._on_frame, stop_event=self._stop)

    def stop(self):
        self._stop.set()

    def _on_frame(self, raw, annotated):
        ok, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return
        with self._cond:
            self._latest_jpeg = buf.tobytes()
            self._latest_raw = raw
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
        return {
            "running": sc.running,
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
    def recalibrate(self, image_points, world_points):
        calib = Calibration(image_points, world_points, meta={"units": "meters",
                                                              "source": "web"})
        calib.save(self.calibration_file)
        self.speedcam.set_calibration(calib)
        return calib.reprojection_error()


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

    return app
