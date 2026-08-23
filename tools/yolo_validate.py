#!/usr/bin/env python3
"""Offline YOLO validation harness for SpeedKam recognition (Phase 14).

Grades a directory of event clips against ground truth: does YOLO find a
vehicle, or is the clip a blank-road phantom? This is the harness that proved
(2026-08-23, on the dev box) that YOLOv8n cleanly separates real cars from the
phantoms the Pi-3 geometry gates leaked. Reuse it to grade the on-node
recognizer before trusting any false-positive number. See
docs/pi4-yolo-phase14-execution.md.

Usage:
    python tools/yolo_validate.py <clips_dir> [--conf 0.35] [--imgsz 640] [--maxfr 80]

Clips are .mp4 files. Ground truth is inferred from the filename convention used
by the current pipeline (e.g. 20260823_125927_id195_92mph.mp4). Provide a
labels file (one "<basename> phantom|car" per line) with --labels to override.

Requires: ultralytics, opencv-python(-headless). On a node use the system
python3 (which already has cv2/numpy); install ultralytics into it or a venv.
"""
import argparse
import glob
import os
import sys

VEHICLE = {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}


def load_labels(path):
    labels = {}
    if not path:
        return labels
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, kind = line.partition(" ")
        labels[name.strip()] = kind.strip().lower()
    return labels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clips_dir")
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--maxfr", type=int, default=80)
    ap.add_argument("--weights", default="yolov8n.pt")
    ap.add_argument("--labels", default=None,
                    help="optional '<basename> phantom|car' per line")
    ap.add_argument("--vote", type=int, default=2,
                    help="min vehicle-frames to call a clip a car")
    ap.add_argument("--proof", default=None, help="dir to write annotated frames")
    args = ap.parse_args()

    import cv2
    from ultralytics import YOLO

    labels = load_labels(args.labels)
    model = YOLO(args.weights)
    clips = sorted(glob.glob(os.path.join(args.clips_dir, "*.mp4")))
    if not clips:
        print(f"no .mp4 clips in {args.clips_dir}", file=sys.stderr)
        return 2
    if args.proof:
        os.makedirs(args.proof, exist_ok=True)

    print(f"{'clip':44} {'fr':>4} {'veh':>4} {'best':>5}  verdict / ground-truth")
    print("-" * 92)
    rows = []
    for path in clips:
        cap = cv2.VideoCapture(path)
        fr = veh = 0
        best = 0.0
        best_frame = best_res = None
        while fr < args.maxfr:
            ok, frame = cap.read()
            if not ok:
                break
            fr += 1
            res = model(frame, conf=args.conf, imgsz=args.imgsz, verbose=False)[0]
            here = max((float(b.conf[0]) for b in res.boxes
                        if int(b.cls[0]) in VEHICLE), default=0.0)
            if here > 0:
                veh += 1
                if here > best:
                    best, best_frame, best_res = here, frame.copy(), res
        cap.release()

        name = os.path.basename(path)
        gt = labels.get(name)
        if gt is None:  # infer from filename
            gt = "phantom" if any(t in name for t in ("90mph", "91mph", "92mph",
                 "93mph", "94mph", "95mph")) else "car"
        pred = "car" if veh >= args.vote else "phantom"
        mark = "OK " if pred == gt else "!! "
        print(f"{name:44} {fr:4d} {veh:4d} {best:5.2f}  {mark}pred={pred} gt={gt}")
        rows.append((name, gt, pred, veh, best))

        if args.proof:
            if best_res is not None:
                out = best_res.plot()
                cv2.imwrite(os.path.join(args.proof,
                            name.replace(".mp4", "_DETECTED.jpg")), out)

    ok = sum(1 for _, gt, pred, *_ in rows if gt == pred)
    print(f"\n{ok}/{len(rows)} correct  "
          f"(phantoms rejected + cars kept). Mismatches marked '!!' above.")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
