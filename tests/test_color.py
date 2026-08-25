"""Colour estimation regressions (2026-08-25):
- grey Pilot logged "green" from the tree canopy merged into its motion box;
- fire-engine-red Civic logged "green"/"blue" because red's hue wraps (0 and 180)
  and the old code took the MEDIAN hue, landing at ~90 = cyan.
"""
import numpy as np
import cv2

from speedkam.recognition import (estimate_color, estimate_color_pixels,
                                   changed_pixels, _trim_foliage_top)

GRAY = (128, 128, 128)   # BGR neutral mid-grey
GREEN = (0, 200, 0)      # BGR saturated leaf-green (HSV hue ~60, in [33,85))
BLUE = (200, 0, 0)       # BGR saturated blue


def _block(color, h, w):
    a = np.zeros((h, w, 3), np.uint8)
    a[:] = color
    return a


def _stack(top, bottom):
    return np.vstack([top, bottom])


def test_tight_gray_car_reads_gray():
    assert estimate_color(_block(GRAY, 200, 400)) == "gray"


def test_gray_car_with_green_canopy_above_is_not_green():
    # Motion box swallowed a tall band of tree canopy above the roofline.
    box = _stack(_block(GREEN, 300, 400), _block(GRAY, 200, 400))
    assert estimate_color(box) != "green"
    assert estimate_color(box) == "gray"


def test_blue_paint_is_preserved():
    # The fix must not bleach a genuinely coloured car toward neutral.
    assert estimate_color(_block(BLUE, 200, 400)) == "blue"


def test_trim_removes_leading_green_rows():
    box = _stack(_block(GREEN, 120, 400), _block(GRAY, 200, 400))
    trimmed = _trim_foliage_top(box)
    assert trimmed.shape[0] == 200  # canopy band gone, car kept


def test_trim_leaves_a_clean_car_untouched():
    car = _block(GRAY, 200, 400)
    assert _trim_foliage_top(car).shape[0] == 200


def test_trim_never_eats_more_than_70pct():
    # A box that is entirely green (phantom) is capped, never fully consumed.
    box = _block(GREEN, 200, 400)
    assert _trim_foliage_top(box).shape[0] == int(200 * 0.30)


# --- red hue-wraparound (the "red car logged blue/green" bug) ----------------
RED = (0, 0, 220)      # BGR pure red -> HSV hue 0 (and reflections near 180)


def test_red_pixels_are_red_not_blue():
    # A block of red pixels whose hue straddles both ends of the wheel (2 and
    # 178) must read red -- the median would land at ~90 (cyan/blue).
    lo = np.zeros((200, 200, 3), np.uint8); lo[:] = (40, 40, 220)   # hue ~2
    hsv = cv2.cvtColor(lo, cv2.COLOR_BGR2HSV); hsv[:, :100, 0] = 178  # other end
    both = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    assert estimate_color_pixels(both.reshape(-1, 3)) == "red"


def test_red_car_crop_is_red():
    assert estimate_color(_block(RED, 200, 400)) == "red"


# --- background subtraction isolates the car from a static backdrop ----------
def test_changed_pixels_returns_only_the_car():
    plate = _block(GREEN, 400, 400)                  # static leafy backdrop
    frame = plate.copy()
    frame[250:380, 120:300] = RED                    # a red car drives in
    px = changed_pixels(frame, plate)
    assert px is not None
    assert estimate_color_pixels(px) == "red"        # backdrop dropped out


def test_changed_pixels_none_when_nothing_moved():
    plate = _block(GRAY, 300, 300)
    assert changed_pixels(plate.copy(), plate) is None


# --- blue needs more evidence than real paint (sky reflects blue) ------------
def test_dark_red_paint_beats_neutral_gate():
    # A dark-maroon car is only ~38% saturated; red is real paint, so the lower
    # chroma gate still calls it red rather than washing out to neutral.
    body = np.zeros((100, 100, 3), np.uint8)
    body[:] = (30, 30, 30)                       # dark neutral majority
    body[:, :40] = (40, 40, 150)                 # ~40% dark-red paint
    assert estimate_color_pixels(body.reshape(-1, 3)) == "red"


def test_minority_blue_reflection_stays_neutral():
    # A grey car with big sky-reflecting windows: ~35% saturated blue must NOT
    # win -- blue needs the higher gate, so it reads gray.
    body = np.zeros((100, 100, 3), np.uint8)
    body[:] = (130, 130, 130)                    # grey body majority
    body[:, :35] = (200, 60, 0)                  # ~35% saturated blue (glass)
    assert estimate_color_pixels(body.reshape(-1, 3)) == "gray"
