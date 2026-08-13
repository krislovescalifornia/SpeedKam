#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SpeedKam entry point.

    python run.py                 # run with config.yaml
    python run.py --config x.yaml # custom config
    python run.py --source 1      # override camera index/file (test rig)
    python run.py --no-display    # headless (Raspberry Pi deployment)

Calibrate first with:  python tools/calibrate.py
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Quiet OpenCV's backend chatter before anything imports cv2.
os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")

sys.path.insert(0, str(Path(__file__).parent / "src"))

from speedkam.config import load_config  # noqa: E402
from speedkam.pipeline import SpeedCamera  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="SpeedKam vehicle speed camera")
    ap.add_argument("--config", default="config.yaml", help="path to config.yaml")
    ap.add_argument("--source", help="override camera source (index or file/URL)")
    ap.add_argument("--no-display", action="store_true", help="run headless")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.source is not None:
        cfg["camera"]["source"] = args.source
    if args.no_display:
        cfg["display"]["show_window"] = False

    SpeedCamera(cfg).run()


if __name__ == "__main__":
    main()
