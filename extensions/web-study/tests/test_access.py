# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import json

import pytest
from fastapi.testclient import TestClient

from tests.helpers import PASSWORDS
from webstudy import USERS, access
from webstudy.access import AccessConfig, user_for_host
from webstudy.app import create_app
from webstudy.auth import Auth

CONFIG = {"team": "team", "aud": "audience", "domain": "study.example"}


def test_user_for_host_matches_preset_accounts_only():
    config = AccessConfig(**CONFIG, certs_url="https://study.example/certs")
    assert user_for_host(config, f"{USERS[0]}.study.example") == USERS[0]
    assert user_for_host(config, f"{USERS[1]}.STUDY.example:8765") == USERS[1]
    assert user_for_host(config, "study.example") is None
    assert user_for_host(config, f"x{USERS[0]}.study.example") is None
    assert user_for_host(config, "study.example.evil.test") is None


@pytest.fixture
def app(tmp_path):
    (tmp_path / "access.json").write_text(json.dumps(CONFIG), encoding="utf-8")
    Auth(tmp_path / "auth.sqlite3").initialize(PASSWORDS)
    return create_app(tmp_path)


def local(app, host):
    return TestClient(app, client=("127.0.0.1", 50000), base_url=f"https://{host}")


def test_subdomain_link_signs_into_its_account(app, monkeypatch):
    monkeypatch.setattr(access, "valid_assertion", lambda config, token: True)
    with local(app, f"{USERS[0]}.study.example") as client:
        entry = client.get(
            "/study",
            headers={"cf-access-jwt-assertion": "assertion"},
            follow_redirects=False,
        )
        assert entry.status_code == 303
        assert entry.headers["location"] == "/study"
        page = client.get("/study")
        assert f"单词复习 · {USERS[0].upper()}" in page.text
        assert "退出" in page.text


def test_tampered_sources_stay_on_the_login_page(app, monkeypatch):
    monkeypatch.setattr(access, "valid_assertion", lambda config, token: True)
    with TestClient(app, base_url=f"https://{USERS[0]}.study.example") as remote:
        # Not arriving from the loopback tunnel proxy.
        response = remote.get("/study", follow_redirects=False)
        assert response.headers["location"] == "/login"
    with local(app, "stranger.study.example") as client:
        # Not one of the preset accounts.
        response = client.get("/study", follow_redirects=False)
        assert response.headers["location"] == "/login"
    with local(app, "study.example") as client:
        # The apex hostname maps to no account.
        response = client.get(
            "/study",
            headers={"cf-access-jwt-assertion": "assertion"},
            follow_redirects=False,
        )
        assert response.headers["location"] == "/login"
    with local(app, f"{USERS[1]}.study.example") as client:
        # No Access assertion at all.
        response = client.get("/study", follow_redirects=False)
        assert response.headers["location"] == "/login"


def test_rejected_assertions_stay_on_the_login_page(app, monkeypatch):
    monkeypatch.setattr(access, "valid_assertion", lambda config, token: False)
    with local(app, f"{USERS[1]}.study.example") as client:
        response = client.get(
            "/study",
            headers={"cf-access-jwt-assertion": "assertion"},
            follow_redirects=False,
        )
        assert response.headers["location"] == "/login"


def test_password_login_still_works_without_access_config(tmp_path):
    Auth(tmp_path / "auth.sqlite3").initialize(PASSWORDS)
    app = create_app(tmp_path)
    assert app.state.access is None
    with TestClient(app) as client:
        landing = client.get("/study", follow_redirects=False)
        assert landing.headers["location"] == "/login"
