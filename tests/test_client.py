"""
Unit tests for coinbase_client.

These tests cover:
  - Decimal-to-string conversion (no sci notation, no trailing zeros, no float)
  - Order payload construction for both order types (via a mocked session)
  - Response validation rejects missing/malformed JSON shapes

No network. All HTTP calls are mocked.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from turtle_crypto.coinbase_auth import CDPKey
from turtle_crypto.coinbase_client import (
    CoinbaseClient,
    CoinbaseClientError,
    CoinbaseHTTPError,
    _decimal_to_str,
)


# -- Decimal → string --------------------------------------------------------


def test_decimal_to_str_strips_trailing_zeros() -> None:
    assert _decimal_to_str(Decimal("1.2300")) == "1.23"
    assert _decimal_to_str(Decimal("10.0")) == "10"
    assert _decimal_to_str(Decimal("0.00001000")) == "0.00001"


def test_decimal_to_str_no_scientific_notation() -> None:
    assert _decimal_to_str(Decimal("1E-8")) == "0.00000001"
    assert _decimal_to_str(Decimal("1E+3")) == "1000"


def test_decimal_to_str_rejects_float() -> None:
    with pytest.raises(CoinbaseClientError, match="must be Decimal"):
        _decimal_to_str(1.23)  # type: ignore[arg-type]


def test_decimal_to_str_handles_zero() -> None:
    assert _decimal_to_str(Decimal("0")) == "0"


# -- Fixtures ----------------------------------------------------------------


@pytest.fixture()
def cdp_key(tmp_path: Path) -> CDPKey:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    key_file = tmp_path / "cdp.json"
    key_file.write_text(
        json.dumps({"name": "organizations/o/apiKeys/k", "privateKey": pem})
    )
    return CDPKey.from_file(key_file)


def _mock_session(status: int, body: dict | list | str) -> MagicMock:
    session = MagicMock(spec=requests.Session)
    session.headers = {}
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    if isinstance(body, str):
        resp.text = body
        resp.json.side_effect = ValueError("not json")
    else:
        resp.text = json.dumps(body)
        resp.json.return_value = body
    session.get.return_value = resp
    session.post.return_value = resp
    return session


# -- HTTP error handling -----------------------------------------------------


def test_http_error_raises(cdp_key: CDPKey) -> None:
    session = _mock_session(400, {"error": "bad"})
    client = CoinbaseClient(cdp_key, session=session)
    with pytest.raises(CoinbaseHTTPError) as exc_info:
        client.get_accounts()
    assert exc_info.value.status == 400


def test_non_json_response_raises(cdp_key: CDPKey) -> None:
    session = _mock_session(200, "not json at all")
    client = CoinbaseClient(cdp_key, session=session)
    with pytest.raises(CoinbaseClientError, match="non-JSON"):
        client.get_accounts()


def test_missing_field_raises(cdp_key: CDPKey) -> None:
    session = _mock_session(200, {"not_accounts": []})
    client = CoinbaseClient(cdp_key, session=session)
    with pytest.raises(CoinbaseClientError, match="accounts"):
        client.get_accounts()


def test_public_endpoint_without_auth_works() -> None:
    session = _mock_session(200, {"products": [{"product_id": "BTC-USD"}]})
    client = CoinbaseClient(None, session=session)
    products = client.list_products_page(limit=250, offset=0)
    assert products == [{"product_id": "BTC-USD"}]


def test_private_endpoint_without_auth_raises() -> None:
    client = CoinbaseClient(None)
    with pytest.raises(CoinbaseClientError, match="requires CDP auth"):
        client.get_accounts()


# -- Order payload shape -----------------------------------------------------


def test_place_market_buy_payload(cdp_key: CDPKey) -> None:
    session = _mock_session(200, {"success": True, "order_id": "abc"})
    client = CoinbaseClient(cdp_key, session=session)
    client.place_market_buy(
        product_id="BTC-USD",
        quote_size_usd=Decimal("1500"),
        retail_portfolio_id="portfolio-uuid-123",
        client_order_id="fixed-id",
    )
    args, kwargs = session.post.call_args
    body = kwargs["json"]

    assert body["client_order_id"] == "fixed-id"
    assert body["product_id"] == "BTC-USD"
    assert body["side"] == "BUY"
    assert body["retail_portfolio_id"] == "portfolio-uuid-123"
    assert body["order_configuration"] == {
        "market_market_ioc": {"quote_size": "1500"}
    }
    # Must hit the right URL, and must carry an Authorization bearer.
    assert args[0].endswith("/api/v3/brokerage/orders")
    assert kwargs["headers"]["Authorization"].startswith("Bearer ")
    assert kwargs["headers"]["Content-Type"] == "application/json"


def test_place_stop_limit_buy_payload(cdp_key: CDPKey) -> None:
    session = _mock_session(200, {"success": True, "order_id": "xyz"})
    client = CoinbaseClient(cdp_key, session=session)
    client.place_stop_limit_buy(
        product_id="ETH-USD",
        base_size=Decimal("0.5"),
        limit_price=Decimal("2010"),
        stop_price=Decimal("2000"),
        retail_portfolio_id="p-uuid",
        stop_direction="UP",
        client_order_id="stop-id",
    )
    _, kwargs = session.post.call_args
    body = kwargs["json"]
    assert body["side"] == "BUY"
    assert body["order_configuration"]["stop_limit_stop_limit_gtc"] == {
        "base_size": "0.5",
        "limit_price": "2010",
        "stop_price": "2000",
        "stop_direction": "STOP_DIRECTION_STOP_UP",
    }


def test_place_market_buy_rejects_non_positive(cdp_key: CDPKey) -> None:
    client = CoinbaseClient(cdp_key, session=_mock_session(200, {}))
    with pytest.raises(CoinbaseClientError, match="positive"):
        client.place_market_buy(
            product_id="BTC-USD",
            quote_size_usd=Decimal("0"),
            retail_portfolio_id="p",
        )


def test_place_stop_limit_buy_rejects_bad_direction(cdp_key: CDPKey) -> None:
    client = CoinbaseClient(cdp_key, session=_mock_session(200, {}))
    with pytest.raises(CoinbaseClientError, match="stop_direction"):
        client.place_stop_limit_buy(
            product_id="BTC-USD",
            base_size=Decimal("0.01"),
            limit_price=Decimal("51000"),
            stop_price=Decimal("50000"),
            retail_portfolio_id="p",
            stop_direction="SIDEWAYS",  # type: ignore[arg-type]
        )


# -- Pagination --------------------------------------------------------------


def test_list_all_products_stops_on_short_page() -> None:
    session = MagicMock(spec=requests.Session)
    session.headers = {}
    page1 = [{"product_id": f"P{i}-USD"} for i in range(250)]
    page2 = [{"product_id": f"Q{i}-USD"} for i in range(10)]
    r1, r2 = MagicMock(), MagicMock()
    r1.status_code = 200
    r2.status_code = 200
    r1.json.return_value = {"products": page1}
    r2.json.return_value = {"products": page2}
    r1.text = json.dumps({"products": page1})
    r2.text = json.dumps({"products": page2})
    session.get.side_effect = [r1, r2]

    client = CoinbaseClient(None, session=session)
    all_products = client.list_all_products(page_limit=250)
    assert len(all_products) == 260
    assert session.get.call_count == 2
