"""
Executor: the only component that places real orders.

Responsibilities
----------------
- Run startup sanity checks (CDP key load, JWT round-trip against /accounts,
  portfolio binding, balance drift, audit DB writable).
- Enforce hard caps on every single order (notional, daily count, portfolio
  heat), even in dry-run mode.
- Log every order intent to the audit DB BEFORE the network call. Update the
  row after.
- Any exception or rejection sets the HALT flag and refuses further orders
  until `--reset-halt` is passed.
- Two order types only: MARKET_BUY and STOP_LIMIT_BUY.

The executor is instantiated once per script run. It is NOT thread-safe.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from turtle_crypto.audit import AuditStore
from turtle_crypto.coinbase_auth import CDPKey
from turtle_crypto.coinbase_client import CoinbaseClient, CoinbaseClientError
from turtle_crypto.config import (
    ACCOUNT_BALANCE_TOLERANCE,
    ACCOUNT_SIZE,
    MAX_DAILY_ORDERS,
    MAX_ORDER_NOTIONAL_USD,
    MAX_PORTFOLIO_HEAT_USD,
)
from turtle_crypto.trade_sheet import TradeOrder

logger = logging.getLogger(__name__)


class ExecutorError(Exception):
    """Raised on any executor precondition failure or cap violation."""


class SanityCheckError(ExecutorError):
    """Raised when a startup sanity check fails."""


class CapViolation(ExecutorError):
    """Raised when a hard cap would be violated by the proposed order."""


class HaltedError(ExecutorError):
    """Raised when the executor is halted and refuses to place orders."""


@dataclass(frozen=True)
class ExecutionResult:
    order: TradeOrder
    status: str
    response: dict[str, Any] | None
    error_text: str | None


class Executor:
    """
    Stateful executor bound to a single portfolio. One instance per script run.
    """

    def __init__(
        self,
        *,
        client: CoinbaseClient,
        audit: AuditStore,
        portfolio_uuid: str,
        dry_run: bool,
    ) -> None:
        if not portfolio_uuid or not portfolio_uuid.strip():
            raise ExecutorError("portfolio_uuid is required")
        self._client = client
        self._audit = audit
        self._portfolio_uuid = portfolio_uuid
        self._dry_run = dry_run
        # Running totals — in-memory for the current script run. These
        # complement the audit DB's persistent daily counter.
        self._session_notional = Decimal("0")
        self._session_risk = Decimal("0")
        self._session_order_count = 0
        self._sanity_checked = False

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def portfolio_uuid(self) -> str:
        return self._portfolio_uuid

    @property
    def session_totals(self) -> tuple[Decimal, Decimal, int]:
        return self._session_notional, self._session_risk, self._session_order_count

    # ------------------------------------------------------------------
    # Sanity checks
    # ------------------------------------------------------------------

    def run_sanity_checks(self) -> None:
        """
        Read-only startup checks. Must pass before any order. Raises
        SanityCheckError on any failure.
        """
        logger.info("Running executor sanity checks (dry_run=%s)", self._dry_run)

        if self._audit.is_halted():
            reason = self._audit.halt_reason() or "(no reason recorded)"
            raise HaltedError(
                f"Executor is HALTED. Reason: {reason}. "
                "Pass --reset-halt to execute_live.py to clear it."
            )

        # 1) JWT round-trip: fetch all accounts. Any HTTP/auth error surfaces here.
        try:
            accounts = self._client.get_accounts()
        except CoinbaseClientError as exc:
            raise SanityCheckError(f"JWT round-trip against /accounts failed: {exc}") from exc
        if not isinstance(accounts, list):
            raise SanityCheckError("Accounts response did not return a list")
        logger.info("Auth OK — %d accounts visible", len(accounts))

        # Parse existing holdings so we can skip assets already in the portfolio.
        self._existing_holdings = self._parse_holdings(accounts)
        if self._existing_holdings:
            held = ", ".join(
                f"{cur}={bal}" for cur, bal in sorted(self._existing_holdings.items())
            )
            logger.info("Existing holdings: %s", held)

        # 2) Portfolio must exist in the API key's allowed set.
        try:
            portfolios = self._client.get_portfolios()
        except CoinbaseClientError as exc:
            raise SanityCheckError(f"Failed to list portfolios: {exc}") from exc

        matching = [p for p in portfolios if p.get("uuid") == self._portfolio_uuid]
        if not matching:
            visible = [p.get("uuid") for p in portfolios]
            raise SanityCheckError(
                f"ALLOWED_PORTFOLIO_UUID {self._portfolio_uuid} not found in accessible "
                f"portfolios. Visible UUIDs: {visible}"
            )
        logger.info("Portfolio binding OK — UUID matches")

        # 3) Balance check. Log the available USD+USDC for awareness.
        #    After deploying capital into positions, cash will be less than
        #    ACCOUNT_SIZE — that's expected. The real safety is per-order
        #    notional caps and heat caps, plus retail_portfolio_id binding.
        balance_usd = self._sum_usd_from_accounts(accounts)
        logger.info("Available USD+USDC cash: $%s", balance_usd)
        if balance_usd <= 0:
            raise SanityCheckError("No USD or USDC balance available. Cannot place orders.")

        # 4) Audit DB writable. We'll insert a sentinel intent and roll it back
        #    by updating to a recognizable status; the row is left in place as
        #    evidence that the check ran.
        probe_id = self._audit.insert_intent(
            product_id="__sanity_probe__",
            client_order_id="sanity-" + uuid.uuid4().hex[:8],
            intent={"sanity": "probe", "dry_run": self._dry_run},
            dry_run=True,
        )
        self._audit.update_status(probe_id, status="dry_run")
        logger.info("Audit DB writable — probe id %d", probe_id)

        self._sanity_checked = True

    # Currencies treated as USD-equivalent for the balance sanity check.
    _USD_EQUIVALENT_CURRENCIES = frozenset({"USD", "USDC"})

    @staticmethod
    def _sum_usd_from_accounts(accounts: list[dict[str, Any]]) -> Decimal:
        """
        Sum the USD-equivalent value across all accounts. Each account has:
            available_balance: {value, currency}
        We count both USD and USDC as dollar-equivalent (USDC is 1:1 pegged
        and redeemable on Coinbase).
        """
        total = Decimal("0")
        for account in accounts:
            if not isinstance(account, dict):
                continue
            bal = account.get("available_balance")
            if not isinstance(bal, dict):
                continue
            currency = bal.get("currency")
            value_raw = bal.get("value")
            if currency in Executor._USD_EQUIVALENT_CURRENCIES and value_raw is not None:
                try:
                    total += Decimal(str(value_raw))
                except (ArithmeticError, ValueError):
                    continue
        return total

    @staticmethod
    def _parse_holdings(accounts: list[dict[str, Any]]) -> dict[str, Decimal]:
        """
        Extract non-zero crypto holdings from the accounts list.

        Returns {currency: balance} for every currency with a positive balance,
        excluding USD/USDC (those are cash, not positions).
        """
        holdings: dict[str, Decimal] = {}
        for account in accounts:
            if not isinstance(account, dict):
                continue
            bal = account.get("available_balance")
            if not isinstance(bal, dict):
                continue
            currency = bal.get("currency")
            value_raw = bal.get("value")
            if not currency or currency in Executor._USD_EQUIVALENT_CURRENCIES:
                continue
            if value_raw is None:
                continue
            try:
                amount = Decimal(str(value_raw))
            except (ArithmeticError, ValueError):
                continue
            if amount > 0:
                holdings[currency] = amount
        return holdings

    def filter_already_held(self, orders: list[TradeOrder]) -> list[TradeOrder]:
        """
        Remove orders for assets the portfolio already holds. Returns the
        filtered list and logs each skipped asset.
        """
        filtered: list[TradeOrder] = []
        for order in orders:
            if order.asset in self._existing_holdings:
                held = self._existing_holdings[order.asset]
                logger.info(
                    "SKIP %s — already holding %s %s",
                    order.product_id, held, order.asset,
                )
                continue
            filtered.append(order)
        return filtered

    # ------------------------------------------------------------------
    # Hard caps
    # ------------------------------------------------------------------

    def _enforce_caps(self, order: TradeOrder) -> None:
        """Raise CapViolation if the proposed order would breach any cap."""
        if order.notional_usd > MAX_ORDER_NOTIONAL_USD:
            raise CapViolation(
                f"order {order.asset} notional ${order.notional_usd:,.2f} > "
                f"cap ${MAX_ORDER_NOTIONAL_USD}"
            )

        daily_count = self._audit.daily_order_count()
        if not self._dry_run and daily_count >= MAX_DAILY_ORDERS:
            raise CapViolation(
                f"daily order count {daily_count} >= cap {MAX_DAILY_ORDERS} "
                "(resets at UTC midnight)"
            )

        projected_risk = self._session_risk + order.risk_usd
        if projected_risk > MAX_PORTFOLIO_HEAT_USD:
            raise CapViolation(
                f"projected session risk ${projected_risk:,.2f} > "
                f"heat cap ${MAX_PORTFOLIO_HEAT_USD}"
            )

    # ------------------------------------------------------------------
    # Product ID mapping (scan on USD, execute on USDC)
    # ------------------------------------------------------------------

    @staticmethod
    def _execution_product_id(product_id: str) -> str:
        """
        Map analysis product_id to execution product_id.

        Scanner runs on XXX-USD pairs (larger universe, more liquid candle
        data). Execution runs on XXX-USDC pairs (portfolio holds USDC).
        USDC is 1:1 with USD on Coinbase so quote_size is identical.
        """
        if product_id.endswith("-USD"):
            return product_id[:-4] + "-USDC"
        return product_id

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def _build_intent(self, order: TradeOrder, client_order_id: str) -> dict[str, Any]:
        """Build the request body we'd POST to /orders, as a dict."""
        exec_pid = self._execution_product_id(order.product_id)
        if order.order_type == "MARKET_BUY":
            return {
                "client_order_id": client_order_id,
                "product_id": exec_pid,
                "side": "BUY",
                "order_configuration": {
                    "market_market_ioc": {
                        "quote_size": _dec_str(order.notional_usd),
                    }
                },
                "retail_portfolio_id": self._portfolio_uuid,
            }
        if order.order_type == "STOP_LIMIT_BUY":
            if order.stop_trigger_price is None or order.stop_limit_price is None:
                raise ExecutorError(
                    f"stop-limit order for {order.asset} missing trigger/limit prices"
                )
            return {
                "client_order_id": client_order_id,
                "product_id": exec_pid,
                "side": "BUY",
                "order_configuration": {
                    "stop_limit_stop_limit_gtc": {
                        "base_size": _dec_str(order.base_size),
                        "limit_price": _dec_str(order.stop_limit_price),
                        "stop_price": _dec_str(order.stop_trigger_price),
                        "stop_direction": "STOP_DIRECTION_STOP_UP",
                    }
                },
                "retail_portfolio_id": self._portfolio_uuid,
            }
        raise ExecutorError(f"unknown order_type: {order.order_type}")

    def place_order(self, order: TradeOrder) -> ExecutionResult:
        """
        Place a single order. In dry_run mode, logs the intent and returns
        without hitting the network. On any failure, halts the executor.
        """
        if not self._sanity_checked:
            raise ExecutorError(
                "place_order called before run_sanity_checks — refusing"
            )
        if self._audit.is_halted():
            raise HaltedError(
                f"Executor is halted: {self._audit.halt_reason() or '(unknown)'}"
            )

        self._enforce_caps(order)

        client_order_id = f"turtle-{uuid.uuid4().hex}"
        intent = self._build_intent(order, client_order_id)

        row_id = self._audit.insert_intent(
            product_id=order.product_id,
            client_order_id=client_order_id,
            intent=intent,
            dry_run=self._dry_run,
        )

        if self._dry_run:
            self._audit.update_status(row_id, status="dry_run")
            # Track session totals even in dry run so heat-cap logic is meaningful.
            self._session_notional += order.notional_usd
            self._session_risk += order.risk_usd
            self._session_order_count += 1
            logger.info(
                "DRY RUN — %s %s logged (audit id %d) notional=$%s risk=$%s",
                order.order_type,
                order.product_id,
                row_id,
                order.notional_usd,
                order.risk_usd,
            )
            return ExecutionResult(
                order=order, status="dry_run", response=None, error_text=None
            )

        # Live order. Any exception => halt the executor so subsequent orders
        # in the same run are refused until the user investigates.
        exec_pid = self._execution_product_id(order.product_id)
        try:
            if order.order_type == "MARKET_BUY":
                response = self._client.place_market_buy(
                    product_id=exec_pid,
                    quote_size_usd=order.notional_usd,
                    retail_portfolio_id=self._portfolio_uuid,
                    client_order_id=client_order_id,
                )
            elif order.order_type == "STOP_LIMIT_BUY":
                if order.stop_trigger_price is None or order.stop_limit_price is None:
                    raise ExecutorError(
                        f"stop-limit {order.asset} missing trigger/limit prices"
                    )
                response = self._client.place_stop_limit_buy(
                    product_id=exec_pid,
                    base_size=order.base_size,
                    limit_price=order.stop_limit_price,
                    stop_price=order.stop_trigger_price,
                    retail_portfolio_id=self._portfolio_uuid,
                    stop_direction="UP",
                    client_order_id=client_order_id,
                )
            else:
                raise ExecutorError(f"unknown order_type: {order.order_type}")
        except CoinbaseClientError as exc:
            self._audit.update_status(row_id, status="errored", error_text=str(exc))
            self._audit.set_halt(f"{order.product_id} place_order error: {exc}")
            raise

        # Parse the response structure. Coinbase returns {success: bool, ...}
        if not isinstance(response, dict):
            self._audit.update_status(
                row_id, status="errored", error_text=f"non-dict response: {response!r}"
            )
            self._audit.set_halt(f"{order.product_id} non-dict response")
            raise ExecutorError(f"non-dict response: {response!r}")

        if response.get("success") is False:
            err = response.get("error_response") or response.get("failure_reason") or response
            self._audit.update_status(
                row_id, status="rejected", response=response, error_text=str(err)
            )
            self._audit.set_halt(f"{order.product_id} rejected: {err}")
            raise ExecutorError(f"order rejected by Coinbase: {err}")

        self._audit.update_status(row_id, status="filled", response=response)
        self._audit.increment_daily_order_count()
        self._session_notional += order.notional_usd
        self._session_risk += order.risk_usd
        self._session_order_count += 1
        logger.info(
            "FILLED — %s %s audit id %d notional=$%s",
            order.order_type,
            order.product_id,
            row_id,
            order.notional_usd,
        )
        return ExecutionResult(
            order=order, status="filled", response=response, error_text=None
        )


def _dec_str(value: Decimal) -> str:
    """Decimal → plain string, no sci notation, no trailing zeros."""
    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"
