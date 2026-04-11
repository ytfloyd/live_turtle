"""
Unit tests for coinbase_auth.

These tests generate an in-memory ES256 keypair, write a fake CDP key file,
build a JWT, and verify the resulting token structure (claims, header,
signature verification against the public key).

No network. No secrets. Safe to run offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from turtle_crypto.coinbase_auth import (
    CDPKey,
    CoinbaseAuthError,
    build_jwt,
    format_jwt_uri,
)


@pytest.fixture()
def fake_cdp_key(tmp_path: Path) -> tuple[Path, str, str]:
    """Generate a test ES256 key and write a CDP-format JSON file."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")

    key_name = "organizations/org-abc/apiKeys/key-123"
    key_file = tmp_path / "cdp_key.json"
    key_file.write_text(
        json.dumps({"name": key_name, "privateKey": pem}), encoding="utf-8"
    )
    return key_file, key_name, public_pem


def test_format_jwt_uri_basic() -> None:
    assert (
        format_jwt_uri("get", "/api/v3/brokerage/accounts")
        == "GET api.coinbase.com/api/v3/brokerage/accounts"
    )


def test_format_jwt_uri_rejects_query_string() -> None:
    with pytest.raises(CoinbaseAuthError, match="query string"):
        format_jwt_uri("GET", "/api/v3/brokerage/accounts?limit=5")


def test_format_jwt_uri_rejects_missing_leading_slash() -> None:
    with pytest.raises(CoinbaseAuthError, match="must start with"):
        format_jwt_uri("GET", "api/v3/brokerage/accounts")


def test_format_jwt_uri_rejects_empty_method() -> None:
    with pytest.raises(CoinbaseAuthError, match="non-empty"):
        format_jwt_uri("", "/api/v3/brokerage/accounts")


def test_cdp_key_from_file_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CoinbaseAuthError, match="not found"):
        CDPKey.from_file(tmp_path / "nope.json")


def test_cdp_key_from_file_rejects_non_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not json at all")
    with pytest.raises(CoinbaseAuthError, match="not valid JSON"):
        CDPKey.from_file(bad)


def test_cdp_key_from_file_rejects_missing_fields(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"name": "foo"}))
    with pytest.raises(CoinbaseAuthError, match="privateKey"):
        CDPKey.from_file(bad)

    bad2 = tmp_path / "bad2.json"
    bad2.write_text(json.dumps({"privateKey": "-----BEGIN PRIVATE KEY-----"}))
    with pytest.raises(CoinbaseAuthError, match="name"):
        CDPKey.from_file(bad2)


def test_cdp_key_from_file_rejects_invalid_pem(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {"name": "organizations/o/apiKeys/k", "privateKey": "-----BEGIN PRIVATE KEY-----\nnot-real\n-----END PRIVATE KEY-----"}
        )
    )
    with pytest.raises(CoinbaseAuthError, match="valid PEM"):
        CDPKey.from_file(bad)


def test_build_jwt_claims_and_headers(fake_cdp_key: tuple[Path, str, str]) -> None:
    key_file, key_name, public_pem = fake_cdp_key
    cdp_key = CDPKey.from_file(key_file)
    token = build_jwt(
        cdp_key,
        "POST",
        "/api/v3/brokerage/orders",
        now=1_700_000_000,
        nonce="deadbeef" * 4,
    )

    # Decode WITHOUT signature verification to inspect claims.
    unverified_header = jwt.get_unverified_header(token)
    assert unverified_header["alg"] == "ES256"
    assert unverified_header["kid"] == key_name
    assert unverified_header["nonce"] == "deadbeef" * 4
    assert unverified_header["typ"] == "JWT"

    # Verify the signature with the public key we generated above.
    # Disable exp/nbf verification so we can pin `now` to a historical value
    # and test the claims deterministically.
    decoded = jwt.decode(
        token,
        public_pem,
        algorithms=["ES256"],
        options={"verify_aud": False, "verify_exp": False, "verify_nbf": False},
    )
    assert decoded["sub"] == key_name
    assert decoded["iss"] == "cdp"
    assert decoded["nbf"] == 1_700_000_000
    assert decoded["exp"] == 1_700_000_000 + 120
    assert decoded["uri"] == "POST api.coinbase.com/api/v3/brokerage/orders"


def test_build_jwt_generates_fresh_nonce_each_call(
    fake_cdp_key: tuple[Path, str, str],
) -> None:
    key_file, _, _ = fake_cdp_key
    cdp_key = CDPKey.from_file(key_file)
    t1 = build_jwt(cdp_key, "GET", "/api/v3/brokerage/accounts")
    t2 = build_jwt(cdp_key, "GET", "/api/v3/brokerage/accounts")
    # Different nonces + different (or same) now produce different signatures.
    assert t1 != t2

    h1 = jwt.get_unverified_header(t1)
    h2 = jwt.get_unverified_header(t2)
    assert h1["nonce"] != h2["nonce"]
