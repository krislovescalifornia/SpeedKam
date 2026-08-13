# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Local + remote media rotation, so neither the SD card nor the off-site host
fills up.

Two independent knobs (by design):

  * ``retention.local_days``  -- delete LOCAL clips/snapshots older than N days.
      When ``retention.require_backup`` is on, a file is only deleted once
      off-site backup has CONFIRMED it uploaded (see SyncManager's ledger), so
      we never drop the only copy of something.
  * ``backup.remote_retention_days`` -- ask the receiver to delete OFF-SITE
      media older than M days.

CSV log rows are never touched -- they're tiny, and keeping them means the
daily/weekly/monthly vehicle counts survive long after the video is gone.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

MEDIA_GLOBS = ("*.mp4", "*.jpg")


class RetentionManager:
    def __init__(self, cfg, output_dir, sync=None, remote_retention_days=0):
        self.enabled = bool(cfg.get("enabled"))
        self.local_days = int(cfg.get("local_days", 14))
        self.require_backup = bool(cfg.get("require_backup", True))
        self.interval = max(60, int(cfg.get("interval_seconds", 3600)))
        self.output_dir = Path(output_dir)
        self.sync = sync
        self.remote_retention_days = int(remote_retention_days or 0)

        self._stop = threading.Event()
        self._thread = None
        # status (read by the dashboard)
        self.last_sweep = None
        self.deleted_total = 0
        self.last_deleted = 0
        self.remote_pruned_total = 0

    # ------------------------------------------------------------- lifecycle
    def start(self):
        if not (self.enabled or self._remote_enabled()):
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _remote_enabled(self):
        return (self.sync is not None and self.remote_retention_days > 0)

    def _run(self):
        where = []
        if self.enabled:
            where.append(f"local>{self.local_days}d")
        if self._remote_enabled():
            where.append(f"remote>{self.remote_retention_days}d")
        print(f"[SpeedKam] Retention on ({', '.join(where)}), "
              f"sweep every {self.interval}s.")
        # A short initial delay lets the camera warm up before the first sweep.
        while not self._stop.wait(min(30, self.interval)):
            self.sweep()
            self._stop.wait(self.interval - min(30, self.interval))

    # ---------------------------------------------------------------- sweeps
    def sweep(self):
        deleted = 0
        if self.enabled:
            deleted = self._sweep_local()
        if self._remote_enabled():
            self._prune_remote()
        self.last_deleted = deleted
        self.deleted_total += deleted
        self.last_sweep = time.strftime("%Y-%m-%dT%H:%M:%S")
        return deleted

    def _sweep_local(self):
        if not self.output_dir.exists():
            return 0
        cutoff = time.time() - self.local_days * 86400
        deleted = 0
        for pattern in MEDIA_GLOBS:
            for path in self.output_dir.glob(pattern):
                try:
                    if path.stat().st_mtime >= cutoff:
                        continue
                except OSError:
                    continue
                if not self._deletable(path):
                    continue
                try:
                    path.unlink()
                    deleted += 1
                except OSError:
                    pass
        if deleted:
            print(f"[SpeedKam] Retention: deleted {deleted} old local file(s).")
        return deleted

    def _deletable(self, path):
        """True if this media file may be removed locally right now."""
        if not self.require_backup:
            return True
        if self.sync is None:
            return False  # require_backup but no backup configured -> keep
        return self.sync.is_uploaded(path.stem)

    def _prune_remote(self):
        try:
            n = self.sync.prune_remote(self.remote_retention_days)
        except Exception:  # noqa: BLE001 - remote prune is best-effort
            n = None
        if n:
            self.remote_pruned_total += n
            print(f"[SpeedKam] Retention: remote pruned {n} old item(s).")

    # ------------------------------------------------------------------ status
    def status(self):
        return {
            "enabled": self.enabled,
            "local_days": self.local_days,
            "require_backup": self.require_backup,
            "remote_retention_days": self.remote_retention_days,
            "last_sweep": self.last_sweep,
            "deleted_total": self.deleted_total,
            "last_deleted": self.last_deleted,
        }
