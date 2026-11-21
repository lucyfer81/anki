# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import csv
import io
import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

COLUMNS = ("id", "word", "meaning", "example")
DELIMITERS = {"csv": ",", "tsv": "\t"}
MAX_AMBIGUOUS_ROWS = 2000


class InvalidImport(ValueError):
    pass


class StalePreview(InvalidImport):
    pass


class PreviewNotFound(InvalidImport):
    pass


@dataclass(frozen=True)
class Word:
    id: str
    word: str
    meaning: str
    example: str


@dataclass(frozen=True)
class ImportRow:
    line: int
    entry: Word
    before: Word | None
    candidates: tuple[Word, ...] = ()
    example_provided: bool = True


@dataclass(frozen=True)
class Preview:
    id: str
    base_version: int
    rows: tuple[ImportRow, ...]
    previous_version: int | None = None

    @property
    def added(self) -> int:
        return sum(row.before is None and not row.candidates for row in self.rows)

    @property
    def updated(self) -> int:
        return sum(
            row.before is not None and row.before != row.entry for row in self.rows
        )


def _parse(data: bytes, format: str) -> Iterator[tuple[int, Word, bool]]:
    if format not in DELIMITERS:
        raise InvalidImport("Use a UTF-8 CSV or TSV file.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        line = data.count(b"\n", 0, exc.start) + 1
        raise InvalidImport(f"Line {line}: invalid UTF-8.") from exc

    reader = csv.reader(
        io.StringIO(text, newline=""), delimiter=DELIMITERS[format], strict=True
    )
    line = 1
    seen = set()
    try:
        header = [field.strip() for field in next(reader, [])]
        if not {"word", "meaning"} <= set(header) <= set(COLUMNS) or len(header) != len(
            set(header)
        ):
            raise InvalidImport("Line 1: need word, meaning and optional id, example.")
        while True:
            line = reader.line_num + 1
            row = next(reader, None)
            if row is None:
                return
            if not row:
                continue
            if len(row) != len(header):
                raise InvalidImport(f"Line {line}: wrong number of columns.")
            values = dict(zip(header, (field.strip() for field in row), strict=True))
            word, meaning = values["word"], values["meaning"]
            if not word or not meaning:
                raise InvalidImport(f"Line {line}: word and meaning are required.")
            if any(
                ord(char) < 32 and char not in "\t\n\r"
                for value in values.values()
                for char in value
            ):
                raise InvalidImport(f"Line {line}: unsupported control character.")
            source_id = values.get("id", "")
            if source_id and source_id in seen:
                raise InvalidImport(f"Line {line}: duplicate ID.")
            if source_id:
                seen.add(source_id)
            yield (
                line,
                Word(source_id, word, meaning, values.get("example", "")),
                "example" in values,
            )
    except csv.Error as exc:
        raise InvalidImport(f"Line {line}: {exc}.") from exc


def _decode_rows(payload: str) -> Iterator[ImportRow]:
    for row in json.loads(payload):
        yield ImportRow(
            row["line"],
            Word(**row["entry"]),
            Word(**row["before"]) if row["before"] is not None else None,
            tuple(Word(**entry) for entry in row["candidates"]),
            row.get("example_provided", True),
        )


class Catalog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.executescript("""
                create table if not exists catalog_state (
                    singleton integer primary key check (singleton = 1),
                    version integer not null
                );
                insert or ignore into catalog_state values (1, 0);
                create table if not exists words (
                    id text primary key,
                    word text not null,
                    meaning text not null,
                    example text not null
                );
                create table if not exists previews (
                    id text primary key,
                    owner text not null,
                    fingerprint text not null,
                    base_version integer not null,
                    rows_json text not null,
                    confirmed_version integer
                );
                create index if not exists previews_by_file
                    on previews (fingerprint, confirmed_version);
                create table if not exists delivered (
                    user text primary key,
                    version integer not null
                );
            """)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _version(db: sqlite3.Connection) -> int:
        return db.execute("select version from catalog_state").fetchone()[0]

    @staticmethod
    def _words(db: sqlite3.Connection) -> list[Word]:
        return [
            Word(*row)
            for row in db.execute(
                "select id, word, meaning, example from words order by id"
            )
        ]

    def version(self) -> int:
        with self._connection() as db:
            return self._version(db)

    def snapshot(self) -> tuple[int, list[Word]]:
        with self._connection() as db:
            db.execute("begin")
            return self._version(db), self._words(db)

    def delivered_version(self, user: str) -> int:
        with self._connection() as db:
            row = db.execute(
                "select version from delivered where user = ?", (user,)
            ).fetchone()
            return 0 if row is None else row[0]

    def record_delivery(self, user: str, version: int) -> None:
        with self._connection() as db:
            db.execute(
                """
                insert into delivered values (?, ?)
                on conflict (user) do update set version = excluded.version
                """,
                (user, version),
            )

    def export_csv(self) -> str:
        _, entries = self.snapshot()
        out = io.StringIO(newline="")
        writer = csv.DictWriter(out, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(asdict(entry) for entry in entries)
        return out.getvalue()

    @staticmethod
    def _previous(db: sqlite3.Connection, fingerprint: str) -> int | None:
        return db.execute(
            "select min(confirmed_version) from previews where fingerprint = ?",
            (fingerprint,),
        ).fetchone()[0]

    def preview(self, data: bytes, format: str, *, owner: str = "") -> Preview:
        parsed = list(_parse(data, format))
        if not parsed:
            raise InvalidImport("The file contains no vocabulary rows.")
        fingerprint = sha256(format.encode() + b"\0" + data).hexdigest()
        preview_id = uuid4().hex
        with self._connection() as db:
            db.execute("begin immediate")
            version = self._version(db)
            previous = self._previous(db, fingerprint)
            rows = []
            if previous is None:
                words = self._words(db)
                by_id = {word.id: word for word in words}
                by_word: dict[str, list[Word]] = defaultdict(list)
                for word in words:
                    by_word[word.word].append(word)
                ambiguous = 0
                for line, entry, example_provided in parsed:
                    if entry.id:
                        before = by_id.get(entry.id)
                        if before is not None and not example_provided:
                            entry = replace(entry, example=before.example)
                        rows.append(
                            ImportRow(
                                line, entry, before, example_provided=example_provided
                            )
                        )
                    else:
                        candidates = tuple(by_word.get(entry.word, ()))
                        if candidates:
                            ambiguous += 1
                            if ambiguous > MAX_AMBIGUOUS_ROWS:
                                raise InvalidImport(
                                    f"At most {MAX_AMBIGUOUS_ROWS} rows can need a choice. "
                                    "Split the file or provide IDs."
                                )
                        rows.append(
                            ImportRow(
                                line,
                                replace(entry, id=str(uuid4())),
                                None,
                                candidates,
                                example_provided,
                            )
                        )
            db.execute(
                "insert into previews values (?, ?, ?, ?, ?, null)",
                (
                    preview_id,
                    owner,
                    fingerprint,
                    version,
                    json.dumps([asdict(row) for row in rows]),
                ),
            )
        return Preview(preview_id, version, tuple(rows), previous)

    def confirm(
        self, preview_id: str, resolutions: dict[int, str], *, owner: str = ""
    ) -> int:
        with self._connection() as db:
            db.execute("begin immediate")
            preview = db.execute(
                """
                select fingerprint, base_version, rows_json, confirmed_version
                from previews where id = ? and owner = ?
                """,
                (preview_id, owner),
            ).fetchone()
            if preview is None:
                raise PreviewNotFound("Preview not found. Upload the file again.")
            fingerprint, base, payload, confirmed = preview
            if confirmed is not None:
                return confirmed
            version = self._version(db)
            if base != version:
                raise StalePreview(
                    "The vocabulary changed. Upload the file again to preview it."
                )
            previous = self._previous(db, fingerprint)
            if previous is not None:
                version = previous
            else:
                changes = _resolve(tuple(_decode_rows(payload)), resolutions)
                if changes:
                    db.executemany(
                        """
                        insert into words (id, word, meaning, example) values (?, ?, ?, ?)
                        on conflict (id) do update set word = excluded.word,
                        meaning = excluded.meaning, example = excluded.example
                        """,
                        (
                            (entry.id, entry.word, entry.meaning, entry.example)
                            for entry in changes
                        ),
                    )
                    version += 1
                    db.execute("update catalog_state set version = ?", (version,))
            db.execute(
                "update previews set confirmed_version = ? where id = ?",
                (version, preview_id),
            )
            return version


def _resolve(rows: tuple[ImportRow, ...], resolutions: dict[int, str]) -> list[Word]:
    unexpected = resolutions.keys() - {row.line for row in rows if row.candidates}
    if unexpected:
        raise InvalidImport(f"Line {min(unexpected)}: no choice is needed.")
    changes = []
    selected_ids = set()
    for row in rows:
        entry, before = row.entry, row.before
        if row.candidates:
            choice = resolutions.get(row.line, "")
            if choice == "skip":
                continue
            if choice != "new":
                before = next(
                    (word for word in row.candidates if choice == f"update:{word.id}"),
                    None,
                )
                if before is None:
                    raise InvalidImport(
                        f"Line {row.line}: choose an update, new sense or skip."
                    )
                entry = replace(
                    entry,
                    id=before.id,
                    example=entry.example if row.example_provided else before.example,
                )
        if entry.id in selected_ids:
            raise InvalidImport(f"Line {row.line}: multiple rows target the same ID.")
        selected_ids.add(entry.id)
        if entry != before:
            changes.append(entry)
    return changes
