# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from contextlib import closing

import pytest

from webstudy import USERS
from webstudy.anki_adapter import StudyCollection
from webstudy.catalog import Catalog
from webstudy.service import StudyService


@pytest.mark.parametrize("interruption", ["before_b_write", "after_b_write"])
def test_restart_retries_incomplete_delivery_without_duplicating_cards(
    tmp_path, monkeypatch, interruption
):
    first_user, second_user = USERS
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    first = catalog.preview(b"id,word,meaning\nw001,word,first\n", "csv")
    assert catalog.confirm(first.id, {}) == 1
    with StudyService(tmp_path, catalog) as service:
        assert service.catch_up_all() == ()

    a_path, b_path = [tmp_path / "users" / user / "collection.anki2" for user in USERS]
    with closing(StudyCollection(a_path)) as a:
        pending = a.next_card()
        assert pending is not None
        a.answer(pending, "easy")
    a_original = card_state(a_path)
    b_original = card_state(b_path)
    assert (a_original[1], b_original[1]) == (1, 0)

    update = catalog.preview(b"id,word,meaning\nw001,word,updated\n", "csv")
    assert catalog.confirm(update.id, {}) == 2
    with monkeypatch.context() as patch:
        if interruption == "before_b_write":
            upsert = StudyCollection.upsert

            def fail_b_upsert(collection, *args):
                if collection.col.path == str(b_path):
                    raise OSError("interrupted before B write")
                return upsert(collection, *args)

            patch.setattr(StudyCollection, "upsert", fail_b_upsert)
        else:
            record = catalog.record_delivery

            def fail_b_record(user, version):
                if user == second_user:
                    raise OSError("interrupted after B write")
                return record(user, version)

            patch.setattr(catalog, "record_delivery", fail_b_record)

        with StudyService(tmp_path, catalog) as service:
            assert service.catch_up(first_user) == 2
            assert service.catch_up_all() == (second_user,)
            assert service.pending_delivery() == (second_user,)

    with closing(StudyCollection(b_path)) as b:
        pending = b.next_card()
        assert pending is not None
        assert pending.meaning == (
            "first" if interruption == "before_b_write" else "updated"
        )

    catalog = Catalog(tmp_path / "catalog.sqlite3")
    with StudyService(tmp_path, catalog) as restarted:
        assert restarted.catch_up_all() == ()
        assert restarted.pending_delivery() == ()
    assert card_state(a_path) == a_original
    assert card_state(b_path) == b_original
    with closing(StudyCollection(b_path)) as b:
        pending = b.next_card()
        assert pending is not None
        assert pending.meaning == "updated"


def card_state(path):
    with closing(StudyCollection(path)) as collection:
        [card] = collection.col.db.all("select id, reps, due from cards")
        return card
