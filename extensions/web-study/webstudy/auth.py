# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import scrypt, sha256
from pathlib import Path

from webstudy import USERS

LOGIN_TTL = 10 * 60
SESSION_TTL = 12 * 60 * 60


@dataclass(frozen=True)
class Session:
    id: str
    user: str | None
    csrf: str

    def accepts_csrf(self, value: str) -> bool:
        return secrets.compare_digest(self.csrf.encode(), value.encode())


def _hash_password(password: str, salt: bytes) -> bytes:
    return scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)


def _session_id(token: str) -> str:
    return sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        with self._connection() as db:
            db.executescript("""
                create table if not exists accounts (
                    user text primary key,
                    salt blob not null,
                    password_hash blob not null
                );
                create table if not exists sessions (
                    id text primary key,
                    user text,
                    csrf text not null,
                    expires_at integer not null
                );
                create index if not exists sessions_by_expiry on sessions (expires_at);
            """)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self, passwords: dict[str, str]) -> None:
        if passwords.keys() != set(USERS):
            raise ValueError("Supply one password for each of A and B.")
        rows = []
        for user, password in passwords.items():
            if len(password) < 8 or len(password.encode()) > 1024:
                raise ValueError(
                    "Passwords must be at least 8 characters and at most 1024 bytes."
                )
            salt = secrets.token_bytes(16)
            rows.append((user, salt, _hash_password(password, salt)))
        with self._connection() as db:
            db.executemany(
                """
                insert into accounts values (?, ?, ?)
                on conflict (user) do update set
                    salt = excluded.salt, password_hash = excluded.password_hash
                """,
                rows,
            )
            db.execute("delete from sessions")

    def initialized(self) -> bool:
        with self._connection() as db:
            return {row[0] for row in db.execute("select user from accounts")} == set(
                USERS
            )

    def session(self, token: str | None) -> Session | None:
        if not token or len(token) > 128:
            return None
        with self._connection() as db:
            row = db.execute(
                "select id, user, csrf from sessions where id = ? and expires_at > ?",
                (_session_id(token), int(time.time())),
            ).fetchone()
            return Session(*row) if row else None

    def _new_session(
        self, user: str | None, old: Session | None = None
    ) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        session = Session(_session_id(token), user, secrets.token_urlsafe(32))
        now = int(time.time())
        expires = now + (SESSION_TTL if user is not None else LOGIN_TTL)
        with self._connection() as db:
            db.execute("delete from sessions where expires_at <= ?", (now,))
            if old is not None:
                db.execute("delete from sessions where id = ?", (old.id,))
            db.execute(
                "insert into sessions values (?, ?, ?, ?)",
                (session.id, session.user, session.csrf, expires),
            )
        return token, session

    def begin_login(self) -> tuple[str, Session]:
        return self._new_session(None)

    def access_session(self, user: str) -> tuple[str, Session]:
        """Create an authenticated session after Cloudflare Access proved user."""
        if user not in USERS:
            raise ValueError(f"Unknown account: {user}")
        return self._new_session(user)

    def login(
        self, old: Session, user: str, password: str
    ) -> tuple[str, Session] | None:
        if user not in USERS or len(password.encode()) > 1024:
            return None
        with self._connection() as db:
            row = db.execute(
                "select salt, password_hash from accounts where user = ?", (user,)
            ).fetchone()
        if row is None or not secrets.compare_digest(
            _hash_password(password, row[0]), row[1]
        ):
            return None
        return self._new_session(user, old)

    def logout(self, session: Session) -> None:
        with self._connection() as db:
            db.execute("delete from sessions where id = ?", (session.id,))
