# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import json

import pytest
from fastapi.testclient import TestClient

from webstudy import USERS, access
from webstudy.app import create_app

CONFIG = {"team": "team", "aud": "audience", "domain": "study.example"}
ASSERTION = {"cf-access-jwt-assertion": "assertion"}


@pytest.fixture
def app(tmp_path):
    (tmp_path / "access.json").write_text(json.dumps(CONFIG), encoding="utf-8")
    return create_app(tmp_path)


def tunnel(app):
    return TestClient(app, base_url="https://study.example")


def local(app):
    return TestClient(app, client=("127.0.0.1", 50000))


def test_tunnel_entry_with_valid_assertion_signs_in(app, monkeypatch):
    monkeypatch.setattr(access, "valid_assertion", lambda config, token: True)
    with tunnel(app) as client:
        entry = client.get(f"/{USERS[0]}", headers=ASSERTION, follow_redirects=False)
        assert entry.status_code == 303
        assert entry.headers["location"] == "/study"
        assert f"单词复习 · {USERS[0].upper()}" in client.get("/study").text


def test_requests_without_a_valid_assertion_are_refused(app, monkeypatch):
    monkeypatch.setattr(access, "valid_assertion", lambda config, token: True)
    with tunnel(app) as client:
        # https without an Access assertion is not enough.
        response = client.get(f"/{USERS[0]}", follow_redirects=False)
        assert response.status_code == 401
    with TestClient(app, base_url="http://study.example") as lan:
        # A direct LAN connection cannot claim https or forge a signature.
        response = lan.get(f"/{USERS[0]}", headers=ASSERTION, follow_redirects=False)
        assert response.status_code == 401


def test_rejected_assertions_are_refused(app, monkeypatch):
    monkeypatch.setattr(access, "valid_assertion", lambda config, token: False)
    with tunnel(app) as client:
        response = client.get(f"/{USERS[1]}", headers=ASSERTION, follow_redirects=False)
        assert response.status_code == 401


def test_local_machine_entry_needs_no_access_config(tmp_path):
    app = create_app(tmp_path)
    assert app.state.access is None
    with local(app) as client:
        entry = client.get(f"/{USERS[1]}", follow_redirects=False)
        assert entry.status_code == 303
        assert f"单词复习 · {USERS[1].upper()}" in client.get("/study").text


def test_unknown_entry_paths_do_not_exist(app):
    with local(app) as client:
        assert client.get("/stranger").status_code == 404
