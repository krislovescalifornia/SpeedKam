"""Colour estimation: a car near foliage must not be mislabelled by the green
backdrop merged into its motion box (regression for the grey Pilot logged
"green" on 2026-08-25)."""
import numpy as np

from speedkam.recognition import estimate_color, _trim_foliage_top

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
