# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Commit/encode worker (unlock C): the slow clip encode + logging is drained on
a worker thread so a speeder's ~seconds-long save never pauses detection, which
is what let back-to-back cars slip through. These drive `_commit_worker` directly
with a stubbed `_commit_reading` (no camera/recorder/torch) and assert it runs
off the caller's thread, in FIFO order, survives a failing commit, and -- with the
YOLO gate off -- never invokes the vote."""
import queue
import threading

from speedkam.pipeline import SpeedCamera


def _worker_cam(gate_active=False):
    """A bare SpeedCamera with just the commit-worker collaborators wired up."""
    cam = SpeedCamera.__new__(SpeedCamera)
    cam._commit_q = queue.Queue(maxsize=8)
    cam._gate_active = lambda: gate_active
    cam.committed = []            # (job_id, thread_name) in commit order
    cam.voted = []                # track ids the vote ran on

    def fake_commit(job, verdict):
        cam.committed.append((job["id"], threading.current_thread().name, verdict))
    cam._commit_reading = fake_commit

    def fake_vote(track):
        cam.voted.append(track)
        return {"frames": 1, "vehicle_frames": 1}
    cam._yolo_pass = fake_vote
    return cam


def _run(cam, jobs):
    t = threading.Thread(target=cam._commit_worker, name="speedkam-commit")
    t.start()
    for j in jobs:
        cam._commit_q.put(j)
    cam._commit_q.put(None)        # sentinel: drain then exit
    t.join(timeout=5.0)
    assert not t.is_alive(), "commit worker did not exit on the sentinel"


def test_commits_run_on_the_worker_thread_not_the_caller():
    cam = _worker_cam()
    _run(cam, [{"id": 1}])
    assert len(cam.committed) == 1
    # The whole point of unlock C: the commit (and its slow encode) happens on the
    # worker, never on the detection/caller thread.
    assert cam.committed[0][1] == "speedkam-commit"
    assert cam.committed[0][1] != threading.current_thread().name


def test_fifo_order_preserved():
    cam = _worker_cam()
    _run(cam, [{"id": i} for i in range(5)])
    assert [c[0] for c in cam.committed] == [0, 1, 2, 3, 4]


def test_gate_off_skips_the_vote_and_commits_with_no_verdict():
    cam = _worker_cam(gate_active=False)
    _run(cam, [{"id": 1}])
    assert cam.voted == []                      # geometry-only: no YOLO vote
    assert cam.committed[0][2] is None          # verdict passed through as None


def test_gate_on_runs_the_vote_and_passes_the_verdict():
    cam = _worker_cam(gate_active=True)
    _run(cam, [{"id": 1, "track": "trk"}])
    assert cam.voted == ["trk"]
    assert cam.committed[0][2] == {"frames": 1, "vehicle_frames": 1}


class _Trk:
    def __init__(self, tid):
        self.id = tid


def test_a_failing_commit_does_not_kill_the_worker():
    cam = _worker_cam()
    boom = {"id": 0, "track": _Trk(0)}
    ok = {"id": 1, "track": _Trk(1)}

    def flaky_commit(job, verdict):
        if job["id"] == 0:
            raise RuntimeError("encode blew up")
        cam.committed.append((job["id"], threading.current_thread().name, verdict))
    cam._commit_reading = flaky_commit

    _run(cam, [boom, ok])
    # The bad job is swallowed; the next pass still lands.
    assert [c[0] for c in cam.committed] == [1]
