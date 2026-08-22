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
