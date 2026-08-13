#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SpeedKam entry point (desktop preview window / headless).

    python run.py                 # run with config.yaml
    python run.py --config x.yaml # custom config
    python run.py --source 1      # override camera index/file (test rig)
    python run.py --no-display    # headless (Raspberry Pi deployment)

Calibrate first with:  python tools/calibrate.py

This is a thin launcher so the project runs uninstalled (the Pi's apt flow).
If you `pip install` the project, use the `speedkam` command instead.
"""
import sys
from pathlib import Path

# Make `speedkam` importable when running from a source checkout without install.
sys.path.insert(0, str(Path(__file__).parent / "src"))

from speedkam.cli import run_main  # noqa: E402

if __name__ == "__main__":
    run_main()
