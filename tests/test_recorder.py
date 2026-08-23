# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The pre-roll clip buffer must bound its RAM: evict by wall-time so its size
tracks the ACTUAL frame rate, with a hard frame ceiling as a backstop. The old
count-based sizing held ~1GB of 720p frames regardless of the real rate, which
swap-thrashed a 1GB Pi."""
import numpy as np

from speedkam.recorder import Recorder
from speedkam.speed import SpeedResult


def _result(mph=30.0):
    return SpeedResult(speed_kmh=mph * 1.60934, speed_mph=mph,
                       direction="Eastbound", distance_m=6.0, duration_s=0.4,
                       n_samples=12, confidence="ok", peak_index=6)


def _rec(tmp_path, clip_seconds=8, max_buffer_mb=128, record_fps=0):
    # record_fps defaults to 0 (store every frame) so the eviction/hard-cap tests
    # below exercise the buffer directly; the record-rate cap has its own test.
    cfg = {
        "output_dir": str(tmp_path / "caps"),
        "clip_seconds": clip_seconds,
        "max_buffer_mb": max_buffer_mb,
        "save_snapshot": True,
        "record_fps": record_fps,
    }
    log = {"csv_file": str(tmp_path / "events.csv")}
    return Recorder(cfg, log, fps_hint=30)


def test_buffer_evicts_by_wall_time(tmp_path):
    rec = _rec(tmp_path, clip_seconds=8)           # window = 12s of wall-time
    f = np.zeros((8, 8, 3), np.uint8)
    for i in range(30):                            # 30 frames at 1 fps => 30s
        rec.push(float(i), f)
    times = [t for t, _ in rec.buffer]
    assert times[-1] == 29.0                       # newest kept
    assert times[0] >= 29.0 - 12.0                 # nothing older than the window
    assert len(rec.buffer) <= 14                   # ~12 frames, not 360


def test_buffer_hard_cap_bounds_ram(tmp_path):
    # A tiny RAM ceiling must cap the frame count even when every frame is well
    # inside the time window (guards a fast camera / non-monotonic clock).
    rec = _rec(tmp_path, clip_seconds=1000, max_buffer_mb=0.001)  # ~1000 bytes
    f = np.zeros((8, 8, 3), np.uint8)              # 192 bytes/frame => ~5 cap
    for i in range(50):
        rec.push(i * 0.001, f)                     # all within the huge window
    assert 2 <= len(rec.buffer) <= 6


def test_record_fps_caps_stored_rate(tmp_path):
    # Frames arriving faster than record_fps must be dropped from the store, so a
    # 1GB Pi keeps a long pre-roll instead of filling RAM at the sensor rate.
    # 50fps in, 15fps cap => keep every 4th frame (0.08s >= 1/15s; 0.06s < it).
    rec = _rec(tmp_path, clip_seconds=1000, max_buffer_mb=1024, record_fps=15)
    f = np.zeros((8, 8, 3), np.uint8)
    for i in range(200):                           # 200 frames at 50fps => 4s
        rec.push(i / 50.0, f)
    stored = len(rec.buffer)
    assert stored < 200                            # clearly throttled from 50fps
    assert 48 <= stored <= 52                      # ~every 4th (15fps over 4s)
    # Stored timestamps are spaced at least ~1/15s apart.
    times = [t for t, _ in rec.buffer]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert min(gaps) >= 1.0 / 15 - 1e-6


# --------------------------------------------------- media framing (empty-clip fix)
def test_center_index_picks_nearest_to_vehicle_time():
    frames = [(float(i), None) for i in range(11)]   # t = 0..10, middle = idx 5
    # With the vehicle at t=8, the snapshot must frame on idx 8, NOT the buffer
    # middle (idx 5) -- that middle-of-buffer choice is what saved empty road.
    assert Recorder._center_index(frames, 8.0) == 8
    assert Recorder._center_index(frames, None) == 5   # fallback = middle


def test_grab_window_selects_around_center_and_survives_rotation(tmp_path):
    rec = _rec(tmp_path, clip_seconds=1000, max_buffer_mb=1024)
    for i in range(20):
        rec.push(float(i), np.full((8, 8, 3), i, np.uint8))
    win = rec.grab_window(center_t=10.0, half_seconds=2.0)   # t in [8, 12]
    ts = [t for t, _ in win]
    assert ts == [8.0, 9.0, 10.0, 11.0, 12.0]
    # The grabbed frames are held by reference, so evicting the buffer entirely
    # can't blank them -- this is what lets a deferred encode still show the car.
    for i in range(20, 60):
        rec.push(float(i), np.zeros((8, 8, 3), np.uint8))
    assert [t for t, _ in win] == [8.0, 9.0, 10.0, 11.0, 12.0]
    assert int(win[2][1].mean()) == 10               # frame content intact


def test_snapshot_frames_on_the_car_not_empty_road(tmp_path):
    # The regression: a shallow buffer of mostly-empty frames with the car
    # present only near center_t. The saved JPEG must be the CAR frame (bright),
    # not the buffer-middle empty-road frame the old code always picked.
    rec = _rec(tmp_path)
    empty = np.zeros((64, 64, 3), np.uint8)
    car = np.zeros((64, 64, 3), np.uint8)
    car[:20, :, :] = 200                             # bright marker up top (clear of the banner)
    frames = [(float(i), empty) for i in range(11)]
    frames[8] = (8.0, car)                           # car present at t=8 only
    path = rec.save_snapshot_only(1, _result(), "mph", 40.0,
                                  frames=frames, center_t=8.0)
    assert path is not None and path.exists()
    import cv2
    img = cv2.imread(str(path))
    assert img[:20, :, :].mean() > 120               # the CAR frame was saved
    # And with no center_t it would fall back to the empty middle frame.
    path2 = rec.save_snapshot_only(2, _result(), "mph", 40.0, frames=frames)
    img2 = cv2.imread(str(path2))
    assert img2[:20, :, :].mean() < 40               # buffer-middle = empty road
