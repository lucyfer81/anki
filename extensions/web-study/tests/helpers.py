# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import re
import sqlite3
from contextlib import closing, contextmanager

from fastapi.testclient import TestClient


@contextmanager
def browsers(app):
    # Simulate clients on the server machine; entry links trust them.
    with (
        TestClient(app, client=("127.0.0.1", 50000)) as a,
        closing(TestClient(app, client=("127.0.0.1", 50001))) as b,
    ):
        yield a, b


def field(response, name):
    assert response.status_code == 200, response.text
    match = re.search(rf'name="{name}" value="([^"]+)"', response.text)
    assert match is not None, (name, response.text)
    return match[1]


def login(client, user):
    """Open an entry link, which signs the browser into the account."""
    response = client.get(f"/{user}", follow_redirects=False)
    assert response.status_code == 303, response.text
    return response


def upload(client, data, name="words.csv"):
    return client.post(
        "/imports/preview",
        data={"csrf": field(client.get("/study"), "csrf")},
        files={"file": (name, data)},
    )


def confirm(client, preview):
    return client.post(
        "/imports/confirm",
        data={
            "csrf": field(preview, "csrf"),
            "preview_id": field(preview, "preview_id"),
        },
    )


def reveal(client, page):
    return client.post(
        "/study/reveal",
        data={"csrf": field(page, "csrf"), "token": field(page, "token")},
    )


def card_state(data_dir, user):
    path = data_dir / "users" / user / "collection.anki2"
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        [card] = db.execute("select id, reps, due from cards").fetchall()
        history = db.execute("select count(*) from revlog").fetchone()[0]
        return (*card, history)
