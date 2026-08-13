# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Console entry points for SpeedKam.

Two commands, installed via ``[project.scripts]`` in pyproject.toml:

    speedkam          -> run_main()   the pipeline (desktop preview / headless)
    speedkam-serve    -> serve_main() the web dashboard + pipeline

The root ``run.py`` / ``serve.py`` launchers delegate here after bootstrapping
``src`` onto ``sys.path``, so the project still runs uninstalled on the Pi
(``python serve.py``) exactly as before.
"""
from __future__ import annotations

import argparse
import os
import socket

# Quiet OpenCV's backend chatter before anything imports cv2. Must happen before
# the local pipeline/web imports below (which are deferred into the functions).
os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")

from .config import load_config


def run_main(argv=None):
    """`speedkam` -- run the pipeline with a desktop preview (or headless)."""
    ap = argparse.ArgumentParser(prog="speedkam",
                                 description="SpeedKam vehicle speed camera")
    ap.add_argument("--config", default="config.yaml", help="path to config.yaml")
    ap.add_argument("--source", help="override camera source (index or file/URL)")
    ap.add_argument("--no-display", action="store_true", help="run headless")
    args = ap.parse_args(argv)

    from .pipeline import SpeedCamera  # deferred: imports cv2

    cfg = load_config(args.config)
    if args.source is not None:
        cfg["camera"]["source"] = args.source
    if args.no_display:
        cfg["display"]["show_window"] = False
    SpeedCamera(cfg).run()


def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def serve_main(argv=None):
    """`speedkam-serve` -- run the pipeline AND the web dashboard."""
    ap = argparse.ArgumentParser(prog="speedkam-serve",
                                 description="SpeedKam web dashboard")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--source", help="override camera source (index or file/URL)")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    args = ap.parse_args(argv)

    from .web import Runner, auth_enabled, create_app  # deferred: imports cv2/flask

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
    protected = auth_enabled((cfg.get("web") or {}).get("auth"))
    lock = "password-protected" if protected else "OPEN (no auth -- trusted LAN only)"
    print(f"\n[SpeedKam] Dashboard: http://{shown}:{port}  [{lock}]    (Ctrl+C to stop)\n")
    try:
        app.run(host=host, port=port, threaded=True, debug=False,
                use_reloader=False)
    finally:
        runner.stop()
