# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Optional, best-effort vehicle attribute recognition.

The gate of record -- is this a real road vehicle, and how fast? -- is
deterministic geometry/motion physics in the pipeline, NOT a neural net. This
module is purely optional enrichment on top of that:

  * ``color``         -- dominant body colour, from a cheap HSV analysis of the
                          crop. No model needed, so it works on a bare Pi. This
                          is the part that runs in normal operation.
  * ``vehicle_type``  -- car / truck / bus / motorcycle, from a YOLO detector
                          (COCO classes) when ``recognition.model`` is loadable.
                          Optional and OFF by default: a heavy, thermally hot
                          dependency the geometry gates don't need. Kept as a
                          dormant break-glass classifier (see the gate below),
                          not the thing holding the line on phantoms.

Make/model/year fine-grained recognition was dropped (low value; a torch weight
and per-backend tuning we're not paying for). The make/model/year fields remain
in the record schema but always stay ``None``.

Everything here is *optional and defensive*. If ``ultralytics``/``torch`` aren't
installed, or a model fails to load, the recognizer disables itself and every
call returns empty attributes -- the pipeline still counts and times vehicles.
"""
from __future__ import annotations

import cv2
import numpy as np

# COCO class ids we treat as vehicles -> our label.
_COCO_VEHICLES = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}

# COCO class ids that are explicitly NOT vehicles -- the nuisances the geometry
# gates provably cannot separate from a car once they're on the road plane (a
# pedestrian/cyclist whose feet are on the calibrated surface). The YOLO gate
# (Phase 14) exists to catch exactly these, plus the zero-detection phantom.
_COCO_NUISANCES = {0: "person", 1: "bicycle", 15: "cat", 16: "dog"}

# Name<->id lookups for resolving a configured nuisance list.
_COCO_NAMES = dict(_COCO_VEHICLES)
_COCO_NAMES.update(_COCO_NUISANCES)
_NAME_TO_ID = {name: cid for cid, name in _COCO_NAMES.items()}


def _resolve_nuisances(spec):
    """Map a config `nuisance_classes` list (COCO ids or names) to {id: name}.
    Empty/unset -> the sensible default (person/bicycle/cat/dog)."""
    if not spec:
        return dict(_COCO_NUISANCES)
    out = {}
    for item in spec:
        if isinstance(item, bool):  # guard: bool is an int subclass
            continue
        if isinstance(item, int):
            out[item] = _COCO_NAMES.get(item, str(item))
        else:
            name = str(item).strip().lower()
            if name in _NAME_TO_ID:
                out[_NAME_TO_ID[name]] = name
    return out or dict(_COCO_NUISANCES)

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

    # Neutral by default. Real cars are overwhelmingly white / silver / gray /
    # black, so the only way to earn a chromatic name is STRONG saturation over a
    # real slice of the body. This is what stops a white van reading "blue":
    # glossy paint reflects the blue sky and dark windows break up the body,
    # leaving a lot of washed-out (sat 45-85) blue-hued pixels. Those are
    # reflections, not paint -- the old code let them win. Genuine coloured paint
    # sits well above sat 90.
    painted = (sat >= 90) & (val >= 50)
    if painted.mean() >= 0.35:
        dom = int(np.median(hue[painted]))
        for lo, hi, name in _HUE_NAMES:
            if lo <= dom < hi:
                return name
        return None

    # Neutral: black / gray / white by the brightness of the PAINT -- the median
    # of the non-dark neutral pixels. Averaging the whole crop lets dark
    # windows/wheels/shadow drag a white body down into "gray"; the median of the
    # neutral pixels ignores that dark trim.
    neutral = ~painted
    if neutral.sum() < 8:
        return None
    v = float(np.median(val[neutral]))
    if v < 55:
        return "black"
    if v > 175:
        return "white"
    return "gray"


class VehicleRecognizer:
    """Wraps optional YOLO models; safe to construct even without ultralytics."""

    def __init__(self, cfg):
        self.cfg = cfg or {}
        self.enabled = bool(self.cfg.get("enabled"))
        self.defer = bool(self.cfg.get("defer"))
        self.want_color = bool(self.cfg.get("color", True))
        self.min_conf = float(self.cfg.get("min_confidence", 0.35))
        # --- YOLO gate (Phase 14) config ------------------------------------
        # The classifier acting as the gate of record for car-vs-not. Parsed
        # even when disabled so the pipeline can read the knobs uniformly.
        gate = self.cfg.get("gate", {}) or {}
        self.gate_enabled = bool(gate.get("enabled", False))
        # The gate's own detection confidence, decoupled from the enrichment
        # min_confidence: at the Pi-4 sweet-spot imgsz 320 the detector
        # hallucinates faint (<=0.55) "vehicles" on empty-road phantoms, so the
        # gate needs a higher bar (~0.60) to separate them from real cars (0.85+)
        # -- validated 7/7 on the labelled regression clips.
        self.gate_min_conf = float(gate.get("min_confidence", self.min_conf)
                                   or self.min_conf)
        self.imgsz = int(gate.get("imgsz", 320) or 320)
        self.vote_frames = int(gate.get("vote_frames", 8) or 8)
        self.min_vehicle_frames = int(gate.get("min_vehicle_frames", 2) or 2)
        self.fallback = str(
            gate.get("low_confidence_fallback", "geometry") or "geometry").lower()
        self.min_reject_brightness = float(gate.get("min_reject_brightness", 60) or 0)
        self.nuisance_ids = _resolve_nuisances(gate.get("nuisance_classes"))
        # True once a real COCO detector is loaded (below) on a non-defer node --
        # the precondition for running the pass-level vote gate.
        self.can_classify = False
        self._type_model = None
        self._active = False

        if not self.enabled:
            return

        # Deferred mode: this node offloads YOLO to a separate machine. Don't
        # import or load torch/ultralytics at all -- just do the cheap color
        # pass inline; a worker fills in type/make/model later from snapshots.
        if self.defer:
            self._active = self.want_color
            if self._active:
                print("[SpeedKam] Recognition DEFERRED: color inline, "
                      "type/make/model offloaded to recognize_worker.")
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
        # The COCO type model recognizes people/bicycles too, so it can also
        # serve as the car-vs-not gate's classifier (see classify()).
        self.can_classify = self._type_model is not None
        self._active = bool(self._type_model or self.want_color)
        if self._active:
            bits = []
            if self._type_model:
                bits.append("type")
            if self.want_color:
                bits.append("color")
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

        return out

    # ---------------------------------------------------------- full-frame
    def recognize_full(self, frame) -> dict:
        """Recognize the dominant vehicle in a WHOLE frame (no bbox needed).

        Used by the deferred worker: it reads a saved snapshot/clip frame that
        has no stored crop coordinates, so we run the type detector over the
        full image, pick the best vehicle box, and derive color from that box.
        Falls back to the whole frame for color when no box.
        """
        out = _empty()
        if not self._active or frame is None:
            return out

        box = None
        if self._type_model is not None:
            out["vehicle_type"], box = self._detect_type(frame, want_box=True)

        crop = _crop(frame, box) if box is not None else frame

        if self.want_color:
            try:
                out["color"] = estimate_color(crop)
            except Exception:  # noqa: BLE001 - color is best-effort
                pass

        return out

    def _detect_type(self, crop, want_box=False):
        """Best vehicle label in ``crop``. With want_box, also return its bbox
        (x, y, w, h) in ``crop`` coordinates so callers can re-crop tightly."""
        none_result = (None, None) if want_box else None
        try:
            res = self._type_model.predict(crop, verbose=False,
                                           conf=self.min_conf)
        except Exception:  # noqa: BLE001
            return none_result
        best_label, best_conf, best_box = None, 0.0, None
        for r in res:
            boxes = getattr(r, "boxes", None)
            if boxes is None:
                continue
            xyxy = boxes.xyxy.tolist() if want_box else [None] * len(boxes.cls)
            for cls_id, conf, xy in zip(boxes.cls.tolist(),
                                        boxes.conf.tolist(), xyxy):
                label = _COCO_VEHICLES.get(int(cls_id))
                if label and conf > best_conf:
                    best_label, best_conf = label, conf
                    if want_box and xy is not None:
                        x0, y0, x1, y1 = xy
                        best_box = (x0, y0, x1 - x0, y1 - y0)
        return (best_label, best_box) if want_box else best_label

    # ------------------------------------------------------------- gate verdict
    def classify(self, crop) -> dict:
        """One YOLO pass over a crop, scoring it for the car-vs-not gate.

        Returns the best VEHICLE detection and the best NUISANCE detection found
        (person/bicycle/animal), each as (label, confidence):

            {"vehicle_label": str|None, "vehicle_conf": float,
             "nuisance_label": str|None, "nuisance_conf": float}

        All None/0.0 when no model is loaded, the crop is empty, or nothing
        relevant clears ``min_confidence``. This is the per-frame primitive the
        pipeline's pass-level vote tallies -- real cars light up vehicle_conf on
        most frames; an empty-road phantom lights up neither on any frame."""
        out = {"vehicle_label": None, "vehicle_conf": 0.0,
               "nuisance_label": None, "nuisance_conf": 0.0}
        if self._type_model is None or crop is None or getattr(crop, "size", 0) == 0:
            return out
        try:
            res = self._type_model.predict(crop, verbose=False,
                                           conf=self.gate_min_conf,
                                           imgsz=self.imgsz)
        except Exception:  # noqa: BLE001 - inference must never break the loop
            return out
        for r in res:
            boxes = getattr(r, "boxes", None)
            if boxes is None:
                continue
            for cls_id, conf in zip(boxes.cls.tolist(), boxes.conf.tolist()):
                cid = int(cls_id)
                conf = float(conf)
                if cid in _COCO_VEHICLES and conf > out["vehicle_conf"]:
                    out["vehicle_label"] = _COCO_VEHICLES[cid]
                    out["vehicle_conf"] = conf
                elif cid in self.nuisance_ids and conf > out["nuisance_conf"]:
                    out["nuisance_label"] = self.nuisance_ids[cid]
                    out["nuisance_conf"] = conf
        return out


def _crop(frame, bbox):
    x, y, w, h = [int(v) for v in bbox]
    H, W = frame.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    return frame[y0:y1, x0:x1]
