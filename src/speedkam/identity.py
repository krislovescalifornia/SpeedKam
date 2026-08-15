# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Stable, unique per-node identity for fleet backup + remote control.

At fleet scale, every camera POSTs to ONE shared receiver URL and identifies
itself with this id, so the server can bucket each node's data separately with
no per-node configuration baked into the image.

Derivation, in order of preference:
  1. Raspberry Pi CPU serial (``/proc/cpuinfo`` ``Serial``) -- unique per board
     and stable across re-flashes, so a re-imaged card keeps its identity.
  2. systemd ``machine-id`` -- unique per install (regenerated per clone on first
     boot), a good fallback on hardware that doesn't expose a CPU serial.
  3. hostname -- last resort.

Cached so every thread/worker on the node agrees on one value.
"""
from __future__ import annotations

import functools
import re

# Sent on every request to the off-site receiver. Some shared-host WAFs (e.g.
# mod_security) 406-block the default "python-requests/x.y" User-Agent, which
# silently kills backup + heartbeat for the whole fleet -- so we present a real,
# stable identifier instead.
USER_AGENT = "SpeedKam/1.0"


def http_headers(secret):
    """Standard headers for a request to the receiver: shared auth key + UA."""
    return {"X-SpeedKam-Key": secret, "User-Agent": USER_AGENT}


def _from_cpuinfo():
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.lower().startswith("serial"):
                    val = line.split(":", 1)[1].strip()
                    # A Pi with no real serial reports all zeros -- treat as absent.
                    if val and set(val) != {"0"}:
                        return val
    except OSError:
        return None
    return None


def _from_machine_id():
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                val = f.read().strip()
            if val:
                return val
        except OSError:
            continue
    return None


def _from_hostname():
    try:
        import socket
        return socket.gethostname()
    except OSError:
        return None


@functools.lru_cache(maxsize=1)
def node_id() -> str:
    """A stable, filesystem/URL-safe identifier for this camera (max 32 chars)."""
    raw = _from_cpuinfo() or _from_machine_id() or _from_hostname() or "unknown"
    # Keep only chars that are safe in a URL path segment and a folder name; drop
    # dots so a node id can never be a path-traversal token on the receiver.
    slug = re.sub(r"[^A-Za-z0-9_-]", "", str(raw))
    return slug[:32] or "unknown"
