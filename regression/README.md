# Regression clip set — real-world gate validation

A labelled set of **real field clips** used to grade the deterministic geometry/physics
gate (`tools/measure_residual.py --clips`). Seeded with the exact adversaries that
poisoned the data on the Pi 3 (the two blank-road 90+ mph phantoms, the two-kids blob,
the scooter kid, night headlight phantoms, pedestrians, cyclists) plus confirmed real
cars — the discipline described in the gate-of-record plan.

## Privacy — why the clips are NOT in git

The repo is **public**, and these `.mp4`s are real neighbourhood video (children,
pedestrians, neighbours' vehicles). They are deliberately **git-ignored** (`*.mp4` in
`regression/.gitignore`) and are **never** committed. Only the *labels* and *metadata*
live in git. The clips themselves live durably in the off-site fleet backup and are
re-pulled on demand.

## Contents (in git)

- `labels.txt` — `<filename> car|junk`, the ground truth (36 clips: 16 cars, 20 junk).
- `manifest.csv` — per-clip metadata (Pi-3 speed/direction/distance/samples, the Pi-3
  status, and the audited `review_reason` the label is derived from).
- `fetch.sh` — pulls the clips from the off-site backup into this directory.

## Ground-truth provenance

Labels come from the operator-audited `review_reason` on the Pi-3 events, plus three
clips that the Pi-3 mislabelled "ok" but are blank-road phantoms (`id195`, `id283`,
`id305`) — hand-corrected to `junk` here. Source node: CPU serial `000000002d47790c`
(the retired Pi 3). **Caveat:** the Pi-3's own calibration was never backed up, so
world-space gates (straightness/accel/road-region) are only approximate on these clips
— trust the calibration-robust signals (distance, aspect/shape, area_cv, no-track).
The definitive real-car keep-rate check needs a fresh **daylight Pi-4** capture graded
against the Pi-4's own calibration.

## Usage

```bash
# 1. pull the clips (needs the speedkam-web SFTP alias)
bash regression/fetch.sh

# 2. grade on the DEV BOX (never the live node — it shares the Pi's battery/CPU)
PYTHONPATH=src python tools/measure_residual.py \
    --clips regression --calibration <matching-calibration.json> \
    --labels regression/labels.txt --enforce proposed
```
