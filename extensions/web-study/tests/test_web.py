# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from concurrent.futures import ThreadPoolExecutor

from tests.helpers import (
    PASSWORDS,
    browsers,
    card_state,
    confirm,
    field,
    login,
    reveal,
    upload,
)
from webstudy import USERS
from webstudy.app import create_app
from webstudy.auth import Auth


def test_rating_is_account_bound_and_concurrent_submissions_only_count_once(tmp_path):
    first, second = USERS
    Auth(tmp_path / "auth.sqlite3").initialize(PASSWORDS)
    with browsers(create_app(tmp_path)) as (a, b):
        assert a.get("/study", follow_redirects=False).headers["location"] == "/login"
        assert login(a, first, "wrong-password").status_code == 401
        assert login(a, first).status_code == 303
        assert login(b, second).status_code == 303
        assert confirm(a, upload(a, b"id,word,meaning\nw1,pomelo,citrus\n")).is_success

        question = a.get("/study")
        token = field(question, "token")
        assert token == field(a.get("/study"), "token")
        assert "citrus" not in question.text

        assert (
            b.post(
                "/study/answer",
                data={
                    "csrf": field(b.get("/study"), "csrf"),
                    "token": token,
                    "rating": "easy",
                },
            ).status_code
            == 409
        )
        assert (
            a.post(
                "/study/answer",
                data={
                    "csrf": "not-the-session-token",
                    "token": token,
                    "rating": "easy",
                },
            ).status_code
            == 403
        )
        assert (
            a.post(
                "/study/answer",
                data={
                    "csrf": field(question, "csrf"),
                    "token": token,
                    "rating": "easy",
                },
            ).status_code
            == 409
        )

        shown = reveal(a, question)
        assert "citrus" in shown.text

        def answer():
            return a.post(
                "/study/answer",
                data={"csrf": field(shown, "csrf"), "token": token, "rating": "easy"},
                follow_redirects=False,
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            submissions = [executor.submit(answer) for _ in range(2)]
            assert sorted(submission.result() for submission in submissions) == [
                303,
                409,
            ]

        assert "暂无到期卡片" in a.get("/study").text
        assert "pomelo" in b.get("/study").text
        assert (
            a.post(
                "/logout",
                data={"csrf": field(a.get("/study"), "csrf")},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert a.get("/vocabulary.csv").status_code == 401
        assert b.get("/vocabulary.csv").status_code == 200

    assert card_state(tmp_path, first)[1::2] == (1, 1)
    assert card_state(tmp_path, second)[1::2] == (0, 0)


def test_import_preview_is_session_bound_and_confirmation_updates_both_accounts(
    tmp_path,
):
    first, second = USERS
    Auth(tmp_path / "auth.sqlite3").initialize(PASSWORDS)
    with browsers(create_app(tmp_path)) as (a, b):
        assert login(a, first).status_code == login(b, second).status_code == 303
        initial = upload(a, b"id,word,meaning\nw1,pomelo,<old meaning>\n")
        assert "&lt;old meaning&gt;" in initial.text
        assert "<old meaning>" not in initial.text
        assert "暂无到期卡片" in a.get("/study").text
        assert "暂无到期卡片" in b.get("/study").text
        assert (
            b.post(
                "/imports/confirm",
                data={
                    "csrf": field(b.get("/study"), "csrf"),
                    "preview_id": field(initial, "preview_id"),
                },
            ).status_code
            == 404
        )
        assert confirm(a, initial).is_success
        a_old, b_old = a.get("/study"), b.get("/study")
        assert "&lt;old meaning&gt;" in reveal(a, a_old).text
        assert "&lt;old meaning&gt;" in reveal(b, b_old).text

        updated = upload(b, b"id,word,meaning\nw1,pomelo,new meaning\n")
        assert "new meaning" in updated.text
        assert confirm(b, updated).is_success
        assert reveal(a, a_old).status_code == 409
        assert reveal(b, b_old).status_code == 409
        assert "new meaning" in reveal(a, a.get("/study")).text
        assert "new meaning" in reveal(b, b.get("/study")).text
        assert "w1,pomelo,new meaning," in a.get("/vocabulary.csv").text


def test_bulk_ambiguous_preview_can_be_confirmed(tmp_path):
    first, _ = USERS
    Auth(tmp_path / "auth.sqlite3").initialize(PASSWORDS)
    with browsers(create_app(tmp_path)) as (a, _):
        assert login(a, first).status_code == 303
        assert confirm(
            a, upload(a, b"id,word,meaning\nw1,bank,river shore\n")
        ).is_success
        repeated = b"word,meaning\n" + b"bank,other sense\n" * 999
        preview = upload(a, repeated)
        assert "待选择 999 条" in preview.text
        response = a.post(
            "/imports/confirm",
            data={
                "csrf": field(preview, "csrf"),
                "preview_id": field(preview, "preview_id"),
                **{f"choice_{line}": "skip" for line in range(2, 1001)},
            },
        )
        assert response.status_code == 200, response.text
        assert a.get("/vocabulary.csv").text.splitlines()[1:] == [
            "w1,bank,river shore,"
        ]

        rejected = upload(a, b"word,meaning\n" + b"bank,another sense\n" * 2001)
        assert rejected.status_code == 400
