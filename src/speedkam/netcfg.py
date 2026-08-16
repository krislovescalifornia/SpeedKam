# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Wi-Fi onboarding for headless SpeedKam nodes (AP-mode captive portal).

A node deployed at a new site has no screen and no known network. This module
is the "join me to your Wi-Fi from a phone" fallback, run at boot as
``speedkam-netcfg.service``:

    boot -> is the node actually online? --yes--> exit, nothing to do
                                         --no--> raise a Wi-Fi access point
    named ``SpeedKam-Setup-<nodeid>`` and serve a tiny setup page on it. An
    operator connects a phone to that AP, picks a nearby network, types the
    password; we join it (NetworkManager saves the profile), verify we actually
    reached the internet, then tear the AP down. That saved profile auto-joins
    on every future boot -- the AP only ever comes back if the node can't get
    online. So the SAME node moves from your house Wi-Fi to a random site with
    no re-imaging and no SSH.

Deliberately NOT a network-evasion tool. It only ever joins a network the
operator is standing in front of and authorised to use; it never fights a
network's access controls. A node that's online but isolated from LAN peers
(e.g. a guest network) is considered online here and left alone -- that's the
network doing its job, not a problem to route around.

Design notes:
  * Backend is NetworkManager via ``nmcli`` (Raspberry Pi OS Bookworm default).
    ``ipv4.method shared`` gives us AP + DHCP + a captive gateway (10.42.0.1)
    with no hand-rolled hostapd/dnsmasq config.
  * The Pi's single radio can't host the AP and join a candidate network at the
    same time, so the flow is optimistic: we accept the credentials, tell the
    operator the setup network will drop, THEN switch the radio over to join. If
    the join fails we re-raise the AP with the error so they can retry.
  * Dependency-light on purpose (stdlib + Flask, both already present) so this
    keeps working even if the heavy camera stack is broken -- getting a stranded
    node back online must never depend on OpenCV importing.
