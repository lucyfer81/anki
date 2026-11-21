# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from webstudy import USERS
from webstudy.anki_adapter import RATINGS, PendingCard, StudyCollection
from webstudy.catalog import Catalog

log = logging.getLogger(__name__)


class StaleTurn(ValueError):
    pass


@dataclass(frozen=True)
class StudyCard:
    token: str
    word: str
    meaning: str | None
    example: str | None


@dataclass
class _Turn:
    token: str
    card: PendingCard
    revealed: bool = False

    def view(self) -> StudyCard:
        return StudyCard(
            self.token,
            self.card.word,
            self.card.meaning if self.revealed else None,
            self.card.example if self.revealed else None,
        )


class _UserCollection:
    def __init__(self, user: str, path: Path, catalog: Catalog) -> None:
        self.user = user
        self.path = path
        self.catalog = catalog
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"anki-{user}"
        )
        self.collection: StudyCollection | None = None
        self.turn: _Turn | None = None

    def catch_up(self) -> int:
        applied = self.catalog.delivered_version(self.user)
        if self.collection is None:
            if applied and not self.path.exists():
                self.catalog.record_delivery(self.user, 0)
                applied = 0
            self.collection = StudyCollection(self.path)
        if self.catalog.version() == applied:
            return applied
        self.turn = None
        version, entries = self.catalog.snapshot()
        for entry in entries:
            self.collection.upsert(entry.id, entry.word, entry.meaning, entry.example)
        # Never advertise a version until every entry is safely in Anki.
        self.catalog.record_delivery(self.user, version)
        return version

    def next_card(self) -> StudyCard | None:
        self.catch_up()
        assert self.collection is not None
        if self.turn is None and (card := self.collection.next_card()) is not None:
            self.turn = _Turn(secrets.token_urlsafe(32), card)
        return self.turn.view() if self.turn is not None else None

    def _current_turn(self, token: str) -> _Turn:
        self.catch_up()
        if self.turn is None or self.turn.token != token:
            raise StaleTurn("这道题已失效，请重新取题。")
        return self.turn

    def reveal(self, token: str) -> StudyCard:
        turn = self._current_turn(token)
        turn.revealed = True
        return turn.view()

    def answer(self, token: str, rating: str) -> None:
        turn = self._current_turn(token)
        if not turn.revealed:
            raise StaleTurn("请先翻卡再评分。")
        # Consume before calling Anki, even if the caller loses the response.
        self.turn = None
        assert self.collection is not None
        self.collection.answer(turn.card, rating)

    def close(self) -> None:
        if self.collection is not None:
            self.collection.close()
            self.collection = None


class StudyService:
    """Routes every operation on a collection to its owning thread."""

    def __init__(self, data_dir: Path, catalog: Catalog) -> None:
        self.catalog = catalog
        self._users = {
            user: _UserCollection(
                user, data_dir / "users" / user / "collection.anki2", catalog
            )
            for user in USERS
        }
        self._closed = False

    def __enter__(self) -> "StudyService":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def catch_up(self, user: str) -> int:
        worker = self._users[user]
        return worker.executor.submit(worker.catch_up).result()

    def next_card(self, user: str) -> StudyCard | None:
        worker = self._users[user]
        return worker.executor.submit(worker.next_card).result()

    def reveal(self, user: str, token: str) -> StudyCard:
        worker = self._users[user]
        return worker.executor.submit(worker.reveal, token).result()

    def answer(self, user: str, token: str, rating: str) -> None:
        if rating not in RATINGS:
            raise ValueError("Unknown rating.")
        worker = self._users[user]
        worker.executor.submit(worker.answer, token, rating).result()

    def catch_up_all(self) -> tuple[str, ...]:
        futures = {
            user: worker.executor.submit(worker.catch_up)
            for user, worker in self._users.items()
        }
        failures = []
        for user, future in futures.items():
            try:
                future.result()
            except Exception:
                log.exception("Vocabulary delivery failed for user %s", user)
                failures.append(user)
        return tuple(failures)

    def pending_delivery(self) -> tuple[str, ...]:
        version = self.catalog.version()
        return tuple(
            user for user in USERS if self.catalog.delivered_version(user) < version
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        futures = [
            worker.executor.submit(worker.close) for worker in self._users.values()
        ]
        try:
            for future in futures:
                future.result()
        finally:
            for worker in self._users.values():
                worker.executor.shutdown()
