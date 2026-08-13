#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Push all existing local records to the off-site backup.

Use this for the first-time backup, or to catch up after the camera was offline.
It enqueues every row from the local events.csv and uploads them; the server
dedupes by event id, so running it repeatedly is safe.

    python tools/backfill_sync.py
    python tools/backfill_sync.py --config config.yaml
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from speedkam.config import load_config  # noqa: E402
from speedkam.sync import SyncManager  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Backfill off-site backup")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    backup = cfg.get("backup", {})
    if not backup.get("enabled"):
        print("backup.enabled is false in the config. Enable it first.")
        return
    if not backup.get("url") or not backup.get("secret"):
        print("backup.url / backup.secret are not set.")
        return

    csv_path = Path(cfg["logging"]["csv_file"])
    if not csv_path.exists():
        print(f"No events log at {csv_path}; nothing to back up.")
        return

    sync = SyncManager(backup, cfg["recording"]["output_dir"],
                       cfg["logging"]["csv_file"])

    n = 0
    with csv_path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            event = {
                "track_id": r.get("track_id"),
                "speed_kmh": r.get("speed_kmh"),
                "speed_mph": r.get("speed_mph"),
                "direction": r.get("direction"),
                "confidence": r.get("confidence"),
                "distance_m": r.get("distance_m"),
                "vehicle_type": r.get("vehicle_type") or None,
                "make": r.get("make") or None,
                "model": r.get("model") or None,
                "year": r.get("year") or None,
                "color": r.get("color") or None,
                "captured": r.get("captured") in ("1", 1, True, "True"),
                "time": r.get("wall_time"),
                "clip": r.get("clip") or None,
                "snapshot": r.get("snapshot") or None,
            }
            sync.enqueue(event)
            n += 1

    print(f"Queued {n} record(s) for {sync.host}. Uploading…")
    # Drain synchronously, reporting progress.
    while sync.pending:
        before = sync.pending
        sync._drain_once()
        if sync.pending == before:  # no progress -> a failure
            print(f"Stopped with {sync.pending} still queued. "
                  f"Last error: {sync.last_error}")
            print("They remain queued and will retry when serve.py/run.py runs.")
            return
        print(f"  {sync.uploaded_count} uploaded, {sync.pending} left…")

    print(f"Done. {sync.uploaded_count} record(s) mirrored to {sync.host}.")


if __name__ == "__main__":
    main()
