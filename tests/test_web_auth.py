# SPDX-FileCopyrightText: 2026 Kris Kling
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Optional dashboard auth: off by default, and when a password is set it gates
every request via HTTP Basic Auth."""
import base64

import pytest
from flask import Flask

from speedkam.web import _install_auth, auth_enabled


def make_app(auth_cfg):
    app = Flask(__name__)
    _install_auth(app, auth_cfg)

    @app.route("/x")
    def x():
        return "ok"

    return app.test_client()


def basic(user, pw):
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_auth_enabled_predicate():
    assert auth_enabled(None) is False
    assert auth_enabled({"password": ""}) is False
    assert auth_enabled({"password": "x"}) is True


def test_no_password_is_open():
    client = make_app({"username": "admin", "password": ""})
    assert client.get("/x").status_code == 200


def test_missing_credentials_get_401_with_challenge():
    client = make_app({"username": "admin", "password": "s3cret"})
    r = client.get("/x")
    assert r.status_code == 401
    assert r.headers.get("WWW-Authenticate", "").startswith("Basic ")


def test_correct_credentials_pass():
    client = make_app({"username": "admin", "password": "s3cret"})
    assert client.get("/x", headers=basic("admin", "s3cret")).status_code == 200


@pytest.mark.parametrize("user,pw", [("admin", "wrong"), ("root", "s3cret"), ("", "")])
def test_wrong_credentials_rejected(user, pw):
    client = make_app({"username": "admin", "password": "s3cret"})
    assert client.get("/x", headers=basic(user, pw)).status_code == 401
