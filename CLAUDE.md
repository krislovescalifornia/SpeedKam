# SpeedKam — operating notes for Claude

Kris runs SpeedKam entirely through Claude prompts. Make changes AND carry them
through to the running node yourself — do **not** hand back manual steps for Kris
to run. Kris is the operator's boss, not the operator.

## Two machines

- **Dev box** (here): `C:\Users\Kris\dev\SpeedKam` — the git repo. Edit + commit here.
- **The Pi node**: hostname `speedkam-47790c.local`, user `pi`. Runs the live app.

## You HAVE direct SSH access to the node. Use it — do not ask Kris to run things.

Key auth is installed and working. Just run:

```bash
ssh speedkam '<command>'
```

`speedkam` is an alias in `~/.ssh/config` (→ `pi@speedkam-47790c.local`, key auth,
`BatchMode yes` so it never hangs on a password). Passwordless `sudo` works on the
node, so `ssh speedkam 'sudo systemctl restart speedkam'` etc. run non-interactively.
These commands are pre-approved in `.claude/settings.local.json` — no prompts.

Quick checks:
- Reach it: `ssh speedkam 'hostname; uptime'`
- Service status: `ssh speedkam 'systemctl status speedkam --no-pager'`
- Live logs: `ssh speedkam 'journalctl -u speedkam -n 50 --no-pager'`
- App code lives at `/home/pi/SpeedKam` on the node.

If `ssh speedkam` ever fails: it's almost always mDNS flakiness from the shell — retry once
or twice (transient exit 255 / could-not-resolve). The key itself is fine.

## Deploy paths (fastest → slowest)

1. **Direct** — edit a file on the node over SSH and `sudo systemctl restart speedkam`.
   Best for testing a change on the node immediately.
2. **Git + pull** — `git push origin main` here, then on the node
   `ssh speedkam 'cd ~/SpeedKam && sudo systemctl start speedkam-update.service'`
   (runs the tested `git pull --ff-only` + restart). Use this to make a committed
   change go live now instead of waiting for `speedkam-update.timer` (boot + daily).
3. **Timer only** — just `git push`; the node picks it up within a day. Slowest.

Commit only the intended file(s), not the whole dirty working tree.

## HTTP control surface (from the dev box, no SSH)

The node serves settings APIs on port 8080, e.g.
`curl -X POST http://speedkam-47790c.local:8080/api/speedlimit -d '{"limit":25}'`
(also `/api/speedkapture`, `/api/orientation`). Handy for quick setting changes/verification.

## The off-site webhost (speedkam.com) — you CAN deploy to it now

The PHP receiver + dashboard live on Bluehost (cPanel), separate from the Pi.
Deploy over SFTP with the `speedkam-web` SSH alias (key auth; **SFTP only — the
account has no interactive shell**, so `ssh speedkam-web '<cmd>'` fails; use
`sftp -b - speedkam-web` with put/get/ls):

- Host: `50.6.155.107` (Bluehost shared IP), user `qfrwnvmy`, port 22, key `~/.ssh/id_ed25519`.
- **Live bundle is `/fleet/`**, NOT `/node-7fq2k9/` — the node posts to
  `https://speedkam.com/fleet/speedkam_receiver.php` and its backups land in
  `.../fleet/speedkam_data/nodes/<cpu-serial>/`. `node-7fq2k9/` is a dormant
  legacy bundle (no data). Don't be misled by an old `node-7fq2k9` URL.
- Docroot on the server: `public_html/website_489e3d33/` (a Bluehost "website
  container"). So the dashboard is
  `public_html/website_489e3d33/fleet/speedkam_dashboard.php`.

Deploy the dashboard (back up first, then verify byte-identical + HTTP 200):
```bash
printf 'put deploy/webhost/speedkam_dashboard.php public_html/website_489e3d33/fleet/speedkam_dashboard.php\n' | sftp -b - speedkam-web
```
No local PHP; lint with `php -l` by installing `php-cli` on the node briefly
(`ssh speedkam 'sudo apt-get install -y php-cli && php -l /tmp/x.php'`) then purge it.
`speedkam_config.php` (secret + dashboard password) already lives on the server —
never overwrite it. Data flows off-site already, so dashboard-only changes need
no node change.
