#!/usr/bin/env python3
"""Interactive ground-plane calibration for SpeedKam.

You do this ONCE, with the camera in its final mounted position. It builds the
image->meters homography that makes speed measurement possible.

HOW TO MEASURE (do this before running the tool)
------------------------------------------------
Pick 4+ features you can see on the road surface AND tape-measure between:
lane markings, cracks, cones, chalk crosses, driveway corners, fence posts at
the road edge. Choose a flat stretch and spread the points out -- ideally a
rectangle covering the zone where cars will be timed. Define an origin (0,0)
and two axes (e.g. X = along the road in the driving direction, Y = across it).
Record each point's (X, Y) in METERS.

Example (a 20 m x 4 m rectangle along the road):
    A near-left   = (0,  0)
    B far-left    = (20, 0)
    C far-right   = (20, 4)
    D near-right  = (0,  4)

RUNNING
-------
    python tools/calibrate.py                 # grab from the configured camera
    python tools/calibrate.py --image road.jpg  # calibrate from a saved photo

Controls (in the window):
    SPACE  freeze the current live frame to click on
    click  add an image point (in order matching your measured list)
    u      undo last point
    r      un-freeze / retry
    ENTER  finish clicking -> type the world coords in the terminal
    q      quit without saving
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from speedkam.calibration import Calibration  # noqa: E402
from speedkam.capture import Camera  # noqa: E402
from speedkam.config import load_config  # noqa: E402

points = []


def on_mouse(event, x, y, flags, param):
    if event == cv2.EVENT_LBUTTONDOWN:
        points.append((float(x), float(y)))


def draw(img):
    view = img.copy()
    for i, (x, y) in enumerate(points):
        cv2.circle(view, (int(x), int(y)), 6, (0, 0, 255), -1)
        cv2.putText(view, str(i + 1), (int(x) + 8, int(y) - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
    msg = "SPACE=freeze  click=add  u=undo  r=retry  ENTER=done  q=quit"
    cv2.rectangle(view, (0, 0), (view.shape[1], 30), (0, 0, 0), -1)
    cv2.putText(view, f"{msg}   points:{len(points)}", (8, 21),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
    return view


def collect_image_points(cfg, image_path):
    global points
    frozen = None
    if image_path:
        frozen = cv2.imread(image_path)
        if frozen is None:
            raise SystemExit(f"Could not read image {image_path!r}")
        cam = None
    else:
        cam = Camera(cfg["camera"])

    cv2.namedWindow("calibrate")
    cv2.setMouseCallback("calibrate", on_mouse)
    try:
        while True:
            if frozen is None:
                _, live = cam.read()
                if live is None:
                    raise SystemExit("Camera read failed.")
                shown = draw(live)
            else:
                shown = draw(frozen)
            cv2.imshow("calibrate", shown)
            key = cv2.waitKey(20) & 0xFF
            if key == ord(" ") and frozen is None:
                _, frozen = cam.read()
            elif key == ord("r"):
                frozen = None
                points = []
            elif key == ord("u") and points:
                points.pop()
            elif key in (13, 10):  # ENTER
                if frozen is None:
                    frozen = live.copy()
                if len(points) >= 4:
                    break
                print("Need at least 4 points.")
            elif key == ord("q"):
                raise SystemExit("Cancelled.")
    finally:
        if cam is not None:
            cam.release()
        cv2.destroyAllWindows()
    return frozen


def main():
    ap = argparse.ArgumentParser(description="SpeedKam calibration tool")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--image", help="calibrate from a saved photo instead of live cam")
    args = ap.parse_args()

    cfg = load_config(args.config)
    out_path = cfg["speed"]["calibration_file"]

    frozen = collect_image_points(cfg, args.image)

    print(f"\nEnter the real-world coordinates (meters) for each of the "
          f"{len(points)} clicked points, in the order you clicked them.")
    world = []
    for i, (px, py) in enumerate(points):
        while True:
            raw = input(f"  Point {i + 1} at pixel ({px:.0f},{py:.0f})  ->  X Y : ")
            try:
                x_str, y_str = raw.replace(",", " ").split()
                world.append((float(x_str), float(y_str)))
                break
            except ValueError:
                print("    Please enter two numbers, e.g.:  20 4")

    calib = Calibration(points, world, meta={"units": "meters"})
    calib.save(out_path)
    err = calib.reprojection_error()
    print(f"\nSaved calibration -> {out_path}")
    print(f"Mean reprojection error: {err:.3f} m "
          f"({'good' if err < 0.5 else 'consider re-measuring'}).")
    # Save an annotated reference image next to the calibration.
    ref = draw(frozen)
    ref_path = Path(out_path).with_suffix(".ref.jpg")
    cv2.imwrite(str(ref_path), ref)
    print(f"Reference image -> {ref_path}")


if __name__ == "__main__":
    main()
