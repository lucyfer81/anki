# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Automatic sign-in behind Cloudflare Access, one entry link per account."""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import jwt

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccessConfig:
    team: str
    aud: str
    domain: str
    certs_url: str

    @property
    def issuer(self) -> str:
        return f"https://{self.team}.cloudflareaccess.com"


def load_access_config(path: Path) -> AccessConfig | None:
    """Read data/access.json; a broken or missing file disables auto sign-in."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        domain = payload["domain"].strip().strip(".").lower()
        return AccessConfig(
            team=payload["team"].strip().lower(),
            aud=payload["aud"].strip(),
            domain=domain,
            certs_url=payload.get("certs", f"https://{domain}/cdn-cgi/access/certs"),
        )
    except (OSError, ValueError, KeyError):
        return None


_clients: dict[str, jwt.PyJWKClient] = {}


def _jwk_client(config: AccessConfig) -> jwt.PyJWKClient:
    client = _clients.get(config.certs_url)
    if client is None:
        # Cloudflare rejects the default Python-urllib agent with 403.
        client = jwt.PyJWKClient(
            config.certs_url, headers={"User-Agent": "webstudy/0.1"}
        )
        _clients[config.certs_url] = client
    return client


def valid_assertion(config: AccessConfig, token: str) -> bool:
    """Verify a Cloudflare Access JWT against the team's published keys."""
    try:
        key = _jwk_client(config).get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=config.aud)
    except (jwt.PyJWTError, OSError) as exc:
        log.warning("Access assertion rejected: %s", exc)
        return False
    if claims.get("iss") != config.issuer:
        log.warning(
            "Access assertion issuer %r does not match %r",
            claims.get("iss"),
            config.issuer,
        )
        return False
    return True
