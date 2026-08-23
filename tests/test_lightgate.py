# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Low-light gate: brightness measurement and the day/night state machine that
pauses detection in the dark (killing phantom night readings) and auto-resumes
at dawn, with hysteresis + dwell so dusk/dawn flicker can't flap it."""
import numpy as np

from speedkam.pipeline import SpeedCamera


def _cam(**lg):
    base = {"enabled": True, "sleep_below": 40, "wake_above": 60,
            "dwell_seconds": 30.0}
    base.update(lg)
    cam = SpeedCamera.__new__(SpeedCamera)
    cam._lg = base
    cam.paused_low_light = False
    cam._lg_since = None
    cam.scene_brightness = None
    return cam


# --------------------------------------------------------- brightness measure
def test_measure_brightness_grayscale():
    img = np.full((48, 64), 90, dtype=np.uint8)
    assert SpeedCamera._measure_brightness(img) == 90.0


def test_measure_brightness_color_averages_channels():
    img = np.zeros((10, 10, 3), dtype=np.uint8)
    img[:, :, 0] = 30   # B
    img[:, :, 1] = 60   # G
    img[:, :, 2] = 90   # R
    assert SpeedCamera._measure_brightness(img) == 60.0


def test_measure_brightness_none():
    assert SpeedCamera._measure_brightness(None) is None


# ------------------------------------------------------------- gate: sleeping
def test_sleeps_only_after_dwell():
    cam = _cam()
    # Dark, but dwell hasn't elapsed yet -> still awake.
    assert cam._update_light_gate(0.0, 20) is False
    assert cam._update_light_gate(29.0, 20) is False
    # Held dark past dwell_seconds -> pause.
    assert cam._update_light_gate(30.0, 20) is True
    assert cam.paused_low_light is True


def test_brief_dark_dip_does_not_sleep():
    cam = _cam()
    assert cam._update_light_gate(0.0, 20) is False    # timer starts
    # Brightness pops back up before dwell -> timer resets, stays awake.
    assert cam._update_light_gate(10.0, 95) is False
    assert cam._lg_since is None
    # Another dip needs the full dwell again from here.
    assert cam._update_light_gate(11.0, 20) is False
    assert cam._update_light_gate(40.0, 20) is False   # only 29s into this dip
    assert cam._update_light_gate(41.0, 20) is True


# --------------------------------------------------------------- gate: waking
def test_dead_band_keeps_it_asleep():
    cam = _cam()
    cam.paused_low_light = True
    # 50 is between sleep_below(40) and wake_above(60): not bright enough to wake.
    assert cam._update_light_gate(0.0, 50) is True
    assert cam._update_light_gate(100.0, 50) is True   # never wakes on 50
    assert cam._lg_since is None


def test_wakes_only_after_dwell_above_wake():
    cam = _cam()
    cam.paused_low_light = True
    assert cam._update_light_gate(0.0, 95) is True     # dawn timer starts
    assert cam._update_light_gate(29.0, 95) is True
    assert cam._update_light_gate(30.0, 95) is False   # resumed
    assert cam.paused_low_light is False


# --------------------------------------------------------------- gate: off
def test_disabled_never_pauses():
    cam = _cam(enabled=False)
    for t in range(0, 200, 10):
        assert cam._update_light_gate(float(t), 0) is False
    assert cam.paused_low_light is False


def test_none_brightness_never_pauses():
    cam = _cam()
    assert cam._update_light_gate(0.0, None) is False
    assert cam._update_light_gate(1000.0, None) is False
