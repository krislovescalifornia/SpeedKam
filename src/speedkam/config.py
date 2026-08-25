# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Configuration loading with sensible defaults and dotted attribute access."""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

DEFAULTS = {
    "camera": {
        "backend": "opencv",
        "source": 0,
        "width": 1280,
        "height": 720,
        "fps": 30,
        "windows_use_dshow": True,
        "manual_exposure": -1,
        # Raspberry Pi CSI (picamera2) sensor controls. exposure_us/analogue_gain
        # of 0 = auto; `fps` (above) always caps the frame duration so auto-
        # exposure can't strangle the frame rate in dim light.
        "exposure_us": 0,
        "analogue_gain": 0,
        "loop": False,
    },
    "detection": {
        "min_area": 1500,
        "max_area": 500000,
        "history": 400,
        "var_threshold": 40,
        # Run detection on a downscaled copy of each frame (1.0 = full res).
        # Downscaling is the biggest Pi FPS win (0.4 cuts per-frame cost to ~16%).
        # Coordinates and areas are scaled back to full resolution, so nothing
        # downstream (calibration, min_area/max_area, annotation) changes.
        "detect_scale": 0.4,
        # MOG2 shadow modelling off by default: cheaper, and shadows are dropped
        # downstream anyway. See config.yaml for the accuracy trade-off.
        "detect_shadows": False,
        "morph_kernel": 5,
        "min_hits": 3,
    },
    "tracker": {"max_match_distance": 120, "max_missed": 12},
    "light_gate": {
        # Pause detection/recording when the scene is too dark to work (dusk ->
        # night) and auto-resume when light returns (dawn). The service stays up
        # (dashboard + live view keep serving) so the camera detects morning
        # itself -- no clock, sunset table, or location needed.
        "enabled": True,
        # Mean frame brightness (0-255). Hysteresis: fall asleep only below
        # sleep_below, wake only above wake_above; the gap stops dusk/dawn flicker
        # from flapping the gate. Tune per site from the dashboard's live
        # brightness readout (daylight ~90-100, full dark <15 on the ref node).
        "sleep_below": 40,
        "wake_above": 60,
        # Seconds the brightness must hold past a threshold before switching, so a
        # passing headlight or a momentary dark truck can't toggle the gate.
        "dwell_seconds": 30.0,
    },
    "speed": {
        "min_samples": 6,
        "min_speed_kmh": 3,
        "max_speed_kmh": 200,
        # --- SIMPLE two-line crossing-time speed --------------------------------
        # A car crossing between image columns x_a and x_b at a KNOWN speed fixes
        # the real distance of that stretch -- one number per travel direction
        # (d_east_m / d_west_m), set once by driving past each way at a known
        # speed (the log prints the crossing time; d = known_mps * seconds). Every
        # other car's speed is then that distance over its own crossing time. Raw
        # pixel x + timestamps only -- no homography, no undistortion. Per node,
        # so x_a/x_b and the two distances live in config.local.yaml. Until a
        # direction's distance is set the node logs its crossing time and reports
        # no speed for that direction (detection-only).
        "x_a": 1000,
        "x_b": 450,
        "d_east_m": None,
        "d_west_m": None,
        # --- Automatic false-positive rejection (all PIXEL-ONLY) ----------------
        # Cyclists, pedestrians, and wind-blown foreground plants get tracked and
        # timed just like cars. Cheap pixel checks throw the junk out
        # automatically -- it's still LOGGED (visible in the dashboard's
        # auto-reject bin, restorable) but kept out of every count, average and
        # graph. All live-tunable from either dashboard and persisted per node.
        #   min_vehicle_aspect -- minimum bbox aspect ratio (width/height). On a
        #     side-on camera a car's box is WIDE (~2-3), a walking person's TALL
        #     (~0.3-0.5), a side-on cyclist ~square (~0.9). 1.4 keeps cars and
        #     rejects people/bikes. Lower it for a head-on mount. 0 disables.
        #   min_car_width_px -- the size half of the car filter: a car is a BIG
        #     object even in the far lane (bbox width well over 200px), a dog,
        #     bike or pedestrian is small. With the aspect check, nothing that
        #     isn't a car clears both. 0 disables.
        #   max_area_cv -- maximum coefficient of variation (std/mean) of the
        #     bbox area across the pass. A vehicle's silhouette changes size
        #     smoothly (low CV); noise and wind-blown foliage flicker (high CV).
        #     0.90 only fires on egregious flicker, so it never rejects a real
        #     car. 0 disables.
        #   dedupe_seconds -- count each drive-by ONCE: a second confirmed pass in
        #     the same direction within this window is a fragmented re-detection
        #     of the same vehicle and is rejected. 0 disables.
        "min_vehicle_aspect": 1.4,
        "min_car_width_px": 200,
        "max_area_cv": 0.90,
        "dedupe_seconds": 3.0,
        "speed_limit_kmh": 40.2336,   # 25 mph
        "display_units": "mph",
        "direction_positive": "Westbound",
        "direction_negative": "Eastbound",
    },
    "recording": {
        "enabled": True,
        "output_dir": "captures",
        "clip_seconds": 8,
        # Hard RAM ceiling for the pre-roll clip buffer. The buffer is evicted by
        # wall-time (~clip_seconds), so this is a backstop; keep it small on a
        # 1GB Pi (a 720p frame is ~2.7MB, so 128MB ~= 46 frames).
        "max_buffer_mb": 128,
        # Cap how many frames/sec are STORED for clips, independent of the
        # detection rate. The parallel pipeline captures at the sensor rate
        # (~30fps). 0 = store every frame for true full-rate clips (the default):
        # setting the throttle equal to the sensor rate backfires -- jitter drops
        # ~1 in 4 frames and you get only ~22fps clips. Set a value BELOW the
        # sensor rate only to stretch the pre-roll within max_buffer_mb.
        "record_fps": 0,
        "save_only_with_speed": True,
        "save_snapshot": True,
        "burn_overlay": True,
        # SpeedKapture: only save + off-site post a clip/snapshot when the
        # measured speed (in display_units) is ABOVE this threshold. Passes
        # below it are still counted, timed, given a direction, and recognized
        # -- there's just no clip. 0 = capture every vehicle. Adjustable live
        # from the dashboard (persisted to the runtime state file below).
        "speedkapture_threshold": 0,
        # Save a lightweight JPEG snapshot for EVERY counted pass, even ones
        # below the SpeedKapture threshold (which get no clip). This is what
        # lets a deferred recognition worker fill in type/make/model later for
        # sub-threshold passes too -- there has to be an image on disk to look
        # at. Cheap (one JPEG/pass); leave off if you only enrich captured passes.
        "always_snapshot": False,
        # Small JSON file holding dashboard-adjustable settings (SpeedKapture)
        # so they survive a restart without rewriting the commented config.yaml.
        "state_file": "captures/runtime.json",
    },
    "retention": {
        # Auto-delete OLD LOCAL media so the Pi's SD card doesn't fill up.
        "enabled": False,
        # Delete local clips/snapshots older than this many days.
        "local_days": 14,
        # Only delete local media that off-site backup has CONFIRMED uploaded.
        # Keep true whenever backup is on. If backup is disabled you must set
        # this false to allow pure age-based cleanup (else nothing is deleted).
        "require_backup": True,
        # How often the cleanup sweep runs (seconds).
        "interval_seconds": 3600,
    },
    "display": {"show_window": True, "draw_debug": True},
    "logging": {"csv_file": "captures/events.csv"},
    "web": {
        "host": "0.0.0.0",
        "port": 8080,
        # Optional HTTP Basic Auth for the LAN dashboard. Off unless a password
        # is set (put the real one in config.local.yaml). When set, every request
        # -- pages, APIs, the live stream, and /captures -- requires it.
        "auth": {"username": "admin", "password": ""},
        # Show Restart/Shutdown buttons on the dashboard that reboot or power off
        # the whole Pi. Needs the sudoers drop-in from install-service.sh. Set
        # false to hide the buttons and refuse the endpoint (e.g. if the LAN is
        # not fully trusted -- a power-off on a remote node needs a physical
        # visit to undo).
        "allow_power_control": True,
        # Cap the live-preview JPEG-encode rate. The detection loop is never
        # throttled by it. 0 = unthrottled.
        "stream_fps": 10,
        # Downscale the live preview to this width before JPEG-encoding it -- a
        # big CPU saving on a weak Pi (encoding 1280x720 ~34ms vs ~8ms at 640).
        # 0 = full resolution.
        "stream_max_width": 640,
    },
    "backup": {
        "enabled": False,
        "url": "",
        "secret": "",
        "include_snapshots": True,
        "include_clips": True,
        # Full mirror: also back up counted passes BELOW the SpeedKapture
        # threshold (their CSV row + snapshot), not just captured clips. With
        # this on, the off-site copy is a complete historical record, so when
        # local retention trims old media the remote still has everything.
        # (Clips still only exist for captured passes -- there's no clip to
        # mirror below threshold -- but every row and snapshot is mirrored.)
        "mirror_all": False,
        "verify_tls": True,
        "timeout": 30,
        "retry_seconds": 60,
        # Remote rotation: tell the receiver to delete OFF-SITE media older than
        # this many days so remote storage doesn't fill up. 0 = keep forever.
        # This is a separate knob from retention.local_days on purpose.
        "remote_retention_days": 0,
    },
    "netcfg": {
        # Wi-Fi onboarding (speedkam-netcfg.service). On boot, if the node can't
        # reach the internet, it raises a Wi-Fi access point named
        # "<ap_ssid_prefix>-<nodeid>" and serves a phone-friendly setup page so
        # you can join it to a nearby network -- no screen, no SSH, no
        # re-imaging. Already-online boots are a no-op. See src/speedkam/netcfg.py.
        "enabled": True,
        # Wireless interface to host the AP on and join from.
        "interface": "wlan0",
        # AP name prefix; the node's short id is appended so nearby nodes differ.
        "ap_ssid_prefix": "SpeedKam-Setup",
        # Leave blank for an OPEN setup AP (easiest onboarding; the AP is only up
        # while the node is offline and is torn down the moment it joins). Set a
        # password of 8+ chars to lock the setup AP with WPA2 instead.
        "ap_password": "",
        # Seconds to wait for real connectivity at boot before opening the AP.
        "online_timeout": 90,
        # Title shown on the setup page.
        "portal_title": "SpeedKam Wi-Fi setup",
        # NetworkManager 'shared' mode gateway = the portal's address. Only
        # change if it collides with the site network you're joining.
        "captive_ip": "10.42.0.1",
    },
    "control": {
        # Pull-based remote control + heartbeat. The camera periodically POSTs
        # its status to the SAME off-site host as backup (reusing backup.url +
        # backup.secret) and receives any settings the operator changed on the
        # off-site dashboard, applying them. This is how you adjust a camera
        # that lives behind home NAT -- it reaches out; nothing reaches in.
        # Needs backup.enabled with a valid url + secret.
        "enabled": False,
        # How often the camera checks in / pulls settings (seconds).
        "poll_seconds": 30,
    },
    "recognition": {
        # Optional, best-effort vehicle attributes (type + color). The GATE OF
        # RECORD (real vehicle? how fast?) is deterministic geometry in the
        # pipeline, not this. Fully optional: with this off (the default), or
        # ultralytics/torch not installed, every pass is still counted and timed
        # -- color is done inline on a bare CPU, type just stays blank. Leave OFF
        # unless you specifically want the COCO type label; the YOLO type/gate is
        # a heavy, hot dependency the geometry gates don't need (kept dormant as
        # a break-glass classifier). Make/model/year recognition was dropped.
        "enabled": False,
        # Deferred (offloaded) recognition. When true, this node does NOT load
        # or run the heavy YOLO models -- it only does the cheap color pass and
        # persists snapshots, leaving type/make/model blank in the CSV. A
        # separate machine runs tools/recognize_worker.py later to fill them in
        # from the saved images. Set true on a Raspberry Pi to keep the capture
        # loop fast; leave false to recognize inline (desktop/GPU node).
        "defer": False,
        # YOLO weights for vehicle TYPE (COCO: car/truck/bus/motorcycle).
        "model": "yolov8n.pt",
        "min_confidence": 0.35,
        # Estimate the dominant body color from the crop. Cheap (no model), so
        # it works on a bare Pi. Set false to skip color too.
        "color": True,
    },
}


class Section(dict):
    """A dict that also exposes its keys as attributes (read-only convenience)."""

    __getattr__ = dict.__getitem__


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def _wrap(d):
    if isinstance(d, dict):
        return Section({k: _wrap(v) for k, v in d.items()})
    return d


def _load_yaml(p: Path) -> dict:
    if p.exists():
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {}


def load_config(path: str | Path | None) -> Section:
    """Load a YAML config file merged over built-in defaults.

    A missing or None path just returns the defaults, so the app is runnable
    out of the box.

    If a sibling ``*.local.yaml`` overlay exists next to the config file
    (``config.yaml`` -> ``config.local.yaml``), it is deep-merged on TOP of the
    main file. That overlay is untracked (gitignored) and is where real secrets
    and per-deployment overrides live, so the tracked config stays shareable.
    """
    merged = DEFAULTS
    if path:
        p = Path(path)
        merged = _deep_merge(merged, _load_yaml(p))
        local = p.with_name(f"{p.stem}.local{p.suffix}")
        merged = _deep_merge(merged, _load_yaml(local))
    return _wrap(merged)