"""
from __future__ import annotations

import argparse
import html
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from .config import load_config
from .identity import node_id


# --------------------------------------------------------------------------- #
# nmcli helpers                                                                #
# --------------------------------------------------------------------------- #
def _nmcli(*args, timeout=60, check=False):
    """Run nmcli and return CompletedProcess. Never raises on non-zero unless
    check=True; callers inspect returncode/stderr themselves."""
    return subprocess.run(
        ["nmcli", *args],
        capture_output=True, text=True, timeout=timeout, check=check,
    )


def is_online(interface=None) -> bool:
    """True if the node has real internet, not merely an associated radio.

    Primary signal is NetworkManager's own connectivity check ("full"). When NM
    reports "unknown" (its connectivity checking can be disabled), fall back to a
    direct socket probe so we don't falsely strand a perfectly-online node in AP
    mode.
    """
    try:
        r = _nmcli("-t", "networking", "connectivity", "check", timeout=20)
        state = (r.stdout or "").strip().lower()
    except (OSError, subprocess.SubprocessError):
        state = "unknown"

    if state == "full":
        return True
    if state in ("none", "portal", "limited"):
        # Associated-but-no-internet ("limited"/"portal") or nothing at all: for
        # onboarding purposes the node still needs a usable network, so treat as
        # offline and let the operator pick a working one.
        return False

    # state == "unknown" (or nmcli missing): verify with a real reachability test.
    return _socket_online()


def _socket_online() -> bool:
    """Can we open a TCP connection to a public host? A route+DNS smoke test."""
    for host, port in (("1.1.1.1", 53), ("8.8.8.8", 53)):
        try:
            with socket.create_connection((host, port), timeout=4):
                return True
        except OSError:
            continue
    return False


def wait_online(timeout_seconds, interface=None) -> bool:
    """Poll is_online() until True or the deadline. Returns True if we got
    online. Used both for the initial "are we already fine?" check and to
    confirm a freshly-joined network actually reaches the internet."""
    deadline = time.monotonic() + max(0, timeout_seconds)
    while True:
        if is_online(interface):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(3)


@dataclass
class Network:
    ssid: str
    signal: int
    secured: bool


def scan(interface) -> list[Network]:
    """Visible Wi-Fi networks, strongest first, de-duplicated by SSID.

    Uses nmcli's multiline terse output (``-m multiline``) so an SSID containing
    a colon can't corrupt field parsing -- each field is on its own line and we
    only split on the FIRST colon.
    """
    try:
        r = _nmcli("-t", "-m", "multiline", "-f", "SSID,SIGNAL,SECURITY",
                   "device", "wifi", "list", "ifname", interface,
                   "--rescan", "yes", timeout=45)
    except (OSError, subprocess.SubprocessError):
        return []

    best: dict[str, Network] = {}
    cur: dict[str, str] = {}

    def flush():
        ssid = (cur.get("SSID") or "").strip()
        if not ssid:  # hidden networks report an empty SSID; skip in the list
            return
        try:
            signal = int(cur.get("SIGNAL") or 0)
        except ValueError:
            signal = 0
        sec = (cur.get("SECURITY") or "").strip()
        secured = bool(sec) and sec not in ("--", "")
        prev = best.get(ssid)
        if prev is None or signal > prev.signal:
            best[ssid] = Network(ssid=ssid, signal=signal, secured=secured)

    for line in (r.stdout or "").splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        if key == "SSID" and cur:
            flush()
            cur = {}
        cur[key] = val
    if cur:
        flush()

    return sorted(best.values(), key=lambda n: n.signal, reverse=True)


AP_CON_NAME = "speedkam-setup"


def ap_up(interface, ssid, password, captive_ip) -> bool:
    """Bring up an open (or WPA2, if a password is given) AP on `interface` with
    NAT+DHCP via NetworkManager 'shared' mode. Returns True on success.

    'shared' mode makes NM run its own dnsmasq for DHCP and assign the gateway
    (captive_ip, default 10.42.0.1); we bind the portal there.
    """
    # Start clean: a stale profile from a previous session would refuse to add.
    ap_down(interface)

    add = ["connection", "add", "type", "wifi", "ifname", interface,
           "con-name", AP_CON_NAME, "autoconnect", "no",
           "ssid", ssid,
           "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
           "ipv4.method", "shared", "ipv6.method", "ignore"]
    if password and len(password) >= 8:
        add += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]

    r = _nmcli(*add, timeout=30)
    if r.returncode != 0:
        print(f"[speedkam-netcfg] AP add failed: {r.stderr.strip()}")
        return False

    r = _nmcli("connection", "up", AP_CON_NAME, timeout=45)
    if r.returncode != 0:
        print(f"[speedkam-netcfg] AP up failed: {r.stderr.strip()}")
        ap_down(interface)
        return False
    print(f"[speedkam-netcfg] setup AP '{ssid}' up on {interface} "
          f"(portal http://{captive_ip}/)")
    return True


def ap_down(interface=None):
    """Tear down and delete the setup AP profile so the radio is free to join a
    real network and no stray AP profile lingers."""
    _nmcli("connection", "down", AP_CON_NAME, timeout=20)
    _nmcli("connection", "delete", AP_CON_NAME, timeout=20)


def join(interface, ssid, password, hidden=False) -> tuple[bool, str]:
    """Attempt to join `ssid`. NetworkManager saves the profile on success so it
    auto-joins on future boots. Returns (ok, error_message)."""
    args = ["-w", "45", "device", "wifi", "connect", ssid, "ifname", interface]
    if password:
        args += ["password", password]
    if hidden:
        args += ["hidden", "yes"]
    try:
        r = _nmcli(*args, timeout=60)
    except subprocess.TimeoutExpired:
        return False, "timed out trying to associate"
    if r.returncode == 0:
        return True, ""
    # nmcli writes the useful reason to stderr (bad password, out of range, ...).
    return False, (r.stderr or r.stdout or "connection failed").strip()


def forget(ssid):
    """Delete a saved profile for `ssid` (used to drop a failed attempt so it
    doesn't keep auto-retrying a wrong password)."""
    _nmcli("connection", "delete", ssid, timeout=20)


# --------------------------------------------------------------------------- #
# the captive setup portal (Flask)                                            #
# --------------------------------------------------------------------------- #
@dataclass
class Submission:
    ssid: str
    password: str
    hidden: bool


def _page(title, captive_ip, networks, error=None):
    opts = []
    for n in networks:
        lock = " \U0001f512" if n.secured else ""
        bars = "█" * max(1, min(4, (n.signal // 25) + 1))
        label = f"{html.escape(n.ssid)}{lock}  {bars}"
        opts.append(
            f'<option value="{html.escape(n.ssid)}" '
            f'data-secured="{1 if n.secured else 0}">{label}</option>')
    options_html = "\n".join(opts) or '<option value="">(no networks found)</option>'
    err_html = (f'<p class="err">{html.escape(error)}</p>' if error else "")
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         margin: 0; padding: 1.25rem; max-width: 32rem; }}
  h1 {{ font-size: 1.35rem; margin: 0 0 .25rem; }}
  p.sub {{ margin: 0 0 1.25rem; opacity: .75; }}
  label {{ display: block; font-weight: 600; margin: 1rem 0 .35rem; }}
  select, input {{ width: 100%; box-sizing: border-box; padding: .6rem;
                  font-size: 1rem; border-radius: .5rem;
                  border: 1px solid rgba(128,128,128,.5); }}
  button {{ width: 100%; margin-top: 1.5rem; padding: .8rem; font-size: 1.05rem;
           font-weight: 600; border: 0; border-radius: .5rem; cursor: pointer;
           background: #2563eb; color: #fff; }}
  .err {{ background: #fee2e2; color: #991b1b; padding: .6rem .75rem;
         border-radius: .5rem; }}
  @media (prefers-color-scheme: dark) {{ .err {{ background:#450a0a; color:#fecaca; }} }}
  .hint {{ font-size: .85rem; opacity: .7; margin-top: .35rem; }}
</style></head>
<body>
  <h1>{html.escape(title)}</h1>
  <p class="sub">Pick the Wi-Fi network this SpeedKam node should join.</p>
  {err_html}
  <form method="POST" action="/connect">
    <label for="ssid_select">Network</label>
    <select id="ssid_select" name="ssid_select" onchange="syncSsid()">{options_html}</select>
    <div class="hint">Not listed? <a href="#" onclick="manual();return false">enter it manually</a></div>
    <input type="hidden" id="ssid_hidden" name="ssid">
    <div id="manualwrap" style="display:none">
      <label for="ssid_manual">Network name (SSID)</label>
      <input id="ssid_manual" name="ssid_manual" autocapitalize="none" oninput="syncSsid()">
    </div>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocapitalize="none"
           placeholder="leave blank for an open network">
    <label style="font-weight:400"><input type="checkbox" name="hidden" value="1"
           style="width:auto"> This is a hidden network</label>
    <button type="submit">Join network</button>
  </form>
<script>
  function manual() {{ document.getElementById('manualwrap').style.display='block';
                       document.getElementById('ssid_manual').focus(); syncSsid(); }}
  function syncSsid() {{
    var m = document.getElementById('ssid_manual');
    var s = document.getElementById('ssid_select');
    document.getElementById('ssid_hidden').value =
      (m && m.value) ? m.value : (s ? s.value : '');
  }}
  syncSsid();
</script>
</body></html>"""


def _trying_page(ssid):
    ssid = html.escape(ssid)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connecting…</title>
<style>body{{font-family:system-ui,sans-serif;margin:0;padding:1.5rem;max-width:32rem}}
h1{{font-size:1.3rem}}</style></head><body>
<h1>Joining “{ssid}”…</h1>
<p>This setup network (<b>SpeedKam-Setup</b>) will now disappear while the node
switches over. That's expected.</p>
<ul>
  <li><b>If it worked:</b> the node is now on “{ssid}”. Reconnect your
      phone to that same network and open the SpeedKam dashboard on port 8080.</li>
  <li><b>If this setup network comes back</b> in a minute or two, the password
      was wrong or the network was unreachable — rejoin
      <b>SpeedKam-Setup</b> and try again.</li>
</ul>
</body></html>"""


def build_portal_app(title, captive_ip, networks, error, on_submit):
    """Construct the Flask setup portal. `on_submit(Submission)` is called when
    the operator posts valid credentials. Split out from _run_portal so it can be
    exercised with Flask's test_client without touching Wi-Fi hardware.
    """
    from flask import Flask, request, redirect

    app = Flask(__name__)

    @app.route("/", methods=["GET"])
    def index():
        return _page(title, captive_ip, networks, error)

    @app.route("/connect", methods=["POST"])
    def connect():
        ssid_val = (request.form.get("ssid")
                    or request.form.get("ssid_manual")
                    or request.form.get("ssid_select") or "").strip()
        if not ssid_val:
            return _page(title, captive_ip, networks,
                         "Please choose or type a network name."), 400
        sub = Submission(
            ssid=ssid_val,
            password=request.form.get("password", ""),
            hidden=bool(request.form.get("hidden")),
        )
        on_submit(sub)
        return _trying_page(ssid_val)

    # Captive-portal detection: phones probe well-known URLs and pop a "sign in"
    # sheet if the response isn't what they expect. NM's shared dnsmasq points
    # everything at us, so redirect any stray path to the setup page.
    @app.route("/generate_204")
    @app.route("/gen_204")
    @app.route("/ncsi.txt")
    @app.route("/connecttest.txt")
    @app.route("/hotspot-detect.html")
    @app.route("/<path:_ignored>")
    def captive(_ignored=None):
        return redirect(f"http://{captive_ip}/", code=302)

    return app


def _run_portal(interface, ssid, captive_ip, networks, error, port=80):
    """Bring up the AP, serve the setup page, and block until the operator
    submits credentials (or the node comes online by other means, e.g. someone
    plugs in Ethernet). Returns a Submission, or None if we went online.

    The AP is always torn down before returning so the radio is free to join.
    """
    if not ap_up(interface, ssid, _ap_password_holder[0], captive_ip):
        # If the AP can't come up there's nothing to serve; back off and let the
        # main loop retry from the top (it re-checks connectivity first).
        time.sleep(15)
        return None

    result: dict[str, Submission] = {}
    submitted = threading.Event()

    def _capture(sub):
        result["sub"] = sub
        submitted.set()

    app = build_portal_app(_portal_title[0], captive_ip, networks, error, _capture)

    from werkzeug.serving import make_server
    server = make_server("0.0.0.0", port, app, threaded=True)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        # Wait for a submission, but also notice if the node comes online some
        # other way (Ethernet plugged in) so we don't sit in AP mode forever.
        while not submitted.wait(timeout=10):
            if _socket_online():
                print("[speedkam-netcfg] came online during setup; closing portal.")
                return None
    finally:
        server.shutdown()
        ap_down(interface)

    return result.get("sub")


# Small module-level holders so the Flask closures above stay simple; set by
# main() before any portal session runs.
_ap_password_holder = [""]
_portal_title = ["SpeedKam Wi-Fi setup"]


# --------------------------------------------------------------------------- #
# entry point / state machine                                                 #
# --------------------------------------------------------------------------- #
def _ap_ssid(prefix) -> str:
    """A per-node AP name so several nodes being set up nearby don't collide."""
    return f"{prefix}-{node_id()[-6:] or 'node'}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="speedkam-netcfg",
        description="Wi-Fi onboarding: open a setup AP when the node is offline.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--force-portal", action="store_true",
                    help="skip the online check and open the setup AP now "
                         "(for testing the portal).")
    args = ap.parse_args(argv)

    cfg = load_config(args.config).get("netcfg", {})
    if not cfg.get("enabled", True):
        print("[speedkam-netcfg] disabled in config; exiting.")
        return 0

    interface = cfg.get("interface", "wlan0")
    captive_ip = cfg.get("captive_ip", "10.42.0.1")
    ap_ssid = _ap_ssid(cfg.get("ap_ssid_prefix", "SpeedKam-Setup"))
    _ap_password_holder[0] = cfg.get("ap_password", "") or ""
    _portal_title[0] = cfg.get("portal_title", "SpeedKam Wi-Fi setup")
    online_timeout = int(cfg.get("online_timeout", 90))

    # Already online (the common case, every normal boot): do nothing at all.
    if not args.force_portal:
        print(f"[speedkam-netcfg] waiting up to {online_timeout}s for connectivity...")
        if wait_online(online_timeout, interface):
            print("[speedkam-netcfg] node is online; no setup needed.")
            return 0
        print("[speedkam-netcfg] no connectivity -> opening setup AP.")

    error = None
    while True:
        # Scan BEFORE raising the AP: on a single-radio Pi we can't scan while
        # hosting the AP, so the list is captured now and served to the portal.
        networks = scan(interface)
        sub = _run_portal(interface, ap_ssid, captive_ip, networks, error)
        if sub is None:
            # Came online by other means, or the AP couldn't start. Re-check and
            # exit if we're good; otherwise loop and offer the portal again.
            if wait_online(5, interface):
                print("[speedkam-netcfg] online; exiting.")
                return 0
            continue

        print(f"[speedkam-netcfg] operator chose '{sub.ssid}'; joining...")
        ok, err = join(interface, sub.ssid, sub.password, sub.hidden)
        if ok and wait_online(45, interface):
            print(f"[speedkam-netcfg] joined '{sub.ssid}' and online. Done.")
            return 0

        # Failed: drop the bad profile so it doesn't auto-retry, surface why,
        # and loop back to re-raise the setup AP.
        forget(sub.ssid)
        error = err or f"Couldn't reach the internet on '{sub.ssid}'."
        print(f"[speedkam-netcfg] join failed: {error}")


if __name__ == "__main__":
    sys.exit(main())
