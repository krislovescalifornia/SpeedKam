#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dead-simple speed camera: time a car across two fixed lines.

No homography, no undistortion, no gates. A car crossing the stretch between
two vertical image lines at a KNOWN speed fixes the real distance of that
stretch (one number per travel direction). Every other car's speed is then just
that distance divided by its own crossing time. Uses REAL wall-clock timestamps
per frame, so it is immune to frame-rate variation.

    # calibrate: drive past once each way at a known speed
    python simple_speed.py --calibrate 25            # (--units mph, default)
    # then measure live
    python simple_speed.py
    # test on a recorded clip
    python simple_speed.py --video clip.mp4 --calibrate 25
"""
from __future__ import annotations
import argparse, json, os, time
import cv2, numpy as np

CONFIG = os.path.join(os.path.dirname(__file__), "simple_speed.json")
MPS_PER_MPH = 0.44704
MPS_PER_KMH = 0.277778


def load_cfg():
    if os.path.exists(CONFIG):
        return json.load(open(CONFIG))
    return {"x_a": 1000, "x_b": 450, "min_area": 1500,
            "d_east_m": None, "d_west_m": None, "units": "mph"}


def save_cfg(c):
    json.dump(c, open(CONFIG, "w"), indent=2)


class Frames:
    """Yield (timestamp_seconds, frame). Real wall-clock time for the camera;
    frame-index/fps for a file so recorded clips replay at true speed."""
    def __init__(self, video=None):
        self.video = video
        if video:
            self.cap = cv2.VideoCapture(video)
            self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
            self.n = 0
        else:
            from picamera2 import Picamera2
            self.pc = Picamera2()
            self.pc.configure(self.pc.create_video_configuration(
                main={"size": (1456, 1088), "format": "RGB888"}))
            self.pc.start(); time.sleep(1.0)

    def __iter__(self):
        if self.video:
            while True:
                ok, f = self.cap.read()
                if not ok:
                    return
                yield self.n / self.fps, f
                self.n += 1
        else:
            while True:
                yield time.monotonic(), self.pc.capture_array("main")


class Tracker:
    """One car at a time: accumulate the moving blob's x-centroid vs time until
    it leaves, then hand back the finished (t[], x[]) trail."""
    def __init__(self, min_area):
        self.bg = cv2.createBackgroundSubtractorMOG2(
            history=200, varThreshold=40, detectShadows=False)
        self.k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        self.min_area = min_area
        self.ts, self.xs = [], []
        self.missed = 0

    def update(self, t, frame):
        m = self.bg.apply(frame)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, self.k)
        m = cv2.dilate(m, self.k, iterations=2)
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        big = [c for c in cnts if cv2.contourArea(c) > self.min_area]
        if big:
            x, _, w, _ = cv2.boundingRect(max(big, key=cv2.contourArea))
            self.ts.append(t); self.xs.append(x + w / 2.0); self.missed = 0
            return None
        # no blob: if we were tracking one, the car has left -> finish it
        if self.xs:
            self.missed += 1
            if self.missed >= 3:
                trail = (np.array(self.ts), np.array(self.xs))
                self.ts, self.xs, self.missed = [], [], 0
                return trail
        return None


def cross_time(ts, xs, xa, xb):
    """Seconds to travel between image columns xa and xb, and direction."""
    def tc(xc):
        for i in range(1, len(xs)):
            if (xs[i - 1] - xc) * (xs[i] - xc) <= 0 and xs[i] != xs[i - 1]:
                f = (xc - xs[i - 1]) / (xs[i] - xs[i - 1])
                return ts[i - 1] + f * (ts[i] - ts[i - 1])
        return None
    ta, tb = tc(xa), tc(xb)
    if ta is None or tb is None:
        return None, None
    # In this camera's view eastbound runs right-to-left (x decreases), matching
    # the node's own labelling of the calibration passes.
    direction = "eastbound" if xs[-1] < xs[0] else "westbound"
    return abs(tb - ta), direction


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default=None, help="test on a clip instead of the camera")
    ap.add_argument("--calibrate", type=float, default=None,
                    help="known speed of the passes you drive now (sets the stretch distance)")
    ap.add_argument("--units", default=None, choices=["mph", "kmh"])
    args = ap.parse_args()
    c = load_cfg()
    if args.units:
        c["units"] = args.units
    per = MPS_PER_MPH if c["units"] == "mph" else MPS_PER_KMH
    tk = Tracker(c["min_area"])
    print(f"lines x={c['x_a']} and x={c['x_b']} | "
          f"D_east={c['d_east_m']} D_west={c['d_west_m']} m | units={c['units']}")
    if args.calibrate:
        print(f"CALIBRATION: drive past at {args.calibrate} {c['units']} — "
              f"one eastbound, one westbound. Ctrl-C when done.")
    for t, frame in Frames(args.video):
        trail = tk.update(t, frame)
        if trail is None:
            continue
        ts, xs = trail
        import sys as _sys
        print(f"  [motion: {len(xs)} pts, x {xs.min():.0f}..{xs.max():.0f}, {ts[-1]-ts[0]:.2f}s]",
              file=_sys.stderr, flush=True)
        if len(xs) < 4:
            continue
        dt, direction = cross_time(ts, xs, c["x_a"], c["x_b"])
        if dt is None or dt <= 0:
            continue
        key = "d_east_m" if direction == "eastbound" else "d_west_m"
        if args.calibrate:
            c[key] = args.calibrate * per * dt
            save_cfg(c)
            print(f"  {direction}: crossed in {dt:.3f}s -> {key} = {c[key]:.2f} m  (saved)")
        else:
            D = c.get(key)
            if D is None:
                print(f"  {direction}: not calibrated yet — run --calibrate first")
            else:
                mph = D / dt / per
                print(f"  {direction}: {mph:.0f} {c['units']}  (crossed in {dt:.3f}s)")


if __name__ == "__main__":
    main()
