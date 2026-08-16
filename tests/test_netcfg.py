# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Wi-Fi onboarding: nmcli scan parsing and the AP-mode setup portal."""
from speedkam import netcfg


class _FakeCP:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def test_scan_parses_dedups_and_drops_hidden(monkeypatch):
    # nmcli -m multiline output; note an SSID with a colon and a duplicate.
    out = (
        "SSID:HomeNet\nSIGNAL:82\nSECURITY:WPA2\n"
        "SSID:Cafe:Guest\nSIGNAL:40\nSECURITY:\n"       # colon in name, open
        "SSID:HomeNet\nSIGNAL:55\nSECURITY:WPA2\n"       # weaker dup
        "SSID:\nSIGNAL:20\nSECURITY:WPA2\n"              # hidden -> dropped
        "SSID:Neighbor\nSIGNAL:67\nSECURITY:WPA3\n"
    )
    monkeypatch.setattr(netcfg, "_nmcli", lambda *a, **k: _FakeCP(out))
    nets = netcfg.scan("wlan0")

    by = {n.ssid: n for n in nets}
    assert "" not in by                              # hidden dropped
    assert by["HomeNet"].signal == 82                # strongest dup kept
    assert by["Cafe:Guest"].secured is False         # colon survives; open
    assert by["Neighbor"].secured is True            # WPA3 secured
    assert [n.ssid for n in nets][0] == "HomeNet"    # sorted by signal desc


def test_scan_survives_nmcli_missing(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("nmcli")
    monkeypatch.setattr(netcfg, "_nmcli", boom)
    assert netcfg.scan("wlan0") == []


def test_is_online_full_is_online(monkeypatch):
    monkeypatch.setattr(netcfg, "_nmcli", lambda *a, **k: _FakeCP("full"))
    assert netcfg.is_online() is True


def test_is_online_limited_is_offline(monkeypatch):
    # 'limited' = associated but no internet -> still needs onboarding.
    monkeypatch.setattr(netcfg, "_nmcli", lambda *a, **k: _FakeCP("limited"))
    monkeypatch.setattr(netcfg, "_socket_online", lambda: True)  # must be ignored
    assert netcfg.is_online() is False


def test_is_online_unknown_falls_back_to_socket(monkeypatch):
    monkeypatch.setattr(netcfg, "_nmcli", lambda *a, **k: _FakeCP("unknown"))
    monkeypatch.setattr(netcfg, "_socket_online", lambda: True)
    assert netcfg.is_online() is True
    monkeypatch.setattr(netcfg, "_socket_online", lambda: False)
    assert netcfg.is_online() is False


def test_ap_ssid_has_node_suffix():
    name = netcfg._ap_ssid("SpeedKam-Setup")
    assert name.startswith("SpeedKam-Setup-")
    assert len(name) > len("SpeedKam-Setup-")


# --------------------------------------------------------------------------- #
# portal routes                                                               #
# --------------------------------------------------------------------------- #
def _client(networks=None, error=None, captured=None):
    networks = networks or [netcfg.Network("HomeNet", 82, True),
                            netcfg.Network("Open Cafe", 40, False)]
    on_submit = (lambda s: captured.append(s)) if captured is not None else (lambda s: None)
    app = netcfg.build_portal_app("SpeedKam Wi-Fi setup", "10.42.0.1",
                                  networks, error, on_submit)
    return app.test_client()


def test_portal_index_lists_networks_and_escapes_error():
    c = _client(error="bad password <x>")
    r = c.get("/")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "HomeNet" in body and "Open Cafe" in body
    assert "&lt;x&gt;" in body                        # error HTML-escaped
    assert body.count('id="ssid_hidden"') == 1        # no duplicate ids


def test_portal_connect_captures_credentials():
    captured = []
    c = _client(captured=captured)
    r = c.post("/connect", data={"ssid": "HomeNet", "password": "hunter2"})
    assert r.status_code == 200
    assert "Joining" in r.get_data(as_text=True)
    assert len(captured) == 1
    assert captured[0].ssid == "HomeNet"
    assert captured[0].password == "hunter2"
    assert captured[0].hidden is False


def test_portal_connect_requires_ssid():
    captured = []
    c = _client(captured=captured)
    r = c.post("/connect", data={"password": "x"})
    assert r.status_code == 400
    assert captured == []


def test_portal_captive_probes_redirect_to_portal():
    c = _client()
    for path in ("/generate_204", "/hotspot-detect.html", "/anything/else"):
        r = c.get(path)
        assert r.status_code == 302, path
        assert r.headers["Location"] == "http://10.42.0.1/"
