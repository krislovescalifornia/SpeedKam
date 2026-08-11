"""Optional, best-effort vehicle attribute recognition.

Given a frame and a vehicle's bounding box, try to fill in:

  * ``vehicle_type``  -- car / truck / bus / motorcycle, from a YOLO detector
                          (COCO classes) when ``recognition.model`` is loadable.
  * ``color``         -- dominant body colour, from a cheap HSV analysis of the
                          crop. No model needed, so it works on a bare Pi.
  * ``make`` / ``model`` / ``year`` -- only when you point
                          ``recognition.make_model_weights`` at a fine-grained
                          YOLOv8-cls model whose class names look like
                          "Toyota Camry 2018". Otherwise they stay ``None``.

Everything here is *optional and defensive*. If ``ultralytics``/``torch`` aren't
installed, or a model fails to load, the recognizer disables itself and every
call returns empty attributes -- the pipeline still counts and times vehicles.
That's the "when available" contract from the spec.
"""
from __future__ import annotations

import re

import cv2
import numpy as np

# COCO class ids we treat as vehicles -> our label.
_COCO_VEHICLES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}

EMPTY = {
    "vehicle_type": None,
    "make": None,
    "model": None,
    "year": None,
    "color": None,
}


def _empty():
    return dict(EMPTY)


# --------------------------------------------------------------------- colour
# Hue ranges (OpenCV hue is 0-179) mapped to human colour names. Saturation and
# value gate the achromatic (white/gray/black) decision first.
_HUE_NAMES = [
    (0, 10, "red"), (10, 22, "orange"), (22, 33, "yellow"),
    (33, 85, "green"), (85, 100, "teal"), (100, 130, "blue"),
    (130, 150, "purple"), (150, 170, "pink"), (170, 180, "red"),
]


def estimate_color(crop) -> str | None:
    """Return a human colour name for a BGR vehicle crop, or None if unclear."""
    if crop is None or crop.size == 0:
        return None
    h, w = crop.shape[:2]
    if h < 6 or w < 6:
        return None
    # Sample the central body of the vehicle, away from road/edges/windows.
    y0, y1 = int(h * 0.30), int(h * 0.75)
    x0, x1 = int(w * 0.20), int(w * 0.80)
    body = crop[y0:y1, x0:x1]
    if body.size == 0:
        body = crop
    body = cv2.resize(body, (32, 32), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(body, cv2.COLOR_BGR2HSV)
    hue = hsv[:, :, 0].reshape(-1).astype(np.int32)
    sat = hsv[:, :, 1].reshape(-1).astype(np.int32)
    val = hsv[:, :, 2].reshape(-1).astype(np.int32)

    # Achromatic pixels: low saturation. Decide black/gray/white by brightness.
    achroma = sat < 45
    if achroma.mean() > 0.6:
        v = float(val.mean())
        if v < 60:
            return "black"
        if v > 190:
            return "white"
        return "gray"

    # Chromatic: vote hue among reasonably saturated, non-dark pixels.
    good = (sat >= 45) & (val >= 40)
    if good.sum() < 8:
        return None
    dom = int(np.median(hue[good]))
    for lo, hi, name in _HUE_NAMES:
        if lo <= dom < hi:
            return name
    return None


class VehicleRecognizer:
    """Wraps optional YOLO models; safe to construct even without ultralytics."""

    def __init__(self, cfg):
        self.cfg = cfg or {}
        self.enabled = bool(self.cfg.get("enabled"))
        self.want_color = bool(self.cfg.get("color", True))
        self.min_conf = float(self.cfg.get("min_confidence", 0.35))
        self._type_model = None
        self._mm_model = None
        self._active = False

        if not self.enabled:
            return
        try:
            from ultralytics import YOLO  # noqa: F401  (import gate only)
        except Exception as exc:  # noqa: BLE001 - any import failure disables it
            print("[SpeedKam] recognition.enabled but ultralytics not available "
                  f"({exc}); attributes disabled (color only if requested).")
            # We can still do colour with pure OpenCV even without ultralytics.
            self._active = self.want_color
            return

        self._YOLO = YOLO
        self._type_model = self._try_load(self.cfg.get("model"), "type")
        self._mm_model = self._try_load(self.cfg.get("make_model_weights"),
                                        "make/model")
        self._active = bool(self._type_model or self._mm_model or self.want_color)
        if self._active:
            bits = []
            if self._type_model:
                bits.append("type")
            if self.want_color:
                bits.append("color")
            if self._mm_model:
                bits.append("make/model/year")
            print(f"[SpeedKam] Recognition on: {', '.join(bits) or 'none'}.")

    @property
    def active(self):
        return self._active

    def _try_load(self, weights, label):
        if not weights:
            return None
        try:
            model = self._YOLO(weights)
            print(f"[SpeedKam] Loaded {label} model: {weights}")
            return model
        except Exception as exc:  # noqa: BLE001 - bad path/download, keep going
            print(f"[SpeedKam] Could not load {label} model '{weights}': {exc}")
            return None

    # ------------------------------------------------------------- recognize
    def recognize(self, frame, bbox) -> dict:
        """Best-effort attributes for the vehicle at ``bbox`` in ``frame``."""
        out = _empty()
        if not self._active or frame is None or bbox is None:
            return out
        crop = _crop(frame, bbox)
        if crop is None:
            return out

        if self.want_color:
            try:
                out["color"] = estimate_color(crop)
            except Exception:  # noqa: BLE001 - colour is best-effort
                pass

        if self._type_model is not None:
            out["vehicle_type"] = self._detect_type(crop)

        if self._mm_model is not None:
            make, model, year = self._classify_make_model(crop)
            out["make"], out["model"], out["year"] = make, model, year

        return out

    def _detect_type(self, crop):
        try:
            res = self._type_model.predict(crop, verbose=False,
                                           conf=self.min_conf)
        except Exception:  # noqa: BLE001
            return None
        best_label, best_conf = None, 0.0
        for r in res:
            boxes = getattr(r, "boxes", None)
            if boxes is None:
                continue
            for cls_id, conf in zip(boxes.cls.tolist(), boxes.conf.tolist()):
                label = _COCO_VEHICLES.get(int(cls_id))
                if label and conf > best_conf:
                    best_label, best_conf = label, conf
        return best_label

    def _classify_make_model(self, crop):
        try:
            res = self._mm_model.predict(crop, verbose=False)
        except Exception:  # noqa: BLE001
            return None, None, None
        label = None
        for r in res:
            probs = getattr(r, "probs", None)
            names = getattr(r, "names", None)
            if probs is None or names is None:
                continue
            top = int(probs.top1)
            if float(probs.top1conf) >= self.min_conf:
                label = names.get(top, None) if isinstance(names, dict) \
                    else names[top]
            break
        return _parse_make_model(label)


def _crop(frame, bbox):
    x, y, w, h = [int(v) for v in bbox]
    H, W = frame.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    return frame[y0:y1, x0:x1]


def _parse_make_model(label):
    """Split a class label like 'Toyota Camry 2018' into make/model/year."""
    if not label:
        return None, None, None
    text = str(label).replace("_", " ").strip()
    year = None
    m = re.search(r"\b(19|20)\d{2}\b", text)
    if m:
        year = m.group(0)
        text = (text[:m.start()] + text[m.end():]).strip()
    parts = text.split()
    make = parts[0] if parts else None
    model = " ".join(parts[1:]) if len(parts) > 1 else None
    return make, model, year
