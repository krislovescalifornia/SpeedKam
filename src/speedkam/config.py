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
        # Optional lens undistortion applied to every frame before detection and
        # calibration. Off by default. dist_coeffs is OpenCV order [k1,k2,p1,p2,k3];
        # intrinsics are derived from fov_deg + frame size unless fx/fy/cx/cy given.
        "undistort": {
            "enabled": False,
            "fov_deg": 70.0,
            "fx": None, "fy": None, "cx": None, "cy": None,
            "dist_coeffs": [0.0, 0.0, 0.0, 0.0, 0.0],
            "alpha": 0.0,
        },
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
        "calibration_file": "calibration.json",
        "min_track_distance_m": 3.0,
        "min_samples": 6,
        "min_speed_kmh": 3,
        "max_speed_kmh": 200,
        # False-positive auto-reject (dashboard-tunable, persisted per node):
        #   max_track_distance_m -- a reading whose vehicle "traveled" farther than
        #     this across the scene is a phantom track (noise/vegetation stitched
        #     into a path) and is rejected. Real passes here cover only a few metres
        #     through the measure band; 45 m is a generous ceiling.
        #   min_vehicle_span_m -- the object's real-world ground footprint; below
        #     this it's sub-car-sized (a cyclist or pedestrian) and rejected. A car
        #     is >~1.3 m; a bike+rider <~0.8 m. 0 disables either gate.
        "max_track_distance_m": 45.0,
        "min_vehicle_span_m": 1.0,
        # Car-shape gate: minimum bbox aspect ratio (width/height). A side-on
        # car's box is wide (~2-3); a walking person's is tall (~0.3-0.5), a
        # side-on cyclist ~square (~0.9). 1.1 keeps cars, rejects people/bikes.
        # Pixel-only (no homography), so a person close to the lens is caught.
        # Lower it for a head-on camera (cars look squarer). 0 disables.
        "min_vehicle_aspect": 1.1,
        # Road-region gate: the fraction of a track's ground points that must lie
        # ON the calibrated road surface (vs the camera-side foreground -- kerb,
        # grass, sidewalk -- or well off to the side) for it to count as a road
        # vehicle. A foreground pedestrian's ground point maps through the road
        # homography to garbage and invents phantom speeds (this is what once
        # logged two kids on the lawn as a 69 mph car). Below this fraction the
        # pass happened mostly off the road and is rejected. Only the NEAR and
        # LATERAL road edges are bounded; the far edge is left open so distant
        # cars still count. Needs a calibration to apply; 0 disables. road_margin_
        # frac widens the road box by this fraction of the frame on those edges.
        "min_on_road_frac": 0.6,
        "road_margin_frac": 0.03,
        # Count each drive-by ONCE: a second confirmed pass in the same direction
        # within this many seconds is treated as a fragmented re-detection of the
        # same vehicle and rejected. 0 disables.
        "dedupe_seconds": 3.0,
        "speed_limit_kmh": 40.2336,   # 25 mph
        "display_units": "mph",
        "direction_positive": "Westbound",
        "direction_negative": "Eastbound",
        # Center-band measurement gate: only time a vehicle while its ground
        # point is inside a band of the frame (fractions of width/height), where
        # the pixels->meters map is trustworthy. The right band depends on how
        # the camera is mounted, so two presets are kept and `orientation`
        # selects the active one (toggle it live on the dashboard):
        #   parallel (side-on): traffic crosses L<->R, edges distorted
        #     horizontally -> a horizontal centre band. Tuned to +2.0% against a
        #     synthetic side-on clip (tools/tune_measure_band.py); re-tune per lens.
        #   head_on (receding): car stays near centre-x and shrinks toward the
        #     vanishing point, where pixels-per-metre collapses -> a vertical
        #     near/mid band dropping the far top. Tuned to -1.4% (vs +32%
        #     full-frame) against a synthetic head-on clip (make_headon_video.py).
        "measure_band": {
            "enabled": True,
            "orientation": "parallel",
            "parallel": {"x_min": 0.3, "x_max": 0.7, "y_min": 0.0, "y_max": 1.0},
            "head_on": {"x_min": 0.0, "x_max": 1.0, "y_min": 0.55, "y_max": 0.95},
        },
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
        # Optional, best-effort vehicle attributes (type/make/model/year/color).
        # Fully optional: with this off, or ultralytics/torch not installed,
        # every pass is still counted and timed -- attributes just stay blank.
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
        # Optional fine-grained make/model/year classifier (a YOLOv8-cls model
        # whose class names look like "Toyota Camry 2018"). Empty = not
        # available -> make/model/year stay blank ("when available").
        "make_model_weights": "",
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
