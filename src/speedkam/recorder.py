# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Ring-buffer video recorder + event logging.

The pipeline pushes every captured frame into a rolling buffer that holds the
last `clip_seconds` of video. When a vehicle finishes its pass and a speed is
known, the pipeline decides -- via the SpeedKapture threshold -- whether to save
a clip. Either way it logs a CSV row so counts and attributes are recorded even
for passes we don't film.

So this module exposes two separable steps:
  * save_media()  -- dump the ring buffer to an MP4 (+ annotated JPEG snapshot).
  * log_row()     -- append one CSV row (metadata + recognized attributes).

CSV rows are the durable record: they're kept forever (retention only prunes
media), so daily/weekly/monthly vehicle counts survive after clips are deleted.
"""
from __future__ import annotations

import csv
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2

from . import annotate

# Column order for the event log. Attribute columns sit before clip/snapshot;
# `captured` is 1 when a clip was actually saved (above the SpeedKapture
# threshold), 0 for a counted-but-not-filmed pass.
#
# `status` is the false-positive verdict: "ok" (a real, counted pass) or
# "rejected" (auto-flagged junk -- a cyclist, pedestrian, or phantom track --
# excluded from every stat). `review_reason` says why. Older logs predate these
# columns; the migration back-fills blanks, and readers treat an empty status as
# "ok" so historical rows still count. `duration_s`/`n_samples` are the raw
# track quality signals (seconds in frame, samples fitted) -- extra fidelity that
# also explains a rejection.
CSV_COLUMNS = [
    "wall_time", "track_id", "speed_kmh", "speed_mph", "direction",
    "confidence", "distance_m", "duration_s", "n_samples",
    "vehicle_type", "make", "model", "year", "color",
    "status", "review_reason", "captured", "clip", "snapshot",
]

# Attribute columns a deferred recognition worker may fill in later.
ATTR_COLUMNS = ["vehicle_type", "make", "model", "year", "color"]


def row_key(row):
    """The stable identifier for an event row, matching set_status()'s ``key``.

    Prefers the clip stem, then the snapshot stem (so snapshot-only passes are
    addressable too), falling back to ``"<wall_time>|<track_id>"`` for a row with
    no media at all."""
    from pathlib import PurePosixPath
    clip = (row.get("clip") or "").strip()
    snap = (row.get("snapshot") or "").strip()
    if clip:
        return PurePosixPath(clip).stem
    if snap:
        return PurePosixPath(snap).stem
    return f"{row.get('wall_time', '')}|{row.get('track_id', '')}"


def _row_key_matches(row, key):
    return row_key(row) == key


class Recorder:
    def __init__(self, cfg, log_cfg, fps_hint=30):
        self.cfg = cfg
        self.output_dir = Path(cfg["output_dir"])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.csv_path = Path(log_cfg["csv_file"])
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.fps_hint = fps_hint
        # Rolling buffer of (t_monotonic, frame) covering ~clip_seconds. It is
        # evicted by TIME (below), not a fixed frame count, so its RAM scales
        # with the ACTUAL frame rate instead of an optimistic fps guess. The old
        # count-based sizing (clip_seconds * 30fps * 1.5 = 360 frames) held ~1GB
        # of 720p frames whatever the real rate -- fatal on a 1GB Pi, which then
        # swap-thrashes. Keep ~1.5x the clip window of wall-time...
        self._window_s = float(cfg["clip_seconds"]) * 1.5
        # ...and a hard RAM ceiling as a backstop against a fast camera or a bad
        # clock (set from the real frame size on first push).
        self._max_buffer_bytes = float(cfg.get("max_buffer_mb", 128) or 128) * 1e6
        self._hard_cap = None
        self.buffer = deque()
        # Cap how often frames are STORED for clips, independent of the detection
        # rate. The parallel pipeline's capture thread may deliver frames at the
        # full sensor rate (~30fps), but a 1GB Pi can't hold many seconds of
        # 720p at that rate within max_buffer_mb -- so the pre-roll would shrink
        # to a fraction of clip_seconds. Recording at a lower rate (default 15fps,
        # smooth enough to watch) keeps the pre-roll long AND cuts per-frame copy
        # cost/heat. 0 = store every frame. Throttled in frame-time, so it holds
        # for both live (monotonic) and offline (file-clock) sources.
        record_fps = float(cfg.get("record_fps", 15) or 0)
        self._record_min_dt = (1.0 / record_fps) if record_fps > 0 else 0.0
        self._last_push_t = None
        # The ring buffer is written by the capture thread (push) and read by the
        # process thread (frame_at / save_media) once the pipeline runs those on
        # separate cores, so all buffer access is guarded. The lock is held only
        # for cheap deque ops -- never during the slow video encode, which works
        # off a snapshot taken under the lock.
        self._lock = threading.Lock()
        # Separate lock for the event CSV: the pipeline thread appends rows
        # (log_row) while the Flask thread may rewrite the file to flip a row's
        # status (set_status). Without this, a reject landing mid-append could
        # drop a just-logged pass. Held only for the quick file ops.
        self._csv_lock = threading.Lock()
        self._migrate_or_init_csv()

    # ------------------------------------------------------------------- CSV
    def _migrate_or_init_csv(self):
        """Create the CSV with the current header, or upgrade an older one.

        Earlier versions had fewer columns. If we find an old header we rewrite
        the file in place with the new columns, back-filling blanks -- so a
        long-running deployment keeps its history when you update SpeedKam.
        """
        if not self.csv_path.exists():
            with self.csv_path.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(CSV_COLUMNS)
            return
        try:
            with self.csv_path.open(newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, [])
                if header == CSV_COLUMNS:
                    return  # already current
                rows = list(csv.DictReader(f, fieldnames=header))
        except OSError:
            return
        tmp = self.csv_path.with_suffix(".csv.tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in CSV_COLUMNS})
        tmp.replace(self.csv_path)
        print(f"[SpeedKam] Upgraded {self.csv_path.name} to the new event "
              f"schema (kept {len(rows)} existing rows).")

    def log_row(self, track_id, result, units, attrs=None, clip_name=None,
                snapshot_name=None, captured=False, status="ok",
                review_reason=""):
        """Append one event row: metadata + recognized attributes + media names.

        ``status`` is the false-positive verdict ("ok" or "rejected") and
        ``review_reason`` the human explanation for a rejection; both feed the
        dashboards' auto-reject bin and keep junk out of the stats.
        """
        attrs = attrs or {}
        row = {
            "wall_time": datetime.now().isoformat(timespec="seconds"),
            "track_id": track_id,
            "speed_kmh": f"{result.speed_kmh:.1f}",
            "speed_mph": f"{result.speed_mph:.1f}",
            "direction": result.direction,
            "confidence": result.confidence,
            "distance_m": f"{result.distance_m:.1f}",
            "duration_s": f"{result.duration_s:.2f}",
            "n_samples": result.n_samples,
            "vehicle_type": attrs.get("vehicle_type") or "",
            "make": attrs.get("make") or "",
            "model": attrs.get("model") or "",
            "year": attrs.get("year") or "",
            "color": attrs.get("color") or "",
            "status": status or "ok",
            "review_reason": review_reason or "",
            "captured": 1 if captured else 0,
            "clip": clip_name or "",
            "snapshot": snapshot_name or "",
        }
        with self._csv_lock:
            with self.csv_path.open("a", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=CSV_COLUMNS).writerow(row)

    def set_status(self, key, status, review_reason=""):
        """Rewrite the ``status``/``review_reason`` of a logged row in place.

        Powers the dashboards' manual "not a real car" (reject) and "restore"
        actions. ``key`` matches a row by the stem of its clip or snapshot
        filename, or by ``"<wall_time>|<track_id>"`` for media-less rows. Returns
        the number of rows updated (0 if none matched). The whole file is rewritten
        under no lock -- fine for the single-writer pipeline + occasional UI edit.
        """
        if not self.csv_path.exists():
            return 0
        with self._csv_lock:
            with self.csv_path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            updated = 0
            for r in rows:
                if _row_key_matches(r, key):
                    r["status"] = status
                    r["review_reason"] = review_reason or ""
                    updated += 1
            if updated:
                tmp = self.csv_path.with_suffix(".csv.tmp")
                with tmp.open("w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=CSV_COLUMNS,
                                       extrasaction="ignore")
                    w.writeheader()
                    for r in rows:
                        w.writerow({c: r.get(c, "") for c in CSV_COLUMNS})
                tmp.replace(self.csv_path)
        return updated

    # ---------------------------------------------------------------- buffer
    def push(self, t, frame):
        # Record-rate throttle: drop frames that arrive sooner than record_fps
        # allows. A negative delta (clock reset -- e.g. a looped demo file) is
        # treated as a fresh start, not a skip.
        if self._record_min_dt and self._last_push_t is not None:
            dt = t - self._last_push_t
            if 0.0 <= dt < self._record_min_dt:
                return
        self._last_push_t = t
        # Copy outside the lock (the copy is the expensive part); only the deque
        # mutation needs guarding.
        copy = frame.copy()
        with self._lock:
            if self._hard_cap is None:
                frame_bytes = max(1, int(getattr(frame, "nbytes", 0) or frame.size))
                self._hard_cap = max(2, int(self._max_buffer_bytes / frame_bytes))
            self.buffer.append((t, copy))
            # Evict by wall-time so RAM tracks the real frame rate, then enforce
            # the hard frame ceiling as a safety net (guards against a
            # non-monotonic clock -- e.g. a looped demo file -- that would defeat
            # the time window).
            horizon = t - self._window_s
            while len(self.buffer) > 1 and self.buffer[0][0] < horizon:
                self.buffer.popleft()
            while len(self.buffer) > self._hard_cap:
                self.buffer.popleft()

    def frame_at(self, t):
        """The buffered frame whose timestamp is closest to `t`, or None."""
        with self._lock:
            if not self.buffer:
                return None
            best = min(self.buffer, key=lambda tf: abs(tf[0] - t))
            return best[1]

    def _snapshot(self):
        """A stable list copy of the ring buffer for slow media work off-lock."""
        with self._lock:
            return list(self.buffer)

    def _measured_fps(self, frames):
        if len(frames) >= 2:
            span = frames[-1][0] - frames[0][0]
            if span > 0:
                fps = (len(frames) - 1) / span
                # Guard against bad timestamps (e.g. a looped demo file whose
                # clock resets mid-buffer) producing an fps the encoder rejects.
                # The MP4 (mpeg4) timebase denominator caps near 65535 and
                # OpenCV scales fps by 1000, so keep fps well under ~65.
                if 1.0 <= fps <= 60.0:
                    return fps
        return float(self.fps_hint)

    # ----------------------------------------------------------------- media
    def save_snapshot_only(self, track_id, result, units, limit_kmh):
        """Write just the annotated JPEG snapshot (no clip) for one pass.

        Used for counted passes BELOW the SpeedKapture threshold when
        recording.always_snapshot is on, so a deferred recognition worker has
        an image to enrich later. Returns the snapshot Path, or None.
        """
        frames = self._snapshot()
        if not frames:
            return None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        speed_tag = (f"{result.speed_mph:.0f}mph" if units == "mph"
                     else f"{result.speed_kmh:.0f}kmh")
        base = f"{stamp}_id{track_id}_{speed_tag}"
        _, fr = frames[len(frames) // 2]
        snap = fr.copy()
        annotate.draw_speed_banner(snap, result, limit_kmh, units)
        snapshot_path = self.output_dir / f"{base}.jpg"
        cv2.imwrite(str(snapshot_path), snap)
        return snapshot_path

    def save_media(self, track_id, result, units, limit_kmh, burn_overlay):
        """Write clip (+ optional snapshot) for one finished vehicle.

        Returns (clip_path, snapshot_path); either may be None. Does NOT touch
        the CSV -- call log_row() for that.
        """
        frames = self._snapshot()
        if not frames:
            return None, None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        speed_tag = (f"{result.speed_mph:.0f}mph" if units == "mph"
                     else f"{result.speed_kmh:.0f}kmh")
        base = f"{stamp}_id{track_id}_{speed_tag}"
        fps = self._measured_fps(frames)

        h, w = frames[0][1].shape[:2]
        clip_path = self.output_dir / f"{base}.mp4"
        # H.264 (avc1), NOT mp4v: mp4v is MPEG-4 Part 2, which desktop players
        # (VLC) handle but HTML5 <video> refuses ("No video with supported
        # format and MIME type found"). avc1 is what the dashboard's inline
        # player needs. OpenCV's FFmpeg backend on the node writes real avc1.
        # Fall back to mp4v if this build can't open an avc1 writer, so a clip
        # is still recorded (playable in VLC) rather than silently empty.
        writer = cv2.VideoWriter(
            str(clip_path), cv2.VideoWriter_fourcc(*"avc1"), fps, (w, h))
        if not writer.isOpened():
            writer = cv2.VideoWriter(
                str(clip_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        snapshot_path = None
        mid = len(frames) // 2
        for i, (_, fr) in enumerate(frames):
            out = fr.copy()
            if burn_overlay:
                annotate.draw_speed_banner(out, result, limit_kmh, units)
            writer.write(out)
            if self.cfg["save_snapshot"] and i == mid:
                snap = fr.copy()
                annotate.draw_speed_banner(snap, result, limit_kmh, units)
                snapshot_path = self.output_dir / f"{base}.jpg"
                cv2.imwrite(str(snapshot_path), snap)
        writer.release()
        return clip_path, snapshot_path
