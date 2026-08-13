# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Minimal multi-object tracker (greedy nearest-neighbour association).

Associates detections across frames by proximity of their ground point (in
pixels), assigns stable integer IDs, and accumulates a per-track history of
samples used later for speed estimation. Good enough for sparse traffic on a
private road; swap for SORT/DeepSORT if you ever need crowded scenes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Sample:
    t: float                 # monotonic timestamp (s)
    ground_px: tuple         # (x, y) pixels
    world: tuple | None      # (X, Y) meters, or None if uncalibrated
    bbox: tuple              # (x, y, w, h)


@dataclass
class Track:
    id: int
    samples: list = field(default_factory=list)
    hits: int = 0
    missed: int = 0
    confirmed: bool = False

    @property
    def last_ground(self):
        return self.samples[-1].ground_px

    @property
    def last_bbox(self):
        return self.samples[-1].bbox


class Tracker:
    def __init__(self, cfg, min_hits):
        self.max_dist = cfg["max_match_distance"]
        self.max_missed = cfg["max_missed"]
        self.min_hits = min_hits
        self._next_id = 1
        self.tracks: dict[int, Track] = {}

    def update(self, detections, world_points, t):
        """Advance the tracker one frame.

        detections   -- list[Detection]
        world_points -- list[(X,Y) | None] aligned with detections
        Returns (active_tracks, finished_tracks). Finished tracks have left the
        scene and are ready for speed finalisation.
        """
        unmatched = set(range(len(detections)))

        # Greedy match existing tracks to nearest detection within threshold.
        for track in self.tracks.values():
            best_i, best_d = None, self.max_dist
            tx, ty = track.last_ground
            for i in unmatched:
                dx = detections[i].ground_point[0] - tx
                dy = detections[i].ground_point[1] - ty
                d = math.hypot(dx, dy)
                if d < best_d:
                    best_d, best_i = d, i
            if best_i is not None:
                det = detections[best_i]
                track.samples.append(
                    Sample(t, det.ground_point, world_points[best_i], det.bbox)
                )
                track.hits += 1
                track.missed = 0
                if track.hits >= self.min_hits:
                    track.confirmed = True
                unmatched.discard(best_i)
            else:
                track.missed += 1

        # Spawn new tracks for leftover detections.
        for i in unmatched:
            det = detections[i]
            tid = self._next_id
            self._next_id += 1
            self.tracks[tid] = Track(
                id=tid,
                samples=[Sample(t, det.ground_point, world_points[i], det.bbox)],
                hits=1,
            )

        # Retire stale tracks.
        finished = []
        for tid in list(self.tracks):
            tr = self.tracks[tid]
            if tr.missed > self.max_missed:
                if tr.confirmed:
                    finished.append(tr)
                del self.tracks[tid]

        active = [t for t in self.tracks.values() if t.confirmed]
        return active, finished
