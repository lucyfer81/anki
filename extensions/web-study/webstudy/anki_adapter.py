# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from dataclasses import dataclass
from html import escape, unescape
from pathlib import Path

# Start with Collection: Anki's generated hooks import Card during initialization.
import anki.collection
from anki.cards import Card
from anki.models import NotetypeDict, NotetypeId
from anki.notes import NoteId
from anki.scheduler.v3 import CardAnswer, SchedulingStates

FIELDS = ("SourceID", "Word", "Meaning", "Example")
RATINGS = {
    "again": CardAnswer.AGAIN,
    "hard": CardAnswer.HARD,
    "good": CardAnswer.GOOD,
    "easy": CardAnswer.EASY,
}


def _encode(text: str) -> str:
    # Keep brackets out of Anki's own sound/LaTeX markup parsers, too.
    return (
        escape(text).replace("[", "&#91;").replace("]", "&#93;").replace("\n", "<br>")
    )


def _decode(text: str) -> str:
    return unescape(text.replace("<br>", "\n"))


@dataclass(frozen=True)
class PendingCard:
    word: str
    meaning: str
    example: str
    _card: Card
    _states: SchedulingStates


class StudyCollection:
    """A dedicated Anki collection, used only on its owning thread."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.col = anki.collection.Collection(str(path))
        try:
            self._notetype = self._get_or_create_notetype()
            self._deck = self.col.decks.id("Web Study")
            assert self._deck is not None
            if self.col.decks.get_current_id() != self._deck:
                self.col.decks.set_current(self._deck)
        except BaseException:
            self.col.close()
            raise

    def _get_or_create_notetype(self) -> NotetypeDict:
        models = self.col.models
        if existing := models.by_name("Web Study Vocabulary"):
            return existing
        notetype = models.new("Web Study Vocabulary")
        for field in FIELDS:
            models.add_field(notetype, models.new_field(field))
        models.set_sort_index(notetype, 1)
        template = models.new_template("Word")
        template["qfmt"] = "{{Word}}"
        template["afmt"] = (
            '{{FrontSide}}<hr id="answer">{{Meaning}}'
            "{{#Example}}<p>{{Example}}</p>{{/Example}}"
        )
        models.add_template(notetype, template)
        created = models.get(NotetypeId(models.add_dict(notetype).id))
        assert created is not None
        return created

    def upsert(self, source_id: str, word: str, meaning: str, example: str) -> None:
        if not source_id or not word or not meaning:
            raise ValueError("SourceID, Word and Meaning must not be empty")
        # ASCII entities preserve SourceID identity even if Anki normalizes Unicode.
        source = "".join(
            char if 32 <= ord(char) < 127 else f"&#{ord(char)};"
            for char in escape(source_id)
        )
        db = self.col.db
        assert db is not None
        nid = db.scalar(
            """
            select id from notes where mid = ?
              and substr(flds, 1, instr(flds, char(31)) - 1) = ?
            """,
            self._notetype["id"],
            source,
        )
        note = (
            self.col.get_note(NoteId(nid))
            if nid is not None
            else self.col.new_note(self._notetype)
        )
        fields = [source, *map(_encode, (word, meaning, example))]
        if nid is not None and note.fields == fields:
            return
        for name, value in zip(FIELDS, fields, strict=True):
            note[name] = value
        if nid is None:
            self.col.add_note(note, self._deck)
        else:
            self.col.update_note(note)

    def next_card(self) -> PendingCard | None:
        queued = self.col.sched.get_queued_cards(fetch_limit=1).cards
        if not queued:
            return None
        item = queued[0]
        card = Card(self.col, backend_card=item.card)
        card.start_timer()
        note = card.note()
        return PendingCard(
            word=_decode(note["Word"]),
            meaning=_decode(note["Meaning"]),
            example=_decode(note["Example"]),
            _card=card,
            _states=item.states,
        )

    def answer(self, pending: PendingCard, rating: str) -> None:
        answer = self.col.sched.build_answer(
            card=pending._card, states=pending._states, rating=RATINGS[rating]
        )
        self.col.sched.answer_card(answer)

    def close(self) -> None:
        self.col.close()
