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
CSV_COLUMNS = [
    "wall_time", "track_id", "speed_kmh", "speed_mph", "direction",
    "confidence", "distance_m", "vehicle_type", "make", "model", "year",
    "color", "captured", "clip", "snapshot",
]


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
        # The ring buffer is written by the capture thread (push) and read by the
        # process thread (frame_at / save_media) once the pipeline runs those on
        # separate cores, so all buffer access is guarded. The lock is held only
        # for cheap deque ops -- never during the slow video encode, which works
        # off a snapshot taken under the lock.
        self._lock = threading.Lock()
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
                snapshot_name=None, captured=False):
        """Append one event row: metadata + recognized attributes + media names."""
        attrs = attrs or {}
        row = {
            "wall_time": datetime.now().isoformat(timespec="seconds"),
            "track_id": track_id,
            "speed_kmh": f"{result.speed_kmh:.1f}",
            "speed_mph": f"{result.speed_mph:.1f}",
            "direction": result.direction,
            "confidence": result.confidence,
            "distance_m": f"{result.distance_m:.1f}",
            "vehicle_type": attrs.get("vehicle_type") or "",
            "make": attrs.get("make") or "",
            "model": attrs.get("model") or "",
            "year": attrs.get("year") or "",
            "color": attrs.get("color") or "",
            "captured": 1 if captured else 0,
            "clip": clip_name or "",
            "snapshot": snapshot_name or "",
        }
        with self.csv_path.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_COLUMNS).writerow(row)

    # ---------------------------------------------------------------- buffer
    def push(self, t, frame):
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
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(clip_path), fourcc, fps, (w, h))
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
