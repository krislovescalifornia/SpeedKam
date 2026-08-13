#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deferred (offloaded) vehicle recognition.

Run this on a beefier machine (desktop / GPU box) against images the camera
already saved. The Raspberry Pi captures video, measures speed, estimates color
and writes CSV rows with type/make/model LEFT BLANK (recognition.defer: true).
This worker reads those rows, runs YOLO on the saved snapshot (or a clip frame),
and fills the attributes back in -- both in the local events.csv and, for passes
that were mirrored off-site, on the backup receiver.

It is idempotent: rows that already have a vehicle_type/make are skipped, so you
can run it repeatedly (cron, or --watch) and it only does new work.

    # one pass over everything pending, pointed at the Pi's captures/ (mounted
    # or rsync'd locally) via a config that names the same output_dir + backup:
    python tools/recognize_worker.py --config config.yaml

    # keep watching for new pending rows every 30s:
    python tools/recognize_worker.py --config config.yaml --watch 30

    # add a fine-grained make/model classifier:
    python tools/recognize_worker.py --make-model-weights stanford_cars.pt
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from speedkam.config import load_config  # noqa: E402
from speedkam.recognition import VehicleRecognizer  # noqa: E402
from speedkam.sync import SyncManager  # noqa: E402

# Columns the worker is allowed to fill locally. color is only filled if the Pi
# left it blank (the Pi normally sets color inline); type/make/model/year are
# the point of the offload.
FILL_COLS = ("vehicle_type", "make", "model", "year", "color")
# Columns pushed to the off-site mirror. NOT color -- the Pi already includes
# color in the original upload meta, so the remote row has it from the start;
# only the deferred (YOLO) columns need writing back.
REMOTE_COLS = ("vehicle_type", "make", "model", "year")

# Two on-disk ledgers (one id per line), kept next to the media so repeated
# --watch passes don't redo settled work:
#   .recognized      -- events we've already run YOLO on (even if it found
#                       nothing), so undetectable snapshots aren't re-run forever.
#   .enriched_remote -- events whose attributes the mirror has CONFIRMED storing,
#                       so a push that missed (row not off-site yet) is retried
#                       until it lands, but a confirmed one is never re-sent.
_RECOG_LEDGER = ".recognized"
_REMOTE_LEDGER = ".enriched_remote"


def _needs_work(row, done) -> bool:
    """A row is pending if it has no type AND no make yet, and we haven't
    already attempted recognition on it (its id isn't in the `done` set)."""
    if (row.get("vehicle_type") or "").strip() or (row.get("make") or "").strip():
        return False
    eid = _event_id(row)
    return eid is None or eid not in done


def _load_frame(captures_dir: Path, row):
    """Best available image for a row: the snapshot if present, else a frame
    from the middle of the clip. Returns a BGR frame or None."""
    snap = (row.get("snapshot") or "").strip()
    if snap:
        p = captures_dir / snap
        if p.exists():
            img = cv2.imread(str(p))
            if img is not None:
                return img
    clip = (row.get("clip") or "").strip()
    if clip:
        p = captures_dir / clip
        if p.exists():
            cap = cv2.VideoCapture(str(p))
            try:
                n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                if n > 1:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, n // 2)
                ok, frame = cap.read()
                if ok and frame is not None:
                    return frame
            finally:
                cap.release()
    return None


def _event_id(row) -> str | None:
    """Stable id the off-site receiver matches on: the clip (or snapshot) stem."""
    for key in ("clip", "snapshot"):
        name = (row.get(key) or "").strip()
        if name:
            return Path(name).stem
    return None


def _load_ledger(path: Path) -> set:
    ids = set()
    try:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s:
                    ids.add(s)
    except OSError:
        pass
    return ids


def _append_ledger(path: Path, eid: str):
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(eid + "\n")
    except OSError:
        pass  # best-effort; the in-memory set still guards this run


def run_once(cfg, recognizer, sync, limit=0, remote=True, force=False) -> int:
    csv_path = Path(cfg["logging"]["csv_file"])
    captures_dir = Path(cfg["recording"]["output_dir"])
    if not csv_path.exists():
        print(f"No events log at {csv_path}; nothing to recognize.")
        return 0

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    # --- local recognition: fill blank attributes from saved images ----------
    recog_ledger = captures_dir / _RECOG_LEDGER
    done = set() if force else _load_ledger(recog_ledger)
    pending = [r for r in rows if _needs_work(r, done)]
    if limit > 0:
        pending = pending[:limit]

    enriched = 0
    if pending:
        print(f"[worker] {len(pending)} pending row(s) to recognize.")
        for row in pending:
            frame = _load_frame(captures_dir, row)
            if frame is None:
                continue  # image not on disk yet -> leave pending, retry later
            eid = _event_id(row)
            attrs = recognizer.recognize_full(frame)
            # Mark attempted so an undetectable image isn't re-run every pass.
            if eid:
                _append_ledger(recog_ledger, eid)
            changed = {}
            for col in FILL_COLS:
                val = attrs.get(col)
                if not val or (row.get(col) or "").strip():
                    continue  # skip blanks and don't clobber Pi-written values
                row[col] = val
                changed[col] = val
            if not changed:
                continue
            enriched += 1
            label = " ".join(str(changed[c]) for c in FILL_COLS if c in changed)
            print(f"[worker]   {eid or row.get('track_id')}: {label}")
        if enriched:
            _rewrite_csv(csv_path, fieldnames, rows)
            print(f"[worker] Filled {enriched} row(s) in {csv_path}.")
        else:
            print(f"[worker] {len(pending)} attempted, none recognizable "
                  "(no vehicle detected in the saved image).")
    else:
        print("[worker] Nothing pending to recognize.")

    # --- off-site write-back: push deferred attributes to the mirror ---------
    if remote and sync is not None:
        _sync_remote(cfg, sync, captures_dir, rows, force)

    return enriched


