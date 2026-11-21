# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from webstudy import USERS
from webstudy.anki_adapter import StudyCollection
from webstudy.catalog import Catalog

log = logging.getLogger(__name__)


class _UserCollection:
    def __init__(self, user: str, path: Path, catalog: Catalog) -> None:
        self.user = user
        self.path = path
        self.catalog = catalog
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"anki-{user}"
        )
        self.collection: StudyCollection | None = None

    def catch_up(self) -> int:
        applied = self.catalog.delivered_version(self.user)
        if self.collection is None:
            if applied and not self.path.exists():
                self.catalog.record_delivery(self.user, 0)
                applied = 0
            self.collection = StudyCollection(self.path)
        if self.catalog.version() == applied:
            return applied
        version, entries = self.catalog.snapshot()
        for entry in entries:
            self.collection.upsert(entry.id, entry.word, entry.meaning, entry.example)
        # Never advertise a version until every entry is safely in Anki.
        self.catalog.record_delivery(self.user, version)
        return version

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
