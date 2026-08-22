# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""False-positive handling + the richer dashboard analytics.

Covers: the new CSV schema (status/duration_s/n_samples) and its migration,
Recorder.set_status (manual reject/restore), and the Runner query surface
(summary/analytics/report/rejects) all excluding rejected rows from stats while
still surfacing them in the review bin."""
import csv
from dataclasses import dataclass
from pathlib import Path

from speedkam.recorder import CSV_COLUMNS, Recorder, row_key
from speedkam.web import Runner


@dataclass
class FakeResult:
    speed_kmh: float
    speed_mph: float
    direction: str
    distance_m: float
    duration_s: float = 1.0
    n_samples: int = 8
    confidence: str = "ok"


def _recorder(tmp_path):
    cfg = {"output_dir": str(tmp_path / "caps"), "clip_seconds": 8,
           "max_buffer_mb": 128, "save_snapshot": True, "record_fps": 0}
    return Recorder(cfg, {"csv_file": str(tmp_path / "events.csv")})


def test_new_schema_columns_present():
    for c in ("duration_s", "n_samples", "status", "review_reason"):
        assert c in CSV_COLUMNS


def test_log_row_writes_status_and_quality(tmp_path):
    rec = _recorder(tmp_path)
    rec.log_row(1, FakeResult(56.0, 34.8, "inbound", 6.1, 1.2, 9), "mph",
                {"color": "red"}, "a.mp4", "a.jpg", captured=True)
    rec.log_row(2, FakeResult(191.9, 119.3, "inbound", 218.3), "mph",
                {"color": "gray"}, None, "b.jpg", captured=False,
                status="rejected", review_reason="phantom track")
    with rec.csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["status"] == "ok"
    assert rows[0]["duration_s"] == "1.20" and rows[0]["n_samples"] == "9"
    assert rows[1]["status"] == "rejected"
    assert rows[1]["review_reason"] == "phantom track"


def test_migration_backfills_old_schema(tmp_path):
    """An events.csv from before the status columns must upgrade in place and
    keep its rows (blank status = counts as real)."""
    csv_path = tmp_path / "events.csv"
    old_cols = ["wall_time", "track_id", "speed_kmh", "speed_mph", "direction",
                "confidence", "distance_m", "vehicle_type", "make", "model",
                "year", "color", "captured", "clip", "snapshot"]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(old_cols)
        w.writerow(["2026-08-01T10:00:00", "1", "40.0", "24.9", "inbound",
                    "ok", "6.0", "", "", "", "", "red", "1", "c.mp4", "c.jpg"])
    Recorder({"output_dir": str(tmp_path / "caps"), "clip_seconds": 8,
              "max_buffer_mb": 128, "save_snapshot": True, "record_fps": 0},
             {"csv_file": str(csv_path)})
    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        assert list(rows[0].keys()) == CSV_COLUMNS
    assert rows[0]["color"] == "red" and rows[0]["status"] == ""


def test_set_status_rejects_and_restores(tmp_path):
    rec = _recorder(tmp_path)
    rec.log_row(1, FakeResult(72.9, 45.3, "outbound", 29.3), "mph",
                {"color": "green"}, "clip1.mp4", "clip1.jpg", captured=True)
    assert rec.set_status("clip1", "rejected", "no car in clip") == 1
    with rec.csv_path.open(newline="", encoding="utf-8") as f:
        r = list(csv.DictReader(f))[0]
    assert r["status"] == "rejected" and r["review_reason"] == "no car in clip"
    assert rec.set_status("clip1", "ok", "") == 1
    with rec.csv_path.open(newline="", encoding="utf-8") as f:
        assert list(csv.DictReader(f))[0]["status"] == "ok"


def test_row_key_prefers_clip_then_snapshot_then_synthetic():
    assert row_key({"clip": "x.mp4", "snapshot": "x.jpg"}) == "x"
    assert row_key({"clip": "", "snapshot": "y.jpg"}) == "y"
    assert row_key({"wall_time": "T", "track_id": "5"}) == "T|5"


# ------------------------------------------------------------- Runner queries
class FakeSpeedcam:
    units = "mph"
    limit_kmh = 40.0  # ~25 mph

    def __init__(self, recorder=None):
        self.recorder = recorder


def _runner(tmp_path, rows):
    csv_path = tmp_path / "events.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in CSV_COLUMNS})
    r = Runner.__new__(Runner)
    r.csv_path = csv_path
    r.speedcam = FakeSpeedcam(Recorder(
        {"output_dir": str(tmp_path / "caps"), "clip_seconds": 8,
         "max_buffer_mb": 128, "save_snapshot": True, "record_fps": 0},
        {"csv_file": str(csv_path)}))
    return r


def _row(day, kmh, mph, direction="inbound", color="red", status="ok", hour="10"):
    return {"wall_time": f"{day}T{hour}:00:00", "track_id": "1",
            "speed_kmh": str(kmh), "speed_mph": str(mph), "direction": direction,
            "confidence": "ok", "distance_m": "6.0", "color": color,
            "status": status, "captured": "1", "clip": f"{day}_{mph}.mp4",
            "snapshot": f"{day}_{mph}.jpg"}


def test_rejected_rows_excluded_from_stats_but_listed(tmp_path):
    rows = [
        _row("2026-08-20", 40, 24.9, "inbound", "red"),
        _row("2026-08-20", 64, 39.8, "outbound", "blue"),
        _row("2026-08-20", 191, 118.7, "inbound", "gray", status="rejected"),
    ]
    r = _runner(tmp_path, rows)
    an = r.analytics()
    assert an["count"] == 2                       # the phantom is excluded
    # avg over the two real passes only (40 + 64) / 2 = 52 km/h
    assert round(an["avg_speed_kmh"]) == 52
    rej = r.rejects()
    assert len(rej) == 1 and rej[0]["status"] == "rejected"
    # summary period stats also exclude it
    assert r.summary()["all"]["count"] == 2


def test_report_builder_filters(tmp_path):
    rows = [
        # a red inbound Thursday pass (2026-08-20 is a Thursday)
        _row("2026-08-20", 56, 34.8, "inbound", "red"),
        _row("2026-08-20", 48, 29.8, "outbound", "red"),   # wrong direction
        _row("2026-08-19", 56, 34.8, "inbound", "red"),     # Wednesday
        _row("2026-08-20", 56, 34.8, "inbound", "blue"),    # wrong color
        _row("2026-08-20", 191, 118.7, "inbound", "red", status="rejected"),
    ]
    r = _runner(tmp_path, rows)
    # "How many red inbound cars on Thursdays?" -> exactly the first row
    out = r.report({"color": "red", "direction": "inbound", "dows": [3]})
    assert out["aggregate"]["count"] == 1
    assert out["rows"][0]["speed_mph"] == 34.8
    # min speed filter
    assert r.report({"min_mph": 40})["aggregate"]["count"] == 0
    # status='all' includes the rejected row
    assert r.report({"status": "all"})["aggregate"]["count"] == 5
