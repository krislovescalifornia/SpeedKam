# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Re-validate the live ROI band against a day's clips.

Clips are full-frame video (only DETECTION is cropped), so replaying them
full-frame reveals each car's COMPLETE trajectory -- including any part that rode
outside the enabled band. This is the miss-detection check the crop-on live audit
cannot do: does any real car's tyre-line reach above the band's top edge?

Run periodically on a fresh day's clips to confirm the band still covers every
car with margin:  python tools/roi_check_band.py <clips_dir> [y0]
(y0 = band top-edge fraction, default 0.55 -- the live node value.)
"""
import glob, os, sys
import cv2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from speedkam.detector import MotionDetector
from speedkam.tracker import Tracker

Y0 = float(sys.argv[2]) if len(sys.argv) > 2 else 0.55
BAND = (0.0, Y0, 1.0, 1.0)
X_A_F, X_B_F = 1000, 450             # crossing columns, full-res px
MIN_SAMPLES = 6
DET = {"min_area": 1500, "max_area": 500000, "history": 400, "var_threshold": 40,
       "detect_shadows": False, "morph_kernel": 5, "detect_scale": 0.5, "min_hits": 3}
TRK = {"max_match_distance": 120, "max_missed": 12}
CLIPS = sorted(glob.glob(sys.argv[1] + "/*.mp4"))


def tracks(path, roi):
    """Warm the MOG2 model over the clip (mimic the node's steady state), then
    collect confirmed tracks with a fresh tracker."""
    det = MotionDetector(DET)
    cap = cv2.VideoCapture(path)
    W = int(cap.get(3)) or 1456; H = int(cap.get(4)) or 1088
    while True:
        ok, fr = cap.read()
        if not ok: break
        det.detect(fr, roi=roi)
    cap.release()
    trk = Tracker(TRK, min_hits=DET["min_hits"]); cap = cv2.VideoCapture(path)
    t = 0.0; done = []
    while True:
        ok, fr = cap.read()
        if not ok: break
        d, _ = det.detect(fr, roi=roi); _, fin = trk.update(d, t); done += fin; t += 1 / 30
    cap.release()
    return done + [tr for tr in trk.tracks.values() if tr.confirmed], W, H


def car(trks, W):
    xa, xb = X_A_F * W / 1456, X_B_F * W / 1456
    cars = [tr for tr in trks if len(tr.samples) >= MIN_SAMPLES
            and min(s.ground_px[0] for s in tr.samples) <= xb
            and max(s.ground_px[0] for s in tr.samples) >= xa]
    return max(cars, key=lambda tr: len(tr.samples)) if cars else None


def main():
    env = None; worst = (1.0, None); above = []; n = 0; kept = 0; lost = []
    for p in CLIPS:
        trks, W, H = tracks(p, roi=None)          # full-frame = true trajectory
        c = car(trks, W)
        if not c:
            continue
        n += 1
        ys = [s.ground_px[1] / H for s in c.samples]
        xs = [s.ground_px[0] / W for s in c.samples]
        box = [min(xs), min(ys), max(xs), max(ys)]
        env = box if env is None else [min(env[0], box[0]), min(env[1], box[1]),
                                       max(env[2], box[2]), max(env[3], box[3])]
        if min(ys) < worst[0]:
            worst = (min(ys), os.path.basename(p))
        out = sum(1 for y in ys if y < BAND[1])
        if out:
            above.append((os.path.basename(p), min(ys), out, len(ys)))
        rt, _, _ = tracks(p, roi=BAND)
        (kept := kept + 1) if car(rt, W) else lost.append(os.path.basename(p))

    print(f"clips: {len(CLIPS)}   timeable cars: {n}   band top y0={BAND[1]}")
    if env:
        print(f"full-frame envelope (frac): x[{env[0]:.3f},{env[2]:.3f}] "
              f"y[{env[1]:.3f},{env[3]:.3f}]")
        print(f"farthest tyre-line: y {worst[0]:.3f} ({worst[1]})  "
              f"-> margin {worst[0]-BAND[1]:+.3f} ({(worst[0]-BAND[1])*1088:+.0f} px)")
    print(f"cars with any point ABOVE the band: {len(above)}")
    for name, ymin, out, tot in above[:15]:
        print(f"   {name}: min-y {ymin:.3f}, {out}/{tot} pts above")
    print(f"ROI-applied: {n} full-frame cars -> {kept} still timeable, {len(lost)} lost")
    for name in lost[:15]:
        print(f"   LOST: {name}")


if __name__ == "__main__":
    main()
