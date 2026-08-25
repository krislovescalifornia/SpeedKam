# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Web dashboard: live view, recent clips, and stats.

A background thread runs the SpeedCamera pipeline and publishes each annotated
frame here; Flask serves an MJPEG live view, a REST API, and the capture files.
This is what makes the headless Pi usable from a phone/laptop -- browse to
http://<pi-ip>:8080.
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
from datetime import date, datetime as _dt, timedelta

from flask import (Flask, Response, jsonify, request, send_file,
                   send_from_directory)

from .pipeline import SpeedCamera
from .recorder import CSV_COLUMNS, row_key as recorder_row_key

KMH_PER_MS = 3.6
MPH_PER_MS = 2.2369362920544
KMH_PER_MPH = 1.609344

WEBUI_DIR = Path(__file__).parent / "webui"


class Runner:
    """Owns the pipeline thread and the latest frames for the web layer."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.speedcam = SpeedCamera(cfg)
        self.captures_dir = Path(cfg["recording"]["output_dir"]).resolve()
        self.csv_path = Path(cfg["logging"]["csv_file"]).resolve()

        self._latest_jpeg = None       # annotated, for the live stream
        self._latest_raw = None        # clean frame, for the latest-snapshot tile
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
            "camera_source": self.cfg["camera"]["source"],
            "fps": round(sc.current_fps, 1),
            "units": sc.units,
            "speed_limit_kmh": sc.limit_kmh,
            "paused_low_light": bool(getattr(sc, "paused_low_light", False)),
            "scene_brightness": (round(sc.scene_brightness, 1)
                                 if getattr(sc, "scene_brightness", None)
                                 is not None else None),
            "total_count": sc.total_count,
            "speeder_count": sc.speeder_count,
            "last_event": sc.last_event,
            "backup": backup,
            "retention": retention,
            "recognition": bool(getattr(sc, "recognizer", None)
                                and sc.recognizer.active),
            "speedkapture_threshold": sc.speedkapture_threshold,
            "min_vehicle_aspect": sc.min_vehicle_aspect,
            "min_car_width_px": sc.min_car_width_px,
            "max_area_cv": sc.max_area_cv,
            "dedupe_seconds": sc.dedupe_seconds,
            # Crossing-time calibration (read-only here; set in config.local.yaml).
            # The two image columns a car is timed between, and the per-direction
            # distances that turn a crossing time into a speed. A null distance =
            # that direction isn't calibrated yet (node logs its crossing time).
            "x_a": self.cfg["speed"].get("x_a"),
            "x_b": self.cfg["speed"].get("x_b"),
            "d_east_m": self.cfg["speed"].get("d_east_m"),
            "d_west_m": self.cfg["speed"].get("d_west_m"),
            "rejected_count": sum(1 for r in self._all_rows()
                                  if self._is_rejected(r)),
        }

    def set_reject_thresholds(self, min_aspect=None, min_car_width_px=None,
                              max_area_cv=None, dedupe_seconds=None):
        return self.speedcam.set_reject_thresholds(
            min_aspect=min_aspect, min_car_width_px=min_car_width_px,
            max_area_cv=max_area_cv, dedupe_seconds=dedupe_seconds)

    def set_speedkapture(self, value):
        return self.speedcam.set_speedkapture_threshold(value)

    def set_speed_limit(self, value, units=None):
        """Set 'My Road Speed Limit' from a value typed in display units.

        The UI edits in the node's display units (mph or km/h); we convert to
        km/h -- the internal unit the pipeline compares speeds against -- before
        storing. A non-positive value is rejected so the limit stays meaningful.
        """
        units = (units or self.speedcam.units)
        v = float(value)
        if v <= 0:
            raise ValueError("speed limit must be positive")
        kmh = v * KMH_PER_MPH if units == "mph" else v
        return self.speedcam.set_speed_limit_kmh(kmh)

    # -------------------------------------------------------------- event data
    def _all_rows(self):
        """All CSV rows as raw dicts (oldest first)."""
        if not self.csv_path.exists():
            return []
        with self.csv_path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    @staticmethod
    def _is_rejected(r):
        """A row is a false positive only when explicitly marked 'rejected'.
        Legacy rows (blank status) predate the gate and count as real."""
        return (r.get("status") or "ok").strip().lower() == "rejected"

    def _visible_rows(self):
        """Every real (non-rejected) pass -- what all stats are computed over."""
        return [r for r in self._all_rows() if not self._is_rejected(r)]

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
            "duration_s": _f(r.get("duration_s")),
            "n_samples": _f(r.get("n_samples")),
            "vehicle_type": r.get("vehicle_type") or None,
            "make": r.get("make") or None,
            "model": r.get("model") or None,
            "year": r.get("year") or None,
            "color": r.get("color") or None,
            "status": (r.get("status") or "ok").strip().lower() or "ok",
            "review_reason": r.get("review_reason") or None,
            "captured": r.get("captured") in ("1", 1, True, "True"),
            "clip": r.get("clip") or None,
            "snapshot": r.get("snapshot") or None,
            "key": recorder_row_key(r),
        }

    # ------------------------------------------------------------- summaries
    def summary(self):
        """Rich per-period stats for today / this week / this month / all time.

        Each period carries the vehicle count, over-limit count, average speed
        (overall and per travel direction), and colour/type/direction tallies.
        Rejected false positives are excluded. Counts every real logged pass
        (captured or not)."""
        rows = self._visible_rows()
        today = date.today()
        week_start = today - timedelta(days=today.weekday())  # Monday
        month_prefix = today.strftime("%Y-%m")

        periods = {"today": [], "week": [], "month": [], "all": rows}
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

    def _describe(self, rows):
        directions, colors, types = {}, {}, {}
        speeds, over = [], 0
        dir_speeds = {}
        limit = self.speedcam.limit_kmh
        for r in rows:
            _tally(directions, r.get("direction"))
            _tally(colors, r.get("color"))
            _tally(types, r.get("vehicle_type"))
            v = _f(r.get("speed_kmh"))
            if v is not None:
                speeds.append(v)
                if limit and v > limit:
                    over += 1
                d = (r.get("direction") or "").strip()
                if d:
                    dir_speeds.setdefault(d, []).append(v)
        avg = (sum(speeds) / len(speeds)) if speeds else None
        by_dir = {k: {"avg_kmh": sum(vs) / len(vs),
                      "avg_mph": (sum(vs) / len(vs)) / KMH_PER_MPH,
                      "count": len(vs)}
                  for k, vs in dir_speeds.items()}
        return {"count": len(rows), "over": over,
                "directions": directions, "colors": colors, "types": types,
                "avg_speed_kmh": avg,
                "avg_speed_mph": (avg / KMH_PER_MPH) if avg is not None else None,
                "avg_by_direction": by_dir}

    # ------------------------------------------------------------- analytics
    def analytics(self, date_from=None, date_to=None):
        """Histogram-style breakdowns for the visual dashboard, over real passes
        in an optional date range: hour-of-day, day-of-week, speed distribution,
        colour and vehicle-type shares. Speeds are returned in both units so the
        client renders in whichever the node uses."""
        rows = [r for r in self._visible_rows()
                if self._in_range(r, date_from, date_to)]
        hourly = [{"hour": h, "count": 0, "sum_kmh": 0.0} for h in range(24)]
        dow = [{"dow": i, "count": 0, "sum_kmh": 0.0} for i in range(7)]
        colors, types = {}, {}
        # Speed buckets in mph (5-mph bins up to 60+), the operator's usual unit.
        edges = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 999]
        hist = [{"lo": edges[i], "hi": edges[i + 1], "count": 0}
                for i in range(len(edges) - 1)]
        n = 0
        speeds_kmh = []
        for r in rows:
            _tally(colors, r.get("color"))
            _tally(types, r.get("vehicle_type"))
            ts = r.get("wall_time") or ""
            v = _f(r.get("speed_kmh"))
            try:
                dt = _dt.fromisoformat(ts)
            except (ValueError, TypeError):
                dt = None
            if v is not None:
                speeds_kmh.append(v)
            if dt is not None:
                if v is not None:
                    hourly[dt.hour]["count"] += 1
                    hourly[dt.hour]["sum_kmh"] += v
                    dow[dt.weekday()]["count"] += 1
                    dow[dt.weekday()]["sum_kmh"] += v
                    n += 1
            if v is not None:
                mph = v / KMH_PER_MPH
                for b in hist:
                    if b["lo"] <= mph < b["hi"]:
                        b["count"] += 1
                        break

        def _avg(bucket):
            return (bucket["sum_kmh"] / bucket["count"]) if bucket["count"] else None
        for b in hourly + dow:
            a = _avg(b)
            b["avg_kmh"] = a
            b["avg_mph"] = (a / KMH_PER_MPH) if a is not None else None
            del b["sum_kmh"]
        avg_all = (sum(speeds_kmh) / len(speeds_kmh)) if speeds_kmh else None
        return {
            "units": self.speedcam.units,
            "count": len(rows),
            "with_speed": len(speeds_kmh),
            "avg_speed_kmh": avg_all,
            "avg_speed_mph": (avg_all / KMH_PER_MPH) if avg_all is not None else None,
            "hourly": hourly, "dow": dow, "speed_hist": hist,
            "colors": colors, "types": types,
        }

    # --------------------------------------------------------- report builder
    def report(self, filters):
        """Ad-hoc filtered query for the report builder. `filters` may set:
        from/to (YYYY-MM-DD), color, direction, vehicle_type, min_mph, max_mph,
        dows (list of 0=Mon..6=Sun), hour_from/hour_to (0-23), captured (bool),
        status ('ok'|'rejected'|'all'). Returns matching rows (shaped, newest
        first) plus an aggregate the UI can headline."""
        f = filters or {}
        want_status = (f.get("status") or "ok").lower()
        color = (f.get("color") or "").strip().lower() or None
        direction = (f.get("direction") or "").strip().lower() or None
        vtype = (f.get("vehicle_type") or "").strip().lower() or None
        min_mph = _f(f.get("min_mph"))
        max_mph = _f(f.get("max_mph"))
        dows = f.get("dows")
        dows = set(int(d) for d in dows) if dows else None
        hour_from = f.get("hour_from")
        hour_to = f.get("hour_to")
        hour_from = int(hour_from) if hour_from not in (None, "") else None
        hour_to = int(hour_to) if hour_to not in (None, "") else None
        cap = f.get("captured")

        out = []
        for r in self._all_rows():
            if want_status != "all":
                if want_status == "rejected" and not self._is_rejected(r):
                    continue
                if want_status == "ok" and self._is_rejected(r):
                    continue
            if not self._in_range(r, f.get("from"), f.get("to")):
                continue
            if color and (r.get("color") or "").strip().lower() != color:
                continue
            if direction and (r.get("direction") or "").strip().lower() != direction:
                continue
            if vtype and (r.get("vehicle_type") or "").strip().lower() != vtype:
                continue
            v = _f(r.get("speed_mph"))
            if min_mph is not None and (v is None or v < min_mph):
                continue
            if max_mph is not None and (v is None or v > max_mph):
                continue
            if cap is not None:
                is_cap = r.get("captured") in ("1", 1, True, "True")
                if bool(cap) != is_cap:
                    continue
            if dows is not None or hour_from is not None or hour_to is not None:
                try:
                    dt = _dt.fromisoformat(r.get("wall_time") or "")
                except (ValueError, TypeError):
                    continue
                if dows is not None and dt.weekday() not in dows:
                    continue
                if hour_from is not None and dt.hour < hour_from:
                    continue
                if hour_to is not None and dt.hour > hour_to:
                    continue
            out.append(r)

        speeds = [_f(r.get("speed_kmh")) for r in out]
        speeds = [s for s in speeds if s is not None]
        limit = self.speedcam.limit_kmh
        colors, dirs = {}, {}
        for r in out:
            _tally(colors, r.get("color"))
            _tally(dirs, r.get("direction"))
        agg = {
            "count": len(out),
            "avg_speed_kmh": (sum(speeds) / len(speeds)) if speeds else None,
            "avg_speed_mph": ((sum(speeds) / len(speeds)) / KMH_PER_MPH)
                             if speeds else None,
            "max_speed_kmh": max(speeds) if speeds else None,
            "max_speed_mph": (max(speeds) / KMH_PER_MPH) if speeds else None,
            "over": sum(1 for s in speeds if limit and s > limit),
            "colors": colors, "directions": dirs,
        }
        shaped = [self._shape(r) for r in out[::-1]]
        limit_rows = int(f.get("limit") or 500)
        return {"aggregate": agg, "rows": shaped[:limit_rows],
                "truncated": len(shaped) > limit_rows, "total": len(shaped)}

    # ------------------------------------------------------------- rejects
    def rejects(self, limit=100):
        """The auto-rejected (false-positive) readings, newest first, for the
        dashboard's review bin."""
        rows = [r for r in self._all_rows() if self._is_rejected(r)][::-1]
        return [self._shape(r) for r in rows[:limit]]

    def set_row_status(self, key, status, reason=""):
        """Manually reject ('not a real car') or restore a logged reading."""
        status = "rejected" if str(status).lower() == "rejected" else "ok"
        n = self.speedcam.recorder.set_status(key, status, reason) \
            if getattr(self.speedcam, "recorder", None) else 0
        return {"updated": n, "status": status}

    def events(self, limit=10, date_from=None, date_to=None):
        # Real passes only; rejected false positives never show in the clip feed.
        rows = [r for r in self._visible_rows()
                if self._in_range(r, date_from, date_to)]
        rows = rows[::-1]  # most recent first
        if limit:
            rows = rows[:limit]
        return [self._shape(r) for r in rows]

    def _top_raw(self, limit=10, date_from=None, date_to=None):
        rows = [r for r in self._visible_rows()
                if self._in_range(r, date_from, date_to)]
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

    @app.route("/api/analytics")
    def analytics():
        df = request.args.get("from") or None
        dt = request.args.get("to") or None
        return jsonify(runner.analytics(date_from=df, date_to=dt))

    @app.route("/api/report", methods=["GET", "POST"])
    def report():
        if request.method == "POST":
            filters = request.get_json(force=True, silent=True) or {}
        else:
            a = request.args
            dows = a.get("dows")
            filters = {
                "from": a.get("from"), "to": a.get("to"),
                "color": a.get("color"), "direction": a.get("direction"),
                "vehicle_type": a.get("vehicle_type"),
                "min_mph": a.get("min_mph"), "max_mph": a.get("max_mph"),
                "hour_from": a.get("hour_from"), "hour_to": a.get("hour_to"),
                "status": a.get("status"), "limit": a.get("limit"),
                "dows": [int(x) for x in dows.split(",") if x != ""]
                        if dows else None,
            }
        return jsonify(runner.report(filters))

    @app.route("/api/rejects")
    def rejects():
        limit = request.args.get("limit", default=100, type=int)
        return jsonify(runner.rejects(limit=limit))

    @app.route("/api/reject", methods=["POST"])
    def reject():
        data = request.get_json(force=True, silent=True) or {}
        key = data.get("key")
        if not key:
            return jsonify({"ok": False, "error": "missing key"}), 400
        res = runner.set_row_status(key, "rejected",
                                    data.get("reason") or "marked not a vehicle")
        return jsonify({"ok": res["updated"] > 0, **res})

    @app.route("/api/restore", methods=["POST"])
    def restore():
        data = request.get_json(force=True, silent=True) or {}
        key = data.get("key")
        if not key:
            return jsonify({"ok": False, "error": "missing key"}), 400
        res = runner.set_row_status(key, "ok", "")
        return jsonify({"ok": res["updated"] > 0, **res})

    @app.route("/api/rejectconfig", methods=["POST"])
    def rejectconfig():
        data = request.get_json(force=True, silent=True) or {}
        try:
            applied = runner.set_reject_thresholds(
                min_aspect=data.get("min_vehicle_aspect"),
                min_car_width_px=data.get("min_car_width_px"),
                max_area_cv=data.get("max_area_cv"),
                dedupe_seconds=data.get("dedupe_seconds"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "values must be numbers"}), 400
        return jsonify({"ok": True, **applied})

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

    @app.route("/api/speedlimit", methods=["POST"])
    def speedlimit():
        # "My Road Speed Limit": the value is in the node's display units unless
        # the caller says otherwise. Returns the stored km/h so both dashboards
        # can re-render the limit consistently.
        data = request.get_json(force=True, silent=True) or {}
        val = data.get("limit", request.form.get("limit"))
        units = data.get("units") or request.form.get("units")
        try:
            applied = runner.set_speed_limit(val, units)
        except (TypeError, ValueError):
            return jsonify({"ok": False,
                            "error": "limit must be a positive number"}), 400
        return jsonify({"ok": True, "speed_limit_kmh": applied,
                        "units": runner.speedcam.units})

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

    return app
