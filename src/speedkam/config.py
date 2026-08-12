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
        "loop": False,
    },
    "detection": {
        "min_area": 1500,
        "max_area": 500000,
        "history": 400,
        "var_threshold": 40,
        "detect_shadows": True,
        "morph_kernel": 5,
        "min_hits": 3,
    },
    "tracker": {"max_match_distance": 120, "max_missed": 12},
    "speed": {
        "calibration_file": "calibration.json",
        "min_track_distance_m": 3.0,
        "min_samples": 6,
        "min_speed_kmh": 3,
        "max_speed_kmh": 200,
        "speed_limit_kmh": 30,
        "display_units": "mph",
        "direction_positive": "outbound",
        "direction_negative": "inbound",
    },
    "recording": {
        "enabled": True,
        "output_dir": "captures",
        "clip_seconds": 8,
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
    "web": {"host": "0.0.0.0", "port": 8080},
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


def load_config(path: str | Path | None) -> Section:
    """Load a YAML config file merged over built-in defaults.

    A missing or None path just returns the defaults, so the app is runnable
    out of the box.
    """
    user = {}
    if path:
        p = Path(path)
        if p.exists():
            user = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return _wrap(_deep_merge(DEFAULTS, user))
