"""Offline ROI validation on today's real clips.

Replays every captured clip through the SAME MotionDetector + Tracker the node
runs, twice: full-frame (baseline, to measure where cars actually travel and to
confirm the pass is countable+timeable) and with the ROI crop enabled (to prove
the car is still detected, still spans both crossing columns x_a/x_b, and still
has >= min_samples inside the band). If ROI keeps every baseline car, enabling it
regresses nothing.
"""
import glob, os, sys
import cv2, numpy as np

sys.path.insert(0, "src")
from speedkam.detector import MotionDetector
from speedkam.tracker import Tracker

W, H = 1456, 1088
X_A, X_B = 1000, 450
MIN_SAMPLES = 6
DET = {"min_area": 1500, "max_area": 500000, "history": 400, "var_threshold": 40,
       "detect_shadows": False, "morph_kernel": 5, "detect_scale": 0.5, "min_hits": 3}
TRK = {"max_match_distance": 120, "max_missed": 12}
CLIPS = sorted(glob.glob(sys.argv[1] + "/*.mp4"))

def car_tracks(path, roi):
    """All confirmed tracks in a clip (finished + active-at-end).

    The node runs MOG2 continuously with a mature background; a fresh clip has no
    model for its first second. So warm the model over the whole clip once
    (detect-only), THEN rewind and collect with a fresh tracker -- this mirrors
    the node's steady state and recovers cars that cross during cold warmup."""
    det = MotionDetector(DET)
    # warm-up pass: build the background model over the clip (no tracking).
    cap = cv2.VideoCapture(path)
    while True:
        ok, fr = cap.read()
        if not ok: break
        det.detect(fr, roi=roi)
    cap.release()
    # collection pass: fresh tracker, warmed model.
    trk = Tracker(TRK, min_hits=DET["min_hits"])
    cap = cv2.VideoCapture(path); t = 0.0; done = []
    while True:
        ok, fr = cap.read()
        if not ok: break
        dets, _ = det.detect(fr, roi=roi)
        _, finished = trk.update(dets, t)
        done += finished
        t += 1 / 30.0
    cap.release()
    done += [tr for tr in trk.tracks.values() if tr.confirmed]
    return done

def spans(tr):
    xs = [s.ground_px[0] for s in tr.samples]
    return len(xs) >= MIN_SAMPLES and min(xs) <= X_B and max(xs) >= X_A

def best_car(tracks):
    """Longest confirmed track that spans both columns = the timeable car."""
    cars = [tr for tr in tracks if spans(tr)]
    return max(cars, key=lambda tr: len(tr.samples)) if cars else None

# ---- pass 1: full-frame baseline -> envelope + which clips are countable
env = None; base_car = {}
for p in CLIPS:
    car = best_car(car_tracks(p, roi=None))
    base_car[p] = car
    if car:
        xs = [s.ground_px[0] / W for s in car.samples]
        ys = [s.ground_px[1] / H for s in car.samples]
        box = [min(xs), min(ys), max(xs), max(ys)]
        env = box if env is None else [min(env[0], box[0]), min(env[1], box[1]),
                                       max(env[2], box[2]), max(env[3], box[3])]

n_base = sum(1 for c in base_car.values() if c)
print(f"clips: {len(CLIPS)}   baseline timeable cars: {n_base}")
print(f"full-frame envelope (frac): x[{env[0]:.3f},{env[2]:.3f}] y[{env[1]:.3f},{env[3]:.3f}]")
print(f"  live 6-car audit was      x[0.010,0.981] y[0.756,0.869]")

# recommended band = envelope padded, clamped, widened to include x_a/x_b
mx, my = 0.06, 0.10
xa, xb = X_A / W, X_B / W
band = [max(0.0, min(env[0] - mx, min(xa, xb) - 0.02)), max(0.0, env[1] - my),
        min(1.0, max(env[2] + mx, max(xa, xb) + 0.02)), min(1.0, env[3] + my)]
px_frac = (band[2] - band[0]) * (band[3] - band[1])
print(f"recommended band (frac): x[{band[0]:.3f},{band[2]:.3f}] y[{band[1]:.3f},{band[3]:.3f}]"
      f"  -> {px_frac*100:.0f}% of pixels")
print(f"  band in px: x[{band[0]*W:.0f},{band[2]*W:.0f}] y[{band[1]*H:.0f},{band[3]*H:.0f}] of {W}x{H}")

# ---- pass 2: ROI enabled -> does every baseline car survive?
lost = []; kept = 0; frames_delta = []
for p in CLIPS:
    if not base_car[p]:
        continue
    roi_car = best_car(car_tracks(p, roi=tuple(band)))
    if roi_car is None:
        lost.append((os.path.basename(p), "no timeable car under ROI"))
    else:
        kept += 1
        frames_delta.append(len(roi_car.samples) - len(base_car[p].samples))

print(f"\nROI regression test (band applied to detection):")
print(f"  baseline cars: {n_base}   still detected+timeable under ROI: {kept}   LOST: {len(lost)}")
if lost:
    for name, why in lost:
        print(f"    !! {name}: {why}")
else:
    print("  >> zero cars lost -- ROI keeps every car the full frame counted today.")
if frames_delta:
    fd = np.array(frames_delta)
    print(f"  samples/car under ROI vs full: mean {fd.mean():+.1f} "
          f"(min {fd.min():+d}, max {fd.max():+d}) -- more/equal = same-or-better timing")

# convergence: does the envelope stop growing? first-half vs all
half = [c for c in list(base_car.values())[:len(CLIPS)//2] if c]
if half:
    e2 = None
    for c in half:
        xs = [s.ground_px[0]/W for s in c.samples]; ys=[s.ground_px[1]/H for s in c.samples]
        b=[min(xs),min(ys),max(xs),max(ys)]
        e2=b if e2 is None else [min(e2[0],b[0]),min(e2[1],b[1]),max(e2[2],b[2]),max(e2[3],b[3])]
    print(f"\nconvergence (first {len(half)} cars vs all {n_base}):")
    print(f"  y-band first-half [{e2[1]:.3f},{e2[3]:.3f}] vs all [{env[1]:.3f},{env[3]:.3f}]")
