# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Off-site backup: mirror each event to a web domain you own.

Every recorded event (CSV metadata + snapshot + clip) is POSTed over HTTPS to a
small PHP receiver on your site, authenticated with a shared secret. This is a
*mirror* of the local internal storage, so if the camera is stolen or damaged
the records survive off-site.

Reliability: events are written to an on-disk queue first, then a background
worker uploads them. If the Pi is offline or the server is down, jobs stay
queued and retry on a timer -- nothing is lost. The server dedupes by event_id,
so retries are safe (idempotent).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .identity import node_id


class SyncManager:
    def __init__(self, cfg, captures_dir, csv_path):
        self.cfg = cfg
        self.url = cfg["url"]
        self.secret = cfg["secret"]
        self.captures_dir = Path(captures_dir)
        self.csv_path = Path(csv_path)
        self.include_snapshots = cfg.get("include_snapshots", True)
        self.include_clips = cfg.get("include_clips", True)
        self.verify_tls = cfg.get("verify_tls", True)
        self.timeout = cfg.get("timeout", 30)
        self.retry_seconds = cfg.get("retry_seconds", 60)

        self.queue_dir = self.captures_dir / ".sync_queue"
        self.queue_dir.mkdir(parents=True, exist_ok=True)

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        # status (read by the dashboard)
        self.last_success = None
        self.last_error = None
        self.uploaded_count = 0

        # Ledger of event_ids confirmed uploaded off-site. Retention consults
        # this before it deletes a local file, so we never drop the only copy.
        self._ledger_path = self.captures_dir / ".uploaded"
        self._ledger_lock = threading.Lock()
        self._uploaded = self._load_ledger()

    # ------------------------------------------------------------------ ledger
    def _load_ledger(self):
        ids = set()
        try:
            if self._ledger_path.exists():
                for line in self._ledger_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        ids.add(line)
        except OSError:
            pass
        return ids

    def _record_uploaded(self, event_id):
        if not event_id:
            return
        with self._ledger_lock:
            if event_id in self._uploaded:
                return
            self._uploaded.add(event_id)
            try:
                with self._ledger_path.open("a", encoding="utf-8") as f:
                    f.write(event_id + "\n")
            except OSError:
                pass  # in-memory set still lets retention proceed this run

    def is_uploaded(self, event_id) -> bool:
        with self._ledger_lock:
            return event_id in self._uploaded

    # ------------------------------------------------------------------ status
    @property
    def pending(self):
        return len(list(self.queue_dir.glob("*.json")))

    @property
    def host(self):
        try:
            return urlsplit(self.url).netloc or self.url
        except ValueError:
            return self.url

    def status(self):
        return {
            "enabled": True,
            "host": self.host,
            "pending": self.pending,
            "uploaded": self.uploaded_count,
            "last_success": self.last_success,
            "last_error": self.last_error,
        }

    # ----------------------------------------------------------------- enqueue
    def enqueue(self, event: dict):
        """Queue one event for upload. `event` is the last_event dict from the
        pipeline (time, speeds, direction, clip, snapshot, ...)."""
        # Event id doubles as the retention ledger key, which is matched against
        # each media file's stem (see RetentionManager._deletable). So derive it
        # from the media basename when there is one -- clip if present, else the
        # snapshot -- so snapshot-only (sub-threshold) passes are ledger-matched
        # too. Only truly media-less rows fall back to a synthetic id.
        clip = event.get("clip")
        snap = event.get("snapshot")
        if clip:
            event_id = Path(clip).stem
        elif snap:
            event_id = Path(snap).stem
        else:
            event_id = f"{event.get('time')}_{event.get('track_id')}"
        safe_id = "".join(c if c.isalnum() or c in "-_." else "_" for c in event_id)
        job = {
            "event_id": event_id,
            "meta": event,
            "clip": clip if self.include_clips else None,
            "snapshot": event.get("snapshot") if self.include_snapshots else None,
        }
        # Prefix with a sortable counter-free timestamp-ish name so oldest go first.
        path = self.queue_dir / f"{safe_id}.json"
        path.write_text(json.dumps(job), encoding="utf-8")
        self._wake.set()

    # ------------------------------------------------------------- worker loop
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _run(self):
        print(f"[SpeedKam] Backup sync -> {self.host} "
              f"({self.pending} queued at start).")
        while not self._stop.is_set():
            worked = self._drain_once()
            # Wait for a new event or the retry timer, whichever comes first.
            self._wake.wait(timeout=self.retry_seconds if not worked else 1.0)
            self._wake.clear()

    def _drain_once(self):
        """Upload queued jobs oldest-first until one fails or the queue empties.
        Returns True if it made progress (so we loop again promptly)."""
        jobs = sorted(self.queue_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        progressed = False
        for job_path in jobs:
            if self._stop.is_set():
                break
            try:
                job = json.loads(job_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                job_path.unlink(missing_ok=True)  # corrupt job, drop it
                continue
            if self._upload(job):
                job_path.unlink(missing_ok=True)
                self._record_uploaded(job.get("event_id"))
                self.uploaded_count += 1
                self.last_success = _now_iso()
                self.last_error = None
                progressed = True
            else:
                # Stop on first failure; retry the whole queue next cycle.
                break
        return progressed

    # ------------------------------------------------------------- remote prune
    def prune_remote(self, older_than_days) -> int:
        """Ask the receiver to delete off-site media older than N days.

        Returns the number of items the server reported pruning (0 on any
        error). Best-effort: a failure just means we try again next sweep.
        """
        if older_than_days <= 0:
            return 0
        try:
            resp = requests.post(
                self.url,
                headers={"X-SpeedKam-Key": self.secret},
                data={"action": "prune", "node": node_id(),
                      "prune_older_than_days": int(older_than_days)},
                timeout=self.timeout,
                verify=self.verify_tls,
            )
            if resp.status_code == 200:
                try:
                    body = resp.json()
                except ValueError:
                    return 0
                if body.get("ok"):
                    return int(body.get("pruned", 0) or 0)
                self.last_error = f"prune rejected: {resp.text[:120]}"
            else:
                self.last_error = f"prune HTTP {resp.status_code}: {resp.text[:120]}"
        except requests.RequestException as exc:
            self.last_error = f"prune {type(exc).__name__}: {exc}"
        return 0

    # ------------------------------------------------------------- remote enrich
    def enrich_remote(self, event_id, attrs) -> int:
        """Ask the receiver to fill in recognized attributes for an event that
        was already mirrored off-site (deferred recognition write-back).

        ``attrs`` is a dict of any of vehicle_type/make/model/year/color. The
        receiver rewrites that event's off-site CSV row in place. Returns the
        number of remote rows updated: 0 means the row isn't off-site yet (so
        the caller should retry later) or the request failed. Best-effort.
        """
        payload = {k: v for k, v in (attrs or {}).items() if v}
        if not event_id or not payload:
            return 0
        try:
            resp = requests.post(
                self.url,
                headers={"X-SpeedKam-Key": self.secret},
                data={"action": "enrich", "node": node_id(),
                      "event_id": event_id,
                      "attrs": json.dumps(payload)},
                timeout=self.timeout,
                verify=self.verify_tls,
            )
            if resp.status_code == 200:
                try:
                    body = resp.json()
                except ValueError:
                    return 0
                if body.get("ok"):
                    return int(body.get("updated", 0) or 0)
                self.last_error = f"enrich rejected: {resp.text[:120]}"
            else:
                self.last_error = f"enrich HTTP {resp.status_code}: {resp.text[:120]}"
        except requests.RequestException as exc:
            self.last_error = f"enrich {type(exc).__name__}: {exc}"
        return 0

    def _upload(self, job) -> bool:
        data = {
            "event_id": job["event_id"],
            "node": node_id(),
            "meta": json.dumps(job["meta"]),
        }
        files = {}
        opened = []
        try:
            for key in ("snapshot", "clip"):
                name = job.get(key)
                if not name:
                    continue
                fpath = self.captures_dir / name
                if fpath.exists():
                    fh = fpath.open("rb")
                    opened.append(fh)
                    mime = "image/jpeg" if key == "snapshot" else "video/mp4"
                    files[key] = (name, fh, mime)
            resp = requests.post(
                self.url,
                headers={"X-SpeedKam-Key": self.secret},
                data=data,
                files=files or None,
                timeout=self.timeout,
                verify=self.verify_tls,
            )
            if resp.status_code == 200:
                try:
                    ok = resp.json().get("ok", False)
                except ValueError:
                    ok = False
                if ok:
                    return True
                self.last_error = f"server rejected: {resp.text[:120]}"
            else:
                self.last_error = f"HTTP {resp.status_code}: {resp.text[:120]}"
        except requests.RequestException as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            for fh in opened:
                fh.close()
        return False


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")
