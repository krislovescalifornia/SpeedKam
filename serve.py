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

This is a thin launcher so the project runs uninstalled (the Pi's apt flow).
If you `pip install` the project, use the `speedkam-serve` command instead.
"""
import sys
from pathlib import Path

# Make `speedkam` importable when running from a source checkout without install.
sys.path.insert(0, str(Path(__file__).parent / "src"))

from speedkam.cli import serve_main  # noqa: E402

if __name__ == "__main__":
    serve_main()
