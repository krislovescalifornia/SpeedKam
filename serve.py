#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SpeedKam web dashboard entry point.

Runs the speed-camera pipeline AND a web UI (live view, recent clips, stats,
browser-based calibration). This is the recommended way to run on the Pi:

    python serve.py                       # http://<this-machine>:8080
    python serve.py --source 1            # override camera
    python serve.py --port 9000

Then open the printed URL in any browser on the same network.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")
sys.path.insert(0, str(Path(__file__).parent / "src"))

from speedkam.config import load_config  # noqa: E402
from speedkam.web import Runner, create_app  # noqa: E402


def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main():
    ap = argparse.ArgumentParser(description="SpeedKam web dashboard")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--source", help="override camera source (index or file/URL)")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.source is not None:
        cfg["camera"]["source"] = args.source
    # The web view is the display; never open a desktop window here.
    cfg["display"]["show_window"] = False

    host = args.host or cfg["web"]["host"]
    port = args.port or cfg["web"]["port"]

    runner = Runner(cfg)
    runner.start()
    app = create_app(runner)

    shown = _lan_ip() if host in ("0.0.0.0", "::") else host
    print(f"\n[SpeedKam] Dashboard: http://{shown}:{port}    (Ctrl+C to stop)\n")
    try:
        app.run(host=host, port=port, threaded=True, debug=False,
                use_reloader=False)
    finally:
        runner.stop()


if __name__ == "__main__":
    main()
