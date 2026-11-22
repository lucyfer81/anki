# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import socket
import threading
from contextlib import closing, contextmanager

import httpx2
import pytest
import uvicorn

from tests.helpers import card_state, confirm, field, login, reveal, upload
from webstudy import USERS, cli
from webstudy.anki_adapter import StudyCollection
from webstudy.app import create_app


class ReadyServer(uvicorn.Server):
    def __init__(self, config):
        super().__init__(config)
        self.ready = threading.Event()

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        self.ready.set()


@contextmanager
def running_server(data_dir):
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = ReadyServer(
            uvicorn.Config(create_app(data_dir), log_level="error", ws="none")
        )
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, daemon=True
        )
        thread.start()
        try:
            assert server.ready.wait(10), "Local HTTP server did not start"
            assert server.started
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive()


def test_web_and_inbox_share_vocabulary_without_resetting_progress(
    tmp_path, monkeypatch, capsys
):
    first, second = USERS
    data = tmp_path / "data"
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "DATA", data)

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "words.csv").write_text("id,word,meaning\none,yuzu,new meaning\n")

    with (
        httpx2.Client(trust_env=False) as a,
        httpx2.Client(trust_env=False) as b,
    ):
        with running_server(data) as url:
            a.base_url = b.base_url = url
            assert login(a, first).status_code == login(b, second).status_code == 303
            assert confirm(
                a, upload(a, b"id,word,meaning\none,yuzu,old meaning\n")
            ).is_success
            shown = reveal(a, a.get("/study"))
            assert "old meaning" in shown.text
            assert (
                a.post(
                    "/study/answer",
                    data={
                        "csrf": field(shown, "csrf"),
                        "token": field(shown, "token"),
                        "rating": "easy",
                    },
                ).status_code
                == 303
            )
            original = {user: card_state(data, user) for user in USERS}
            assert [state[1::2] for state in original.values()] == [(1, 1), (0, 0)]
            assert "old meaning" in reveal(b, b.get("/study")).text

            monkeypatch.setattr("builtins.input", lambda prompt: "y")
            cli.main(["import-file", "inbox/words.csv", "--user", second, "--url", url])
            output = capsys.readouterr().out
            assert "old meaning" in output and "new meaning" in output
            assert "暂无到期卡片" in a.get("/study").text
            b_page = b.get("/study")
            assert "new meaning" in reveal(b, b_page).text

        with running_server(data) as url:
            a.base_url = b.base_url = url
            assert "暂无到期卡片" in a.get("/study").text
            assert reveal(b, b_page).status_code == 409
            assert "new meaning" in reveal(b, b.get("/study")).text
        assert {user: card_state(data, user) for user in USERS} == original
        for user in USERS:
            with closing(
                StudyCollection(data / "users" / user / "collection.anki2")
            ) as col:
                [nid] = col.col.find_notes("")
                assert col.col.get_note(nid)["Meaning"] == "new meaning"


def test_serve_refuses_to_open_data_already_locked(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DATA", tmp_path)

    def no_server(*args, **kwargs):
        pytest.fail("server started while the data directory was locked")

    monkeypatch.setattr(uvicorn, "run", no_server)
    with cli.data_lock(tmp_path):
        with pytest.raises(ValueError, match="已有进程"):
            cli.serve("127.0.0.1", 8765)
