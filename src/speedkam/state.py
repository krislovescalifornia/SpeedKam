# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Runtime-adjustable settings that survive a restart.

Some knobs (the SpeedKapture capture threshold) are meant to be changed live
from the dashboard. Rewriting the heavily-commented ``config.yaml`` on every
tweak would destroy those comments, so instead we persist just these values to
a tiny JSON file beside the captures. ``config.yaml`` still supplies the
defaults; this file overrides them once the user has changed something.

Thread-safe: the pipeline thread reads it while the Flask thread writes it.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path


class RuntimeState:
    def __init__(self, path, defaults: dict):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data = dict(defaults)
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                disk = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(disk, dict):
                    # Only adopt keys we know about, keeping types sane.
                    for k in self._data:
                        if k in disk:
                            self._data[k] = disk[k]
        except (OSError, ValueError):
            pass  # corrupt/missing state just falls back to config defaults

    def get(self, key):
        with self._lock:
            return self._data.get(key)

    def set(self, key, value):
        with self._lock:
            self._data[key] = value
            self._save_locked()

    def as_dict(self):
        with self._lock:
            return dict(self._data)

    def _save_locked(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            tmp.replace(self.path)  # atomic on the same filesystem
        except OSError:
            pass  # best-effort; a failed save just means it won't persist
