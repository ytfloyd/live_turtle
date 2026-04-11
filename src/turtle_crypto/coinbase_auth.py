"""
Coinbase Advanced Trade JWT signing (CDP keys, ES256).

This module builds short-lived JWTs using the CDP API key format. It is
deliberately minimal: one function that reads the key file, one function that
builds a signed JWT for a given HTTP method + request path. The JWT is then
sent as `Authorization: Bearer <jwt>`.

Reference: https://docs.cdp.coinbase.com/advanced-trade/docs/rest-api-auth

JWT shape
---------
header:
    {"alg": "ES256", "kid": <key name>, "nonce": <random hex>, "typ": "JWT"}
payload:
    {"sub": <key name>,
     "iss": "cdp",
     "nbf": <now>,
     "exp": <now + 120>,
     "uri": "<METHOD> api.coinbase.com/api/v3/brokerage/<path>"}

The `uri` claim must NOT include a scheme, MUST NOT include a query string,
and MUST use the exact HTTP method uppercased.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import jwt
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from turtle_crypto.config import COINBASE_API_HOST, JWT_LIFETIME_SEC

JWT_ALGORITHM: Final[str] = "ES256"
JWT_ISSUER: Final[str] = "cdp"


class CoinbaseAuthError(Exception):
    """Raised when the CDP key cannot be loaded or a JWT cannot be built."""


@dataclass(frozen=True)
class CDPKey:
    """A parsed CDP API key loaded from the portal-downloaded JSON."""

    name: str
    private_key_pem: str

    @classmethod
    def from_file(cls, path: str | Path) -> "CDPKey":
        key_path = Path(path).expanduser()
        if not key_path.is_file():
            raise CoinbaseAuthError(f"CDP key file not found: {key_path}")
        try:
            raw = key_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CoinbaseAuthError(f"CDP key file is not valid JSON: {key_path}") from exc

        if not isinstance(data, dict):
            raise CoinbaseAuthError(
                f"CDP key file must be a JSON object, got {type(data).__name__}"
            )

        name = data.get("name")
        private_key_pem = data.get("privateKey")
        if not isinstance(name, str) or not name:
            raise CoinbaseAuthError("CDP key file is missing the 'name' field")
        if not isinstance(private_key_pem, str) or "PRIVATE KEY" not in private_key_pem:
            raise CoinbaseAuthError(
                "CDP key file is missing a valid 'privateKey' PEM field"
            )
        # Parse the PEM immediately to fail fast on malformed keys.
        try:
            load_pem_private_key(
                private_key_pem.encode("utf-8"), password=None, backend=default_backend()
            )
        except (ValueError, TypeError) as exc:
            raise CoinbaseAuthError(f"CDP private key is not a valid PEM: {exc}") from exc

        return cls(name=name, private_key_pem=private_key_pem)


def format_jwt_uri(method: str, path: str) -> str:
    """
    Build the `uri` claim for a signed request.

    The method is uppercased. The path MUST be the request path only (starting
    with `/`) and MUST NOT include a query string. The final format is:
        "<METHOD> api.coinbase.com<path>"
    """
    if not method or not method.strip():
        raise CoinbaseAuthError("method must be a non-empty string")
    if not path.startswith("/"):
        raise CoinbaseAuthError(f"path must start with '/', got: {path!r}")
    if "?" in path:
        raise CoinbaseAuthError(
            f"uri claim must not include a query string, got: {path!r}"
        )
    return f"{method.upper()} {COINBASE_API_HOST}{path}"


def build_jwt(
    cdp_key: CDPKey,
    method: str,
    path: str,
    *,
    now: int | None = None,
    nonce: str | None = None,
) -> str:
    """
    Build a signed ES256 JWT for a Coinbase Advanced Trade REST request.

    `now` and `nonce` are injectable for deterministic tests.
    """
    issued_at = int(time.time()) if now is None else int(now)
    nonce_hex = secrets.token_hex(16) if nonce is None else nonce

    claims = {
        "sub": cdp_key.name,
        "iss": JWT_ISSUER,
        "nbf": issued_at,
        "exp": issued_at + JWT_LIFETIME_SEC,
        "uri": format_jwt_uri(method, path),
    }
    headers = {
        "kid": cdp_key.name,
        "nonce": nonce_hex,
        "typ": "JWT",
    }
    token = jwt.encode(
        claims,
        cdp_key.private_key_pem,
        algorithm=JWT_ALGORITHM,
        headers=headers,
    )
    # pyjwt returns str on 2.x. Guard against the historical bytes return.
    if isinstance(token, bytes):
        token = token.decode("ascii")
    return token


def bearer_header(token: str) -> dict[str, str]:
    """Return the Authorization header dict for a given JWT."""
    return {"Authorization": f"Bearer {token}"}
