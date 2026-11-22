# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from contextlib import ExitStack

from webstudy import USERS
from webstudy.anki_adapter import StudyCollection


def test_update_preserves_each_collections_card_and_progress(tmp_path):
    paths = [tmp_path / user / "collection.anki2" for user in USERS]
    with ExitStack() as stack:
        a, b = [open_collection(stack, path) for path in paths]
        for collection in (a, b):
            collection.upsert("w001", "recieve", "old meaning", "")

        pending = a.next_card()
        assert pending is not None
        a.answer(pending, "easy")
        original = [card_state(collection) for collection in (a, b)]
        assert [state[1] for state in original] == [1, 0]
        assert [state[3] for state in original] == [1, 0]

        for collection in (a, b):
            collection.upsert("w001", "receive", "new meaning", "<example>")
        assert [card_state(collection) for collection in (a, b)] == original

    with ExitStack() as stack:
        a, b = [open_collection(stack, path) for path in paths]
        assert [card_state(collection) for collection in (a, b)] == original
        assert a.next_card() is None
        pending = b.next_card()
        assert pending is not None
        assert (pending.word, pending.meaning, pending.example) == (
            "receive",
            "new meaning",
            "<example>",
        )


def test_note_text_round_trips_without_activating_anki_markup(tmp_path):
    with ExitStack() as stack:
        collection = open_collection(stack, tmp_path / "collection.anki2")
        collection.upsert("a:b/1", "<b>word</b>", "[sound:a.mp3]", "one\ntwo")
        pending = collection.next_card()
        assert pending is not None
        assert (pending.word, pending.meaning, pending.example) == (
            "<b>word</b>",
            "[sound:a.mp3]",
            "one\ntwo",
        )
        [nid] = collection.col.find_notes("")
        note = collection.col.get_note(nid)
        assert "<b>" not in note["Word"]
        assert "[sound:" not in note["Meaning"]


def open_collection(stack, path):
    collection = StudyCollection(path)
    stack.callback(collection.close)
    return collection


def card_state(collection):
    db = collection.col.db
    assert db is not None
    [card] = db.all("select id, reps, due from cards")
    return (*card, db.scalar("select count(*) from revlog where cid = ?", card[0]))
