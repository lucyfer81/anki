# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Automatic sign-in behind Cloudflare Access, one hostname per account."""

import json
from dataclasses import dataclass
from pathlib import Path

import jwt

from webstudy import USERS


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


def user_for_host(config: AccessConfig, host: str) -> str | None:
    """Map xct.anki.aizhi.app -> xct, ignoring any port and letter case."""
    hostname = host.split(":", maxsplit=1)[0].strip().lower()
    suffix = "." + config.domain
    if not hostname.endswith(suffix):
        return None
    prefix = hostname[: -len(suffix)]
    return prefix if prefix in USERS else None


_clients: dict[str, jwt.PyJWKClient] = {}


def _jwk_client(config: AccessConfig) -> jwt.PyJWKClient:
    client = _clients.get(config.certs_url)
    if client is None:
        client = jwt.PyJWKClient(config.certs_url)
        _clients[config.certs_url] = client
    return client


def valid_assertion(config: AccessConfig, token: str) -> bool:
    """Verify a Cloudflare Access JWT against the team's published keys."""
    try:
        key = _jwk_client(config).get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=config.aud)
    except (jwt.PyJWTError, OSError):
        return False
    return claims.get("iss") == config.issuer
