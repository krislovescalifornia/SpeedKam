#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pinpoint WHERE a node's frame time goes.

Runs the real capture + detect + encode path from your config and reports a
per-stage millisecond breakdown, so you can tell a capture/ISP bottleneck from a
CPU one from a memory-pressure one. Also prints Pi health (throttle/temp/volts),
memory/swap, and the recorder ring-buffer RAM projection -- the usual suspects
when a node crawls.

    python tools/fps_profile.py                 # uses config.yaml
    python tools/fps_profile.py --config c.yaml
    python tools/fps_profile.py --frames 60

Nothing is recorded or served; it just measures.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import cv2  # noqa: E402

from speedkam.config import load_config  # noqa: E402
from speedkam.capture import Camera  # noqa: E402
from speedkam.detector import MotionDetector  # noqa: E402


def _sh(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"(n/a: {exc})"


def _stats(xs):
    xs = sorted(xs)
    n = len(xs)
    avg = 1000 * sum(xs) / n
    med = 1000 * xs[n // 2]
    p95 = 1000 * xs[min(n - 1, int(n * 0.95))]
    mx = 1000 * xs[-1]
    return avg, med, p95, mx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--frames", type=int, default=40)
    args = ap.parse_args()

    print("=" * 68)
    print("SYSTEM HEALTH")
    print("=" * 68)
    thr = _sh(["vcgencmd", "get_throttled"])
    print(f"throttled : {thr}   (0x0 = clean; any bit set = power/heat limiting)")
    print(f"  bit0 under-voltage now, bit1 freq-capped now, bit2 throttled now,")
    print(f"  bit16 under-voltage since boot, bit18 throttled since boot")
    print(f"temp      : {_sh(['vcgencmd', 'measure_temp'])}")
    print(f"core volts: {_sh(['vcgencmd', 'measure_volts'])}")
    print(f"arm clock : {_sh(['vcgencmd', 'measure_clock', 'arm'])}  (idle Pi3B+ ~600MHz, busy ~1400MHz)")
    print("memory/swap:")
    print(_sh(["free", "-h"]))
    print(f"loadavg   : {_sh(['cat', '/proc/loadavg'])}  (>4.0 on a quad-core = overloaded)")

    print("\n" + "=" * 68)
    print("PIPELINE PER-STAGE TIMING")
    print("=" * 68)
    cfg = load_config(args.config)
    ds = float(cfg["detection"].get("detect_scale", 1.0) or 1.0)
    cam = Camera(cfg["camera"], detect_scale=ds)
    if not cam.opened:
        print(f"camera did not open: {cam.open_error}")
        return
    det = MotionDetector(cfg["detection"])
    lores = cam._lores_size if (cam.backend == "picamera2" and cam._lores_size) else None
    print(f"backend       : {cam.backend}")
    print(f"main size     : {cfg['camera']['width']}x{cfg['camera']['height']}")
    print(f"detect_scale  : {ds}")
    print(f"lores stream  : {lores if lores else 'none (software downscale of main)'}")
    print(f"warming up ({args.frames} frames discarded for AE/background settle)...")
    for _ in range(min(args.frames, 30)):
        cam.read()

    cap, conv, detect, encode, copy = [], [], [], [], []
    # Split capture into raw sensor grab vs colour convert when possible.
    raw_grab, cvt = [], []
    picam = getattr(cam, "_picam", None)

    n_none = 0
    for _ in range(args.frames):
        if picam is not None and cam._lores_size is not None:
            a = time.monotonic()
            req = picam.capture_request()
            try:
                rgb = req.make_array("main")
                yuv = req.make_array("lores")
            finally:
                req.release()
            b = time.monotonic()
            lw, lh = cam._lores_size
            df = yuv[:lh, :lw]
            frame = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            c = time.monotonic()
            raw_grab.append(b - a)
            cvt.append(c - b)
            cap.append(c - a)
        else:
            a = time.monotonic()
            t, frame = cam.read()
            b = time.monotonic()
            if frame is None:
                n_none += 1
                continue
            df = cam.detect_frame
            cap.append(b - a)

        d0 = time.monotonic()
        if df is not None:
            det.detect(df, upscale=frame.shape[1] / df.shape[1])
        else:
            det.detect(frame)
        d1 = time.monotonic()
        cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        d2 = time.monotonic()
        frame.copy()
        d3 = time.monotonic()
        detect.append(d1 - d0)
        encode.append(d2 - d1)
        copy.append(d3 - d2)

    cam.release()
    if not cap:
        print("no frames captured -- camera returned None every time.")
        return

    def row(name, xs):
        if not xs:
            return
        avg, med, p95, mx = _stats(xs)
        print(f"  {name:22s} avg {avg:7.1f}  med {med:7.1f}  p95 {p95:7.1f}  max {mx:7.1f}  ms")

    print("per-stage (ms):")
    if raw_grab:
        row("capture: sensor grab", raw_grab)
        row("capture: RGB->BGR cvt", cvt)
    row("capture: TOTAL", cap)
    row("detect", detect)
    row("encode (stream jpeg)", encode)
    row("frame.copy (recorder)", copy)

    per = (sum(cap) + sum(detect)) / len(cap)
    full = per + (sum(encode) / len(encode) if encode else 0) + (sum(copy) / len(copy) if copy else 0)
    print(f"\ncapture+detect only : ~{1000*per:6.1f} ms/frame  => {1/per:5.1f} fps ceiling")
    print(f"+ encode + copy     : ~{1000*full:6.1f} ms/frame  => {1/full:5.1f} fps")
    if n_none:
        print(f"NOTE: camera returned None {n_none}x during the run (drops/stalls).")

    print("\n" + "=" * 68)
    print("RECORDER RING-BUFFER RAM")
    print("=" * 68)
    rec = cfg["recording"]
    if rec.get("enabled", True):
        w, h = cfg["camera"]["width"], cfg["camera"]["height"]
        frame_mb = w * h * 3 / 1e6
        cap_mb = float(rec.get("max_buffer_mb", 128) or 128)
        window_s = float(rec.get("clip_seconds", 8)) * 1.5
        cap_frames = max(2, int(cap_mb * 1e6 / (w * h * 3)))
        print(f"buffer evicts by wall-time (~{window_s:.0f}s) with a {cap_mb:.0f}MB hard cap.")
        print(f"a {w}x{h} frame is ~{frame_mb:.1f}MB, so RAM is bounded at "
              f"~{cap_mb:.0f}MB ({cap_frames} frames).")
        print(f"=> at your real fps it holds ~{window_s:.0f}s of frames; the cap only")
        print(f"   bites if fps is high. Watch 'Swap' in 'free -h' while the service")
        print(f"   runs -- it should stay at 0.")
    else:
        print("recording disabled -- no ring buffer.")


if __name__ == "__main__":
    main()
