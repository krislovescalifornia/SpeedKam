# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""SpeedKam -- a camera-based vehicle speed estimator for private roads.

Runs on a Windows test rig with a USB webcam and moves unchanged to a
Raspberry Pi with a CSI/USB camera. Speed is estimated by timing each vehicle
as it crosses between two fixed image columns whose real-world separation is
calibrated per direction from a known-speed pass -- raw pixel x plus real
capture timestamps, no homography.
"""

__version__ = "0.1.0"
