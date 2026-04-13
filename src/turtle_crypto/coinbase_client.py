"""
Thin wrapper around the Coinbase Advanced Trade REST API.

Exposes two kinds of methods:

  - Public (no auth): get_products, get_candles
  - Private (JWT): get_accounts, get_portfolios, get_product_details, place_order

Each method:
  - Takes typed parameters
  - Raises on HTTP or parse error — no silent fallbacks
  - Validates the response structure before returning (missing field == raise)

The client does NOT retry, does NOT cache, and does NOT smooth over API errors.
If Coinbase is flaky, the caller gets a clean exception and decides what to do.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any, Final, Literal

import requests

from turtle_crypto.coinbase_auth import CDPKey, bearer_header, build_jwt
from turtle_crypto.config import (
    COINBASE_API_BASE_URL,
    HTTP_TIMEOUT_SEC,
    HTTP_USER_AGENT,
    PRIVATE_ACCOUNTS_PATH,
    PRIVATE_ORDERS_PATH,
    PRIVATE_PORTFOLIOS_PATH,
    PRIVATE_PRODUCTS_PATH,
    PUBLIC_PRODUCT_CANDLES_PATH,
    PUBLIC_PRODUCTS_PATH,
)

logger = logging.getLogger(__name__)

_CANDLE_GRANULARITY_ONE_DAY: Final[str] = "ONE_DAY"
_STOP_DIRECTION_UP: Final[str] = "STOP_DIRECTION_STOP_UP"
_STOP_DIRECTION_DOWN: Final[str] = "STOP_DIRECTION_STOP_DOWN"


class CoinbaseClientError(Exception):
    """Raised on any Coinbase API error or validation failure."""


class CoinbaseHTTPError(CoinbaseClientError):
    """Raised when the HTTP call itself fails or returns a non-2xx status."""

    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"{method} {path} -> {status}: {body[:500]}")
        self.method = method
        self.path = path
        self.status = status
        self.body = body


def _require_field(obj: Any, field: str, context: str) -> Any:
    if not isinstance(obj, dict) or field not in obj:
        raise CoinbaseClientError(f"Missing required field '{field}' in {context}: {obj!r}")
    return obj[field]


class CoinbaseClient:
    """
    Coinbase Advanced Trade REST client.

    If `cdp_key` is None, only public endpoints are callable; calling a private
    endpoint will raise.
    """

    def __init__(
        self,
        cdp_key: CDPKey | None = None,
        *,
        session: requests.Session | None = None,
        base_url: str = COINBASE_API_BASE_URL,
    ) -> None:
        self._cdp_key = cdp_key
        self._session = session or requests.Session()
        self._base_url = base_url.rstrip("/")
        self._session.headers.update({"User-Agent": HTTP_USER_AGENT, "Accept": "application/json"})

    # ------------------------------------------------------------------
    # Core request plumbing
    # ------------------------------------------------------------------

    def _public_get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        logger.debug("PUBLIC GET %s params=%s", path, params)
        response = self._session.get(url, params=params, timeout=HTTP_TIMEOUT_SEC)
        return self._parse("GET", path, response)

    def _signed_get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_auth()
        token = build_jwt(self._cdp_key, "GET", path)  # type: ignore[arg-type]
        headers = bearer_header(token)
        url = f"{self._base_url}{path}"
        logger.debug("SIGNED GET %s params=%s", path, params)
        response = self._session.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT_SEC)
        return self._parse("GET", path, response)

    def _signed_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_auth()
        token = build_jwt(self._cdp_key, "POST", path)  # type: ignore[arg-type]
        headers = bearer_header(token)
        headers["Content-Type"] = "application/json"
        url = f"{self._base_url}{path}"
        logger.debug("SIGNED POST %s body=%s", path, body)
        response = self._session.post(url, json=body, headers=headers, timeout=HTTP_TIMEOUT_SEC)
        return self._parse("POST", path, response)

    def _require_auth(self) -> None:
        if self._cdp_key is None:
            raise CoinbaseClientError("This endpoint requires CDP auth; no key configured")

    @staticmethod
    def _parse(method: str, path: str, response: requests.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            raise CoinbaseHTTPError(method, path, response.status_code, response.text)
        try:
            parsed = response.json()
        except ValueError as exc:
            raise CoinbaseClientError(
                f"{method} {path} returned non-JSON body: {response.text[:500]}"
            ) from exc
        if not isinstance(parsed, dict):
            raise CoinbaseClientError(
                f"{method} {path} returned non-object JSON: {type(parsed).__name__}"
            )
        return parsed

    # ------------------------------------------------------------------
    # Public market data
    # ------------------------------------------------------------------

    def list_products_page(
        self, *, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        """One page of the public products catalog."""
        payload = self._public_get(
            PUBLIC_PRODUCTS_PATH, params={"limit": limit, "offset": offset}
        )
        products = _require_field(payload, "products", "products response")
        if not isinstance(products, list):
            raise CoinbaseClientError(
                f"'products' must be a list, got {type(products).__name__}"
            )
        return products

    def list_all_products(self, *, page_limit: int = 250) -> list[dict[str, Any]]:
        """Paginate the public products catalog to exhaustion."""
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.list_products_page(limit=page_limit, offset=offset)
            out.extend(page)
            logger.debug("products page offset=%d returned=%d", offset, len(page))
            if len(page) < page_limit:
                break
            offset += page_limit
        return out

    def get_candles(
        self,
        product_id: str,
        *,
        start_unix: int,
        end_unix: int,
        granularity: str = _CANDLE_GRANULARITY_ONE_DAY,
    ) -> list[dict[str, Any]]:
        """
        Fetch candles for a product.

        Returns the raw list of candle dicts. Each candle has:
            {start, low, high, open, close, volume}
        where all values are strings from the API.
        """
        path = PUBLIC_PRODUCT_CANDLES_PATH.format(product_id=product_id)
        payload = self._public_get(
            path,
            params={
                "start": str(start_unix),
                "end": str(end_unix),
                "granularity": granularity,
            },
        )
        candles = _require_field(payload, "candles", f"candles response for {product_id}")
        if not isinstance(candles, list):
            raise CoinbaseClientError(
                f"'candles' must be a list for {product_id}, got {type(candles).__name__}"
            )
        return candles

    # ------------------------------------------------------------------
    # Private (auth required)
    # ------------------------------------------------------------------

    def get_accounts(self, *, retail_portfolio_id: str | None = None) -> list[dict[str, Any]]:
        """Auth smoke test + account list. Paginates until cursor is empty.
        If retail_portfolio_id is provided, only accounts in that portfolio are returned."""
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 250}
            if retail_portfolio_id:
                params["retail_portfolio_id"] = retail_portfolio_id
            if cursor:
                params["cursor"] = cursor
            payload = self._signed_get(PRIVATE_ACCOUNTS_PATH, params=params)
            accounts = _require_field(payload, "accounts", "accounts response")
            if not isinstance(accounts, list):
                raise CoinbaseClientError("'accounts' must be a list")
            out.extend(accounts)
            has_next = payload.get("has_next")
            cursor = payload.get("cursor")
            if not has_next or not cursor:
                break
        return out

    def get_portfolios(self) -> list[dict[str, Any]]:
        payload = self._signed_get(PRIVATE_PORTFOLIOS_PATH)
        portfolios = _require_field(payload, "portfolios", "portfolios response")
        if not isinstance(portfolios, list):
            raise CoinbaseClientError("'portfolios' must be a list")
        return portfolios

    def get_portfolio_breakdown(self, portfolio_uuid: str) -> dict[str, Any]:
        """
        Detailed breakdown for a single portfolio — used to verify USD balance
        at executor startup.
        """
        path = f"{PRIVATE_PORTFOLIOS_PATH}/{portfolio_uuid}"
        payload = self._signed_get(path)
        return _require_field(payload, "breakdown", f"portfolio breakdown for {portfolio_uuid}")

    def get_product_details(self, product_id: str) -> dict[str, Any]:
        """
        Private product details endpoint (includes base_increment, base_min_size,
        quote_increment, etc. — needed for sizing rounding).
        """
        path = f"{PRIVATE_PRODUCTS_PATH}/{product_id}"
        payload = self._signed_get(path)
        # Product details are returned as a flat dict — sanity check required fields.
        for field in ("product_id", "base_increment", "quote_increment", "base_min_size"):
            _require_field(payload, field, f"product details for {product_id}")
        return payload

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def place_market_buy(
        self,
        *,
        product_id: str,
        quote_size_usd: Decimal,
        retail_portfolio_id: str,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Place a MARKET IOC buy for a specific USD amount. Coinbase computes the
        base quantity, which avoids any client-side rounding vs. base_increment.
        """
        if quote_size_usd <= 0:
            raise CoinbaseClientError(f"quote_size_usd must be positive, got {quote_size_usd}")
        order_id = client_order_id or str(uuid.uuid4())
        body: dict[str, Any] = {
            "client_order_id": order_id,
            "product_id": product_id,
            "side": "BUY",
            "order_configuration": {
                "market_market_ioc": {
                    "quote_size": _decimal_to_str(quote_size_usd),
                }
            },
            "retail_portfolio_id": retail_portfolio_id,
        }
        return self._signed_post(PRIVATE_ORDERS_PATH, body)

    def place_stop_limit_buy(
        self,
        *,
        product_id: str,
        base_size: Decimal,
        limit_price: Decimal,
        stop_price: Decimal,
        retail_portfolio_id: str,
        stop_direction: Literal["UP", "DOWN"] = "UP",
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Place a stop-limit BUY. For a breakout-buy-above-price trigger, use
        stop_direction="UP" (STOP_DIRECTION_STOP_UP) — the order arms once the
        market trades up through `stop_price`.
        """
        if base_size <= 0:
            raise CoinbaseClientError(f"base_size must be positive, got {base_size}")
        if limit_price <= 0 or stop_price <= 0:
            raise CoinbaseClientError("limit_price and stop_price must be positive")
        if stop_direction not in ("UP", "DOWN"):
            raise CoinbaseClientError(f"stop_direction must be UP or DOWN, got {stop_direction}")
        direction_str = _STOP_DIRECTION_UP if stop_direction == "UP" else _STOP_DIRECTION_DOWN

        order_id = client_order_id or str(uuid.uuid4())
        body: dict[str, Any] = {
            "client_order_id": order_id,
            "product_id": product_id,
            "side": "BUY",
            "order_configuration": {
                "stop_limit_stop_limit_gtc": {
                    "base_size": _decimal_to_str(base_size),
                    "limit_price": _decimal_to_str(limit_price),
                    "stop_price": _decimal_to_str(stop_price),
                    "stop_direction": direction_str,
                }
            },
            "retail_portfolio_id": retail_portfolio_id,
        }
        return self._signed_post(PRIVATE_ORDERS_PATH, body)


def _decimal_to_str(value: Decimal) -> str:
    """
    Convert a Decimal to a string without scientific notation or trailing zeros.

    Coinbase rejects things like '1E+1' or '0.00000000'. We use the Decimal
    itself (never float), normalize, and strip.
    """
    if not isinstance(value, Decimal):
        raise CoinbaseClientError(
            f"monetary values must be Decimal, got {type(value).__name__}"
        )
    # Quantize carefully: normalize strips trailing zeros but can emit scientific.
    # Use a plain string format.
    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"
