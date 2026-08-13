# Offloading recognition (defer the YOLO to another machine)

> **You probably don't need this.** On a low-traffic road (a private drive, a
> quiet street) the Pi runs YOLO **once per car**, with long gaps between cars,
> so inline recognition (`recognition.defer: false`) is plenty and keeps the
> whole system to just the Pi + your web host. Deferral only earns its keep on a
> **busy** road where per-pass inference can't keep up with back-to-back cars.

The YOLO type/make/model stage is the expensive part; colour and speed are
cheap. You can split them: let the Pi capture video, measure speed, estimate
colour and save images in real time, then run YOLO **later on a beefier box**
that reads those saved images and fills the attributes in.

Turn it on with two config knobs:

```yaml
recognition:
  enabled: true
  defer: true          # Pi does colour only; never loads torch/ultralytics
recording:
  always_snapshot: true  # keep a JPEG for EVERY pass so sub-threshold passes
                         # are enrichable later, not just captured ones
```

In defer mode the Pi writes CSV rows with `vehicle_type`/`make`/`model`/`year`
blank (colour + speed are still filled inline). Then, on a desktop/GPU machine
that can see the same `captures/` folder (a mount, an `rsync`, or the same SD
card), run the worker:

```bash
python tools/recognize_worker.py --config config.yaml          # one pass
python tools/recognize_worker.py --config config.yaml --watch 30   # keep filling
```

The worker runs YOLO on each saved snapshot (or a clip frame), fills the blank
attribute columns in `events.csv`, and POSTs the fills to the backup receiver's
new `enrich` endpoint so the **off-site copy is updated too** — for every pass
that's mirrored off-site (with `backup.mirror_all: true`, that's all of them;
otherwise just captured clips). It's **idempotent** via two small ledgers next
to the media (`.recognized`, `.enriched_remote`): images it has already run are
never re-run, a confirmed off-site push is never re-sent, but an off-site push
that hasn't landed yet (Pi uploaded late) is retried until it does. It never
overwrites a value the Pi already wrote (e.g. colour).

Add a fine-grained make/model model with `--make-model-weights model.pt`,
`--no-remote` to update only the local CSV, or `--force` to re-process
everything after swapping in a better model. Note: enriching the off-site copy
requires the updated `deploy/webhost/speedkam_receiver.php` on your host.
