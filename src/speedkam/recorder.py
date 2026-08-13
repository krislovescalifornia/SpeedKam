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
        # Buffer of (t_monotonic, frame) covering ~clip_seconds.
        maxlen = int(cfg["clip_seconds"] * max(fps_hint, 1) * 1.5)
        self.buffer = deque(maxlen=maxlen)
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
        self.buffer.append((t, frame.copy()))

    def frame_at(self, t):
        """The buffered frame whose timestamp is closest to `t`, or None."""
        if not self.buffer:
            return None
        best = min(self.buffer, key=lambda tf: abs(tf[0] - t))
        return best[1]

    def _measured_fps(self):
        if len(self.buffer) >= 2:
            span = self.buffer[-1][0] - self.buffer[0][0]
            if span > 0:
                fps = (len(self.buffer) - 1) / span
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
        if not self.buffer:
            return None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        speed_tag = (f"{result.speed_mph:.0f}mph" if units == "mph"
                     else f"{result.speed_kmh:.0f}kmh")
        base = f"{stamp}_id{track_id}_{speed_tag}"
        frames = list(self.buffer)
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
        if not self.buffer:
            return None, None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        speed_tag = (f"{result.speed_mph:.0f}mph" if units == "mph"
                     else f"{result.speed_kmh:.0f}kmh")
        base = f"{stamp}_id{track_id}_{speed_tag}"
        fps = self._measured_fps()

        frames = list(self.buffer)
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
