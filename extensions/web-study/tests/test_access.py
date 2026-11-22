# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import json

import pytest
from fastapi.testclient import TestClient

from tests.helpers import PASSWORDS
from webstudy import USERS, access
from webstudy.app import create_app
from webstudy.auth import Auth

CONFIG = {"team": "team", "aud": "audience", "domain": "study.example"}
ASSERTION = {"cf-access-jwt-assertion": "assertion"}


@pytest.fixture
def app(tmp_path):
    (tmp_path / "access.json").write_text(json.dumps(CONFIG), encoding="utf-8")
    Auth(tmp_path / "auth.sqlite3").initialize(PASSWORDS)
    return create_app(tmp_path)


def local(app):
    return TestClient(
        app, client=("127.0.0.1", 50000), base_url="https://study.example"
    )


def test_entry_link_signs_into_its_account(app, monkeypatch):
    monkeypatch.setattr(access, "valid_assertion", lambda config, token: True)
    for user in USERS:
        with local(app) as client:
            entry = client.get(f"/{user}", headers=ASSERTION, follow_redirects=False)
            assert entry.status_code == 303
            assert entry.headers["location"] == "/study"
            page = client.get("/study")
            assert f"单词复习 · {user.upper()}" in page.text
            assert "退出" in page.text


def test_only_tunnel_requests_are_signed_in(app, monkeypatch):
    monkeypatch.setattr(access, "valid_assertion", lambda config, token: True)
    with TestClient(app, base_url="https://study.example") as remote:
        # Not arriving from the loopback tunnel proxy.
        response = remote.get(f"/{USERS[0]}", headers=ASSERTION, follow_redirects=False)
        assert response.headers["location"] == "/login"


def test_missing_or_rejected_assertions_go_to_the_login_page(app, monkeypatch):
    monkeypatch.setattr(access, "valid_assertion", lambda config, token: False)
    with local(app) as client:
        for headers in (ASSERTION, {}):
            response = client.get(
                f"/{USERS[1]}", headers=headers, follow_redirects=False
            )
            assert response.headers["location"] == "/login"


def test_unknown_entry_paths_do_not_exist(app):
    with local(app) as client:
        assert client.get("/stranger").status_code == 404


def test_password_login_still_works_without_access_config(tmp_path):
    Auth(tmp_path / "auth.sqlite3").initialize(PASSWORDS)
    app = create_app(tmp_path)
    assert app.state.access is None
    with TestClient(app) as client:
        landing = client.get("/study", follow_redirects=False)
        assert landing.headers["location"] == "/login"
