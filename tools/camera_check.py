#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Find and preview your camera.

Run this first on a new machine to discover which source index your webcam is,
then put that number in config.yaml under camera.source.

    python tools/camera_check.py            # probe indices 0..4, all backends
    python tools/camera_check.py --show 0    # live preview of index 0 (q quits)
"""
from __future__ import annotations

import argparse
import os
import sys

# Silence OpenCV's C++ backend chatter (the WARN/ERROR spam you see while
# probing non-existent camera indices is harmless). Must be set before cv2.
os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")
os.environ.setdefault("OPENCV_VIDEOIO_DEBUG", "0")

import cv2

try:
    cv2.setLogLevel(cv2.LOG_LEVEL_SILENT)
except Exception:
    pass

if sys.platform.startswith("win"):
    BACKENDS = [("DSHOW", cv2.CAP_DSHOW), ("MSMF", cv2.CAP_MSMF)]
else:  # Linux / Raspberry Pi
    BACKENDS = [("V4L2", cv2.CAP_V4L2), ("ANY", 0)]


def probe():
    print("Probing camera indices 0..4 across backends...\n")
    found = []
    for name, api in BACKENDS:
        for idx in range(5):
            cap = cv2.VideoCapture(idx, api)
            ok = cap.isOpened()
            frame = None
            if ok:
                _, frame = cap.read()
            cap.release()
            if frame is not None:
                h, w = frame.shape[:2]
                print(f"  OK  backend={name:5s} index={idx}  frame={w}x{h}")
                found.append((name, idx))
    if not found:
        print("  No cameras captured a frame.")
        print("  * Close any app using the webcam (Zoom, Teams, Camera app).")
        print("  * Check Windows Settings > Privacy > Camera > allow desktop apps.")
        print("  * Try running from a normal terminal (not a restricted sandbox).")
    else:
        name, idx = found[0]
        print(f"\nSuggested config.yaml:\n  camera:\n    source: {idx}")
        if name == "DSHOW":
            print("    windows_use_dshow: true")
        else:
            print("    windows_use_dshow: false")
    return found


def show(index):
    for name, api in BACKENDS:
        cap = cv2.VideoCapture(index, api)
        if cap.isOpened():
            ok, _ = cap.read()
            if ok:
                print(f"Previewing index {index} via {name}. Press q to quit.")
                while True:
                    ok, frame = cap.read()
                    if not ok:
                        break
                    cv2.imshow("camera_check", frame)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        break
                cap.release()
                cv2.destroyAllWindows()
                return
        cap.release()
    print(f"Could not open index {index}.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", type=int, help="live-preview this index")
    args = ap.parse_args()
    if args.show is not None:
        show(args.show)
    else:
        probe()


if __name__ == "__main__":
    main()
