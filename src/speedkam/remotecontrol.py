"""Pull-based remote control + heartbeat for a camera behind home NAT.

The off-site host can't reach *into* your home network, so control flows the
other way: this client periodically POSTs the camera's status to the receiver
(``action=sync``) and gets back the DESIRED settings the operator set on the
off-site dashboard, applying any that changed. One outbound round trip -- no
port-forwarding, nothing exposed.

Two properties keep local and remote control from fighting:
  * The desired settings carry a monotonic ``rev``. We only apply when it
    changes, so between remote edits your on-Pi dashboard tweaks stick.
  * The last applied ``rev`` is persisted in RuntimeState, so a reboot doesn't
    re-apply a stale remote value over a newer local one.

Heartbeat doubles as liveness: the receiver server-stamps each sync, so the
dashboard can show "camera online / last seen 12s ago" and live counts even
when you're nowhere near the camera's LAN.
"""
from __future__ import annotations

import json
import threading
import time

import requests


class RemoteControl:
    def __init__(self, cfg, url, secret, camera, verify_tls=True, timeout=15):
        self.poll_seconds = max(10, int((cfg or {}).get("poll_seconds", 30)))
        self.url = url
        self.secret = secret
        self.camera = camera            # SpeedCamera: status source + apply target
        self.verify_tls = verify_tls
        self.timeout = timeout

        self._stop = threading.Event()
        self._thread = None
        self._first = True
        # status read by the on-Pi dashboard
        self.last_sync = None
        self.last_error = None

    # --------------------------------------------------------------- lifecycle
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        print(f"[SpeedKam] Remote control -> {self.url} every {self.poll_seconds}s.")
        # First check-in immediately, then every poll_seconds.
        while True:
            self._sync_once()
            if self._stop.wait(self.poll_seconds):
                break

    # ------------------------------------------------------------------ status
    def _status(self):
        cam = self.camera
        return {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "running": cam.running,
            "fps": round(cam.current_fps, 1),
            "total_count": cam.total_count,
            "speeder_count": cam.speeder_count,
            "speedkapture_threshold": cam.speedkapture_threshold,
            "speed_limit_kmh": cam.limit_kmh,
            "units": cam.units,
            "last_event": cam.last_event,
        }

    # -------------------------------------------------------------------- sync
    def _sync_once(self):
        try:
            resp = requests.post(
                self.url,
                headers={"X-SpeedKam-Key": self.secret},
                data={"action": "sync", "status": json.dumps(self._status())},
                timeout=self.timeout,
                verify=self.verify_tls,
            )
            if resp.status_code != 200:
                self.last_error = f"HTTP {resp.status_code}"
                return
            body = resp.json()
            if not body.get("ok"):
                self.last_error = "server rejected sync"
                return
            self.last_sync = time.strftime("%Y-%m-%dT%H:%M:%S")
            self.last_error = None
            self._apply(body.get("settings") or {})
        except (requests.RequestException, ValueError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._first = False

    # ------------------------------------------------------------------- apply
    def _apply(self, settings):
        rev = settings.get("rev")
        if rev is None:
            return  # operator has never set anything remotely
        last = self.camera.state.get("remote_rev")
        if last is not None and rev == last:
            return  # nothing new since we last applied
        # New revision -> adopt the operator's desired values.
        thr = settings.get("speedkapture_threshold")
        if thr is not None:
            try:
                new = self.camera.set_speedkapture_threshold(float(thr))
                print(f"[SpeedKam] Remote set SpeedKapture threshold -> {new} "
                      f"{self.camera.units} (rev {rev}).")
            except (TypeError, ValueError):
                pass
        self.camera.state.set("remote_rev", rev)
