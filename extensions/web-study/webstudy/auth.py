# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Cookie sessions bound to one of the preset accounts."""

import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from webstudy import USERS

SESSION_TTL = 12 * 60 * 60


@dataclass(frozen=True)
class Session:
    id: str
    user: str
    csrf: str

    def accepts_csrf(self, value: str) -> bool:
        return secrets.compare_digest(self.csrf.encode(), value.encode())


def _session_id(token: str) -> str:
    return sha256(token.encode()).hexdigest()


class Auth:
    """Session storage; identities arrive via Cloudflare Access or local use."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        with self._connection() as db:
            db.executescript("""
                create table if not exists sessions (
                    id text primary key,
                    user text not null,
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

    def session(self, token: str | None) -> Session | None:
        if not token or len(token) > 128:
            return None
        with self._connection() as db:
            row = db.execute(
                "select id, user, csrf from sessions where id = ? and expires_at > ?",
                (_session_id(token), int(time.time())),
            ).fetchone()
            return Session(*row) if row else None

    def _new_session(self, user: str) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        session = Session(_session_id(token), user, secrets.token_urlsafe(32))
        now = int(time.time())
        with self._connection() as db:
            db.execute("delete from sessions where expires_at <= ?", (now,))
            db.execute(
                "insert into sessions values (?, ?, ?, ?)",
                (session.id, session.user, session.csrf, now + SESSION_TTL),
            )
        return token, session

    def access_session(self, user: str) -> tuple[str, Session]:
        """Create a session after entry proved which account to open."""
        if user not in USERS:
            raise ValueError(f"Unknown account: {user}")
        return self._new_session(user)
