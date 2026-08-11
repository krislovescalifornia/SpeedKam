# SpeedKam off-site backup — web host setup

This mirrors every recorded event (CSV row + snapshot + video clip) from the
camera to a web domain you own, so records survive if the camera is stolen or
damaged. The camera keeps everything locally too; this is a second copy.

## 1. Put the receiver on your site

Upload `speedkam_receiver.php` to your domain, e.g. so it is reachable at:

    https://yourdomain.example/speedkam/speedkam_receiver.php

Requirements: any host that runs **PHP** and allows **file uploads** (typical
shared/cPanel hosting is fine). For video clips, raise the upload limits — put a
`.user.ini` or `php.ini` next to the script with:

    upload_max_filesize = 64M
    post_max_size       = 80M

## 2. Set a shared secret

Open `speedkam_receiver.php` and change `$SECRET` to a long random string.
Put the **same** value in the camera's `config.yaml`:

```yaml
backup:
  enabled: true
  url: "https://yourdomain.example/speedkam/speedkam_receiver.php"
  secret: "<the same long random string>"
```

Generate one with, e.g.:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 3. Protect the data folder

Backups land in `speedkam_data/` next to the script (`events.csv` + `media/`).
Copy the included `htaccess-for-data-folder` to `speedkam_data/.htaccess` (the
script also writes a deny rule automatically). This stops the public from
browsing your backups. Access them via SFTP/your host's file manager.

> Use **HTTPS** for the URL so the secret and footage are encrypted in transit.
> The secret only authorizes uploads; anyone with it could POST data, so keep it
> private and rotate it if leaked (update both places).

## 4. Verify

On the camera, run `python serve.py` and watch the dashboard's **backup** pill —
it shows `synced → yourdomain` when the queue is empty, or `N queued` with the
error on hover if it can't reach the server. To push all pre-existing records
(first-time backup or after downtime), run:

```bash
python tools/backfill_sync.py
```

## Layout on the server

```
speedkam/
  speedkam_receiver.php
  speedkam_data/            (created automatically; keep private)
    events.csv              mirror of the camera's event log (+ received_at)
    media/YYYY-MM-DD/*.jpg  snapshots
    media/YYYY-MM-DD/*.mp4  clips
    .index/                 per-event markers (dedupe; safe to keep)
```

Retries are idempotent: the server dedupes by event id, so re-sending the same
event never creates duplicates.
