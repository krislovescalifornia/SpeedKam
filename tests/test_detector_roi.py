# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Road-band detection ROI: proving it never changes what a full-frame detection
would have reported for a blob inside the band (offset-correct coordinates), that
it is a strict no-op when off, and that the pipeline refuses any band that would
break crossing-time speed. This is the safety net for the fps rollout -- see
docs/roi-rollout-log.md."""
import numpy as np

from speedkam.detector import MotionDetector
from speedkam.pipeline import SpeedCamera
from speedkam.tracker import Sample, Track


def _det():
    return MotionDetector({"min_area": 1000, "max_area": 500000, "history": 50,
                           "var_threshold": 16, "detect_shadows": False,
                           "morph_kernel": 5, "detect_scale": 1.0})


def _frame(w=400, h=300, rect=None, val=255):
    f = np.zeros((h, w, 3), np.uint8)
    if rect:
        x, y, rw, rh = rect
        f[y:y + rh, x:x + rw] = val
    return f


def _run(det, frames, roi=None):
    """Feed frames through one detector; return detections from the last frame."""
    dets = []
    for f in frames:
        dets, _ = det.detect(f, roi=roi)
    return dets


# ------------------------------------------------ offset correctness (the crux)
def test_roi_offset_matches_full_frame():
    """A blob inside the ROI reports the SAME full-res box as a full-frame
    detection -- so tracking and speed are byte-identical whether cropped or not."""
    rect = (150, 120, 80, 50)
    empties = [_frame() for _ in range(20)]
    rectf = _frame(rect=rect)
    full = _run(_det(), empties + [rectf])
    cropped = _run(_det(), empties + [rectf], roi=(0.2, 0.3, 0.95, 0.85))
    assert len(full) == 1 and len(cropped) == 1
    for a, b in zip(full[0].bbox, cropped[0].bbox):
        assert abs(a - b) <= 2      # identical modulo integer-crop rounding
    # ground point (bottom-centre) also matches -- that's what speed reads.
    for a, b in zip(full[0].ground_point, cropped[0].ground_point):
        assert abs(a - b) <= 2


def test_roi_none_and_full_frame_are_noops():
    rect = (150, 120, 80, 50)
    empties = [_frame() for _ in range(20)]
    rectf = _frame(rect=rect)
    a = _run(_det(), empties + [rectf], roi=None)
    b = _run(_det(), empties + [rectf], roi=(0.0, 0.0, 1.0, 1.0))
    assert len(a) == 1 and len(b) == 1
    assert a[0].bbox == b[0].bbox


def test_blob_outside_roi_is_not_detected():
    rect = (300, 40, 70, 60)                 # top-right corner
    empties = [_frame() for _ in range(20)]
    rectf = _frame(rect=rect)
    # sanity: full-frame sees it...
    assert len(_run(_det(), empties + [rectf])) == 1
    # ...but a bottom-left band does not.
    outside = _run(_det(), empties + [rectf], roi=(0.0, 0.5, 0.5, 1.0))
    assert len(outside) == 0


# ------------------------------------------------ pipeline config, fail-safe
def _cam_cfg(roi, xa=1000, xb=450, W=1456, H=1088):
    cfg = {"camera": {"width": W, "height": H},
           "speed": {"x_a": xa, "x_b": xb},
           "detection": {"roi": roi}}
    cam = SpeedCamera.__new__(SpeedCamera)
    cam.cfg = cfg
    return cam, cfg


def test_roi_disabled_is_none():
    cam, cfg = _cam_cfg({"enabled": False, "audit": False})
    det, audit = cam._configure_roi(cfg)
    assert det is None and audit is False


def test_roi_audit_flag_parsed_without_crop():
    cam, cfg = _cam_cfg({"enabled": False, "audit": True})
    det, audit = cam._configure_roi(cfg)
    assert det is None and audit is True     # audit never crops


def test_roi_refused_when_band_excludes_crossing_columns():
    # x[0.0,0.3] -> px[0,437]; excludes x_a=1000 -> speed would break -> refuse.
    cam, cfg = _cam_cfg({"enabled": True, "x0": 0.0, "y0": 0.4, "x1": 0.3, "y1": 0.8})
    det, _ = cam._configure_roi(cfg)
    assert det is None                       # fail-safe to full frame


def test_roi_enabled_valid_band():
    # x[0.2,0.85] -> px[291,1237] contains x_b=450..x_a=1000.
    cam, cfg = _cam_cfg({"enabled": True, "x0": 0.2, "y0": 0.4, "x1": 0.85, "y1": 0.85})
    det, _ = cam._configure_roi(cfg)
    assert det == (0.2, 0.4, 0.85, 0.85)


def test_roi_full_frame_enabled_is_noop():
    cam, cfg = _cam_cfg({"enabled": True, "x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0})
    det, _ = cam._configure_roi(cfg)
    assert det is None


# ------------------------------------------------ audit envelope + coverage
def _audit_cam(roi):
    cam, cfg = _cam_cfg(roi)
    cam._det_roi, cam._roi_audit = cam._configure_roi(cfg)
    cam._roi_env = None
    cam._roi_audit_passes = 0
    cam._roi_audit_covered = 0
    cam._roi_audit_min_cov = 1.0
    cam._write_roi_audit = lambda: None      # no disk in tests
    return cam


def _track(points):
    return Track(id=1, samples=[Sample(t=i * 0.1, ground_px=p, bbox=(0, 0, 1, 1))
                                for i, p in enumerate(points)])


def test_audit_grows_observed_envelope():
    cam = _audit_cam({"enabled": False, "audit": True})
    cam._roi_audit_pass(_track([(728, 600), (900, 650)]))
    assert cam._roi_audit_passes == 1
    e = cam._roi_env
    assert abs(e[0] - 728 / 1456) < 1e-6 and abs(e[2] - 900 / 1456) < 1e-6
    assert abs(e[1] - 600 / 1088) < 1e-6 and abs(e[3] - 650 / 1088) < 1e-6


def test_audit_coverage_flags_points_outside_candidate():
    cam = _audit_cam({"enabled": False, "audit": True,
                      "x0": 0.2, "y0": 0.4, "x1": 0.85, "y1": 0.85})
    # one point inside the band, one well above it (y frac ~0.09 < 0.4).
    cam._roi_audit_pass(_track([(728, 600), (728, 100)]))
    assert cam._roi_audit_covered == 0       # not a fully-covered pass
    assert cam._roi_audit_min_cov == 0.5     # exactly 1 of 2 inside