def _sync_remote(cfg, sync, captures_dir, rows, force=False):
    """Push filled attributes to the off-site mirror for rows that are actually
    mirrored there, retrying any that haven't landed yet."""
    mirror_all = bool(cfg.get("backup", {}).get("mirror_all"))
    ledger_path = captures_dir / _REMOTE_LEDGER
    done = set() if force else _load_ledger(ledger_path)
    pushed = misses = 0
    for row in rows:
        eid = _event_id(row)
        if not eid or eid in done:
            continue
        attrs = {c: row.get(c) for c in REMOTE_COLS if (row.get(c) or "").strip()}
        if not attrs:
            continue  # nothing recognized yet -> nothing to write back
        # Only rows that exist off-site: captured passes (have a clip) always;
        # snapshot-only passes only when the node is full-mirroring.
        has_clip = bool((row.get("clip") or "").strip())
        has_snap = bool((row.get("snapshot") or "").strip())
        if not (has_clip or (has_snap and mirror_all)):
            continue
        if sync.enrich_remote(eid, attrs) > 0:
            done.add(eid)
            _append_ledger(ledger_path, eid)
            pushed += 1
        else:
            misses += 1  # not off-site yet (or failed) -> retry next pass
    if pushed:
        print(f"[worker] Pushed attributes for {pushed} event(s) to {sync.host}.")
    if misses:
        print(f"[worker] {misses} event(s) not yet on {sync.host}; will retry "
              f"(last error: {sync.last_error or 'row not present yet'}).")


def _rewrite_csv(csv_path: Path, fieldnames, rows):
    """Atomically rewrite the CSV, preserving header + all columns."""
    tmp = csv_path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in fieldnames})
    tmp.replace(csv_path)


def main():
    ap = argparse.ArgumentParser(description="Deferred vehicle recognition worker")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--weights", default=None,
                    help="YOLO type weights (default: recognition.model or yolov8n.pt)")
    ap.add_argument("--make-model-weights", default=None,
                    help="optional fine-grained make/model YOLOv8-cls weights")
    ap.add_argument("--watch", type=float, default=0,
                    help="seconds between passes (0 = run once and exit)")
    ap.add_argument("--limit", type=int, default=0,
                    help="max rows to process per pass (0 = all)")
    ap.add_argument("--no-remote", action="store_true",
                    help="update only the local CSV; don't touch the backup host")
    ap.add_argument("--force", action="store_true",
                    help="ignore the .recognized/.enriched_remote ledgers and "
                         "re-process every row (e.g. after swapping in a better "
                         "model)")
    args = ap.parse_args()

    cfg = load_config(args.config)

    # Force a FULL recognizer here regardless of the node's defer setting: this
    # machine is the one that actually runs the heavy models.
    rec_cfg = dict(cfg.get("recognition", {}))
    rec_cfg["enabled"] = True
    rec_cfg["defer"] = False
    rec_cfg["model"] = args.weights or rec_cfg.get("model") or "yolov8n.pt"
    if args.make_model_weights is not None:
        rec_cfg["make_model_weights"] = args.make_model_weights
    recognizer = VehicleRecognizer(rec_cfg)
    if not recognizer.active or recognizer._type_model is None:
        print("[worker] No YOLO type model loaded -- install ultralytics/torch "
              "and pass --weights. Nothing to do.")
        return

    sync = None
    if not args.no_remote:
        backup = cfg.get("backup", {})
        if backup.get("enabled") and backup.get("url") and backup.get("secret"):
            sync = SyncManager(backup, cfg["recording"]["output_dir"],
                               cfg["logging"]["csv_file"])
        else:
            print("[worker] backup not configured -> local-only enrichment.")

    if args.watch > 0:
        print(f"[worker] Watching for pending rows every {args.watch:g}s. "
              "Ctrl+C to stop.")
        try:
            while True:
                run_once(cfg, recognizer, sync, args.limit,
                         remote=not args.no_remote, force=args.force)
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\n[worker] Stopped.")
    else:
        run_once(cfg, recognizer, sync, args.limit,
                 remote=not args.no_remote, force=args.force)


if __name__ == "__main__":
    main()
