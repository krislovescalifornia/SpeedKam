# SpeedKam off-site host — setup

Your camera mirrors every pass (CSV row + snapshot, plus a video clip for
speeders) to a web domain you own, and you view it all from a password-protected
**dashboard** on that same domain. So even if the camera is stolen or damaged,
the records — and a live "is it still online?" view — survive off-site.

Three PHP files work together, all in one folder:

| File | Role |
|---|---|
| `speedkam_config.example.php` | Template. Copy to `speedkam_config.php` and edit that copy. |
| `speedkam_config.php` | Your real shared settings (secret, dashboard password, data dir). Gitignored — never committed. |
| `speedkam_receiver.php` | The endpoint the camera POSTs to (uploads + heartbeat + settings). |
| `speedkam_dashboard.php` | The human web UI: view records, control the camera. |

## 1. Upload the files

Put all three into the same folder on your domain, e.g. reachable at:

    https://yourdomain.example/speedkam/speedkam_receiver.php
    https://yourdomain.example/speedkam/speedkam_dashboard.php

Requirements: any host that runs **PHP** and allows **file uploads** (typical
shared/cPanel hosting is fine — no Python or special server needed; the camera
does all the video/speed/recognition work). For video clips, raise the upload
limits — put a `.user.ini` or `php.ini` next to the scripts with:

    upload_max_filesize = 64M
    post_max_size       = 80M

## 2. Set the secret and the dashboard password

Copy the template, then edit the copy (the real file is gitignored):

```bash
cp speedkam_config.example.php speedkam_config.php
```

Open **`speedkam_config.php`** and set two values:

```php
$SECRET             = '<long random string>';   // the CAMERA authenticates with this
$DASHBOARD_PASSWORD = '<a different password>';  // YOU type this to view the dashboard
```

Put the **same** `$SECRET` in the camera's `config.local.yaml` (untracked
overlay — copy it from `config.local.example.yaml`), not in `config.yaml`:

```yaml
backup:
  url: "https://yourdomain.example/speedkam/speedkam_receiver.php"
  secret: "<the same long random string>"
```

Then in the tracked `config.yaml`, just enable the features:

```yaml
backup:
  enabled: true      # url/secret come from config.local.yaml
control:
  enabled: true      # lets you change camera settings from the dashboard
```

Generate a strong secret with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

The dashboard **refuses to load** while `$DASHBOARD_PASSWORD` is still the
placeholder, so you can't accidentally publish your driveway footage unprotected.
Login is also **rate-limited**: after 5 wrong passwords from one IP the dashboard
locks that address out for 15 minutes, to slow down guessing.

## 3. Protect the data folder

Backups land in `speedkam_data/` next to the scripts. Copy the included
`htaccess-for-data-folder` to `speedkam_data/.htaccess` (the receiver also writes
a deny rule automatically). This stops the public from browsing your backups
directly — the dashboard serves snapshots and clips through an **authenticated
proxy** instead, so media is only viewable once you've logged in.

> Use **HTTPS** so the secret, the password, and the footage are encrypted in
> transit. Keep the secret private; rotate it (in both places) if it leaks.

## 4. Verify

- **Receiver health:** open the receiver URL in a browser — it shows a green
  "Online" status page (storage-writable, secret-configured, upload limits). It
  reveals no secret or data, so it's safe to leave public. Add `?format=json`
  for a monitoring probe (`{"ok":true,...}`, HTTP 200 healthy / 503 if not).
- **Dashboard:** open the dashboard URL, sign in with `$DASHBOARD_PASSWORD`.
  Until the camera has sent data it'll be empty; once it runs you'll see counts,
  a gallery of passes, and an online/offline pill with the last check-in time.

On the camera, run `python serve.py` (or the systemd service). To push all
pre-existing records off-site (first-time backup or after downtime):

```bash
python tools/backfill_sync.py
```

## Remote control (how it reaches a camera behind home NAT)

Your camera sits behind your home router, so the host can't connect *in* to it.
Control flows the other way: the camera **checks in** every `control.poll_seconds`
(POST `action=sync`), reporting its status and pulling any settings you changed
on the dashboard. So when you set a new SpeedKapture threshold on the website, it
takes effect on the camera's next check-in (a few seconds later) — no
port-forwarding, nothing about your home network exposed.

The dashboard writes your desired settings to `desired.json` with a revision
number; the camera only re-applies when that number changes, so your on-camera
(LAN) dashboard tweaks aren't fought over between remote edits.

## Layout on the server

```
speedkam/
  speedkam_config.example.php  template (tracked in git)
  speedkam_config.php        your copy: secret + dashboard password (gitignored)
  speedkam_receiver.php      camera upload / heartbeat endpoint
  speedkam_dashboard.php     the web UI you open in a browser
  speedkam_data/             (created automatically; keep private)
    events.csv               mirror of the camera's event log (+ received_at)
    media/YYYY-MM-DD/*.jpg    snapshots
    media/YYYY-MM-DD/*.mp4    clips (speeders)
    status.json              latest camera heartbeat (liveness + counts)
    desired.json             settings you've queued for the camera
    .index/                  per-event dedupe markers (safe to keep)
```

Uploads are idempotent: the server dedupes by event id, so re-sending the same
event never creates duplicates. Deferred attributes (make/model, on a busy-road
setup) arrive later via the receiver's `enrich` action; on a low-traffic drive
the camera fills them in before uploading, so you won't need that.

## Dedicating a whole domain (apex hardening)

If you point a **dedicated domain** at SpeedKam (e.g. an addon domain whose only
job is off-site infrastructure), don't drop the bundle in the docroot. Put the
three PHP files in a **subfolder** and lock down the apex around them. The files
in [`apex/`](apex/) are drop-in templates for the docroot:

| Template | Rename to | Where |
|---|---|---|
| `apex/index.html` | `index.html` | docroot — a dead end that reveals nothing |
| `apex/robots.txt` | `robots.txt` | docroot — keeps the whole domain out of search |
| `apex/htaccess-for-apex` | `.htaccess` | docroot — forces HTTPS, no listings, noindex + security headers |

Resulting layout for a dedicated domain:

```
yourdomain/                       docroot (apex)
  index.html                      dead-end page (from apex/index.html)
  robots.txt                      Disallow: /   (from apex/robots.txt)
  .htaccess                       HTTPS + noindex + headers (from apex/htaccess-for-apex)
  <bundle>/                       pick your own folder name (e.g. ingest/)
    speedkam_config.php           secret + dashboard password (gitignored)
    speedkam_receiver.php         camera endpoint
    speedkam_dashboard.php        the web UI
    speedkam_data/                backups (.htaccess-denied)
```

The camera then posts to `https://yourdomain/<bundle>/speedkam_receiver.php`
(this is `backup.url` in `config.local.yaml`) and you browse
`https://yourdomain/<bundle>/speedkam_dashboard.php`.

> **Pick the `<bundle>` folder name before you image a fleet.** It's baked into
> every node's `backup.url` (and rides along inside a cloned SD image), so
> changing it later means editing every deployed node. An unguessable name adds
> cheap obscurity in front of the dashboard's password gate.
