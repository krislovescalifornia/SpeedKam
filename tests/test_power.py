# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Dashboard power controls: /api/power reboots or powers off the Pi, gated by
config and by a sudoers rule (represented here by a stubbed subprocess)."""
import types

import pytest

from speedkam import web


class _FakeRunner:
    """create_app only reads runner.cfg at setup; that's all we need here."""
    def __init__(self, allow=True):
        self.cfg = {"web": {"allow_power_control": allow}}


def _client(allow=True):
    return web.create_app(_FakeRunner(allow=allow)).test_client()


@pytest.fixture
def calls(monkeypatch):
    """Record subprocess.run invocations and return a controllable result."""
    recorded = []
    result = {"returncode": 0, "stdout": "", "stderr": ""}

    def fake_run(cmd, **kwargs):
        recorded.append(cmd)
        return types.SimpleNamespace(**result)

    monkeypatch.setattr(web.subprocess, "run", fake_run)
    return recorded, result


def test_reboot_runs_systemctl_reboot(calls):
    recorded, _ = calls
    r = _client().post("/api/power", json={"action": "reboot"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert recorded == [["sudo", "-n", "systemctl", "reboot"]]


def test_shutdown_maps_to_poweroff(calls):
    recorded, _ = calls
    r = _client().post("/api/power", json={"action": "shutdown"})
    assert r.status_code == 200
    assert recorded == [["sudo", "-n", "systemctl", "poweroff"]]


def test_unknown_action_is_rejected(calls):
    recorded, _ = calls
    r = _client().post("/api/power", json={"action": "sleep"})
    assert r.status_code == 400
    assert recorded == []          # nothing was executed


def test_disabled_in_config_forbids(calls):
    recorded, _ = calls
    r = _client(allow=False).post("/api/power", json={"action": "reboot"})
    assert r.status_code == 403
    assert recorded == []


def test_missing_sudo_rule_gives_friendly_error(calls):
    recorded, result = calls
    result["returncode"] = 1
    result["stderr"] = "sudo: a password is required"
    r = _client().post("/api/power", json={"action": "reboot"})
    assert r.status_code == 500
    body = r.get_json()
    assert body["ok"] is False
    assert "install-service.sh" in body["error"]   # actionable hint, not raw sudo noise
