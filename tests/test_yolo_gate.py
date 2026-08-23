# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""YOLO gate (Phase 14): the pass-level car-vs-not vote + decision policy that
promote the classifier to the gate of record. Stubs the recognizer with canned
per-frame verdicts (no torch needed) and drives the pure vote/gate helpers, per
the execution plan's test task: all-vehicle -> keep, all-person -> reject,
zero-detection -> reject (the phantom case geometry can't catch), low-confidence
-> fallback."""
from speedkam.pipeline import SpeedCamera
from speedkam.recognition import _resolve_nuisances, _COCO_NUISANCES

# Canned per-frame classify() results.
VEH = {"vehicle_label": "car", "vehicle_conf": 0.94,
       "nuisance_label": None, "nuisance_conf": 0.0}
TRUCK = {"vehicle_label": "truck", "vehicle_conf": 0.90,
         "nuisance_label": None, "nuisance_conf": 0.0}
PERSON = {"vehicle_label": None, "vehicle_conf": 0.0,
          "nuisance_label": "person", "nuisance_conf": 0.82}
EMPTY = {"vehicle_label": None, "vehicle_conf": 0.0,
         "nuisance_label": None, "nuisance_conf": 0.0}


class FakeRecog:
    """Returns queued classify() verdicts in order (cycling if exhausted)."""

    def __init__(self, results, min_vehicle_frames=2, fallback="geometry",
                 min_reject_brightness=60, vote_frames=8):
        self._results = list(results)
        self._i = 0
        self.min_vehicle_frames = min_vehicle_frames
        self.fallback = fallback
        self.min_reject_brightness = min_reject_brightness
        self.vote_frames = vote_frames

    def classify(self, crop):
        r = self._results[self._i % len(self._results)]
        self._i += 1
        return r


def _cam(recog):
    cam = SpeedCamera.__new__(SpeedCamera)
    cam.recognizer = recog
    return cam


def _vote(cam, results, n=8):
    """Run the vote over n crops (crop content is irrelevant -- classify is
    stubbed), returning the verdict dict."""
    return cam._yolo_vote(["crop"] * n)


# ------------------------------------------------------------------ vote tally
def test_vote_counts_vehicle_frames():
    cam = _cam(FakeRecog([VEH]))
    v = _vote(cam, [VEH], n=8)
    assert v["frames"] == 8
    assert v["vehicle_frames"] == 8
    assert v["nuisance_frames"] == 0
    assert v["best_vehicle_label"] == "car"
    assert round(v["best_vehicle_conf"], 2) == 0.94


def test_vote_counts_nuisance_frames():
    cam = _cam(FakeRecog([PERSON]))
    v = _vote(cam, [PERSON], n=6)
    assert v["frames"] == 6
    assert v["vehicle_frames"] == 0
    assert v["nuisance_frames"] == 6
    assert v["best_nuisance_label"] == "person"


def test_vote_best_vehicle_label_tracks_highest_conf():
    # A truck (0.90) then cars (0.94): best label follows the highest conf.
    cam = _cam(FakeRecog([TRUCK, VEH, VEH]))
    v = _vote(cam, None, n=3)
    assert v["vehicle_frames"] == 3
    assert v["best_vehicle_label"] == "car"


# ------------------------------------------------------------------- gate policy
def test_all_vehicle_keeps():
    cam = _cam(FakeRecog([VEH]))
    v = _vote(cam, [VEH], n=8)
    decision, info = cam._yolo_gate(v, brightness=120)
    assert decision == "keep"
    assert info == "car"


def test_all_person_rejects():
    cam = _cam(FakeRecog([PERSON]))
    v = _vote(cam, [PERSON], n=8)
    decision, reason = cam._yolo_gate(v, brightness=120)
    assert decision == "reject"
    assert "person" in reason and "not a car" in reason


def test_zero_detection_rejects_as_phantom():
    # The exact failure geometry could never catch: a plausible-looking but
    # empty road. Zero vehicle frames in good light -> hard reject.
    cam = _cam(FakeRecog([EMPTY]))
    v = _vote(cam, [EMPTY], n=8)
    decision, reason = cam._yolo_gate(v, brightness=120)
    assert decision == "reject"
    assert "no vehicle detected" in reason and "phantom" in reason


def test_low_confidence_falls_back_to_geometry():
    # 1 vehicle frame out of 8, below min_vehicle_frames=2 -> defer to geometry
    # (default policy): don't drop a real car the model was merely unsure about.
    cam = _cam(FakeRecog([VEH] + [EMPTY] * 7))
    v = _vote(cam, None, n=8)
    assert v["vehicle_frames"] == 1
    decision, info = cam._yolo_gate(v, brightness=120)
    assert decision == "fallback"
    assert info is None


def test_low_confidence_reject_policy():
    cam = _cam(FakeRecog([VEH] + [EMPTY] * 7, fallback="reject"))
    v = _vote(cam, None, n=8)
    decision, reason = cam._yolo_gate(v, brightness=120)
    assert decision == "reject"
    assert "1/8" in reason


def test_low_confidence_keep_policy():
    cam = _cam(FakeRecog([VEH] + [EMPTY] * 7, fallback="keep"))
    v = _vote(cam, None, n=8)
    decision, info = cam._yolo_gate(v, brightness=120)
    assert decision == "keep"
    assert info == "car"


def test_dark_scene_defers_instead_of_rejecting():
    # Zero vehicles but too dark to trust YOLO -> geometry decides, not a reject.
    cam = _cam(FakeRecog([EMPTY]))
    v = _vote(cam, [EMPTY], n=8)
    decision, info = cam._yolo_gate(v, brightness=15)
    assert decision == "fallback"
    assert info is None


def test_dark_guard_disabled_when_zero_floor():
    cam = _cam(FakeRecog([EMPTY], min_reject_brightness=0))
    v = _vote(cam, [EMPTY], n=8)
    decision, _ = cam._yolo_gate(v, brightness=5)
    assert decision == "reject"  # no brightness floor -> still a hard reject


def test_no_frames_falls_back():
    cam = _cam(FakeRecog([VEH]))
    assert cam._yolo_gate(None, brightness=120) == ("fallback", None)
    assert cam._yolo_gate({"frames": 0}, brightness=120) == ("fallback", None)


def test_exactly_threshold_keeps():
    cam = _cam(FakeRecog([VEH, VEH] + [EMPTY] * 6, min_vehicle_frames=2))
    v = _vote(cam, None, n=8)
    assert v["vehicle_frames"] == 2
    decision, info = cam._yolo_gate(v, brightness=120)
    assert decision == "keep"


# --------------------------------------------------------------- nuisance config
def test_resolve_nuisances_default():
    assert _resolve_nuisances(None) == _COCO_NUISANCES
    assert _resolve_nuisances([]) == _COCO_NUISANCES


def test_resolve_nuisances_by_name_and_id():
    got = _resolve_nuisances(["person", 1])
    assert got == {0: "person", 1: "bicycle"}


def test_resolve_nuisances_ignores_junk_and_falls_back():
    # An unknown name resolves to nothing -> default set (never empty).
    assert _resolve_nuisances(["not_a_class"]) == _COCO_NUISANCES
