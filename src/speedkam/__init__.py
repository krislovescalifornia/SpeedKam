"""SpeedKam -- a camera-based vehicle speed estimator for private roads.

Runs on a Windows test rig with a USB webcam and moves unchanged to a
Raspberry Pi with a CSI/USB camera. Speed is estimated by mapping the image
to real-world ground coordinates (a homography calibrated from measured
markers on the road) and tracking vehicles across frames using real capture
timestamps.
"""

__version__ = "0.1.0"
