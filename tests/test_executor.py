"""
Unit tests for the Executor.

These tests use a mocked CoinbaseClient — no network. They verify:
  - Sanity checks pass when everything is OK.
  - Each sanity check failure mode raises SanityCheckError with a clear message.
  - Hard caps fire before any network call.
  - Dry-run logs intents but never calls place_order on the client.
  - A live order failure halts the executor.
  - A halted executor refuses further orders.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from turtle_crypto.audit import AuditStore
from turtle_crypto.coinbase_client import CoinbaseClient, CoinbaseClientError
from turtle_crypto.config import MAX_DAILY_ORDERS, MAX_PORTFOLIO_HEAT_USD
from turtle_crypto.executor import (
    CapViolation,
    Executor,
    ExecutorError,
    HaltedError,
    SanityCheckError,
)
from turtle_crypto.trade_sheet import TradeOrder


PORTFOLIO_UUID = "test-portfolio-uuid"


def _make_order(
    *,
    asset: str = "BTC",
    product_id: str = "BTC-USD",
    order_type: str = "MARKET_BUY",
    base_size: Decimal = Decimal("0.02"),
    notional: Decimal = Decimal("1400"),
    risk: Decimal = Decimal("100"),
    entry_price: Decimal = Decimal("70000"),
) -> TradeOrder:
    stop_price = entry_price - Decimal("4800")
    return TradeOrder(
        asset=asset,
        product_id=product_id,
        order_type=order_type,  # type: ignore[arg-type]
        base_size=base_size,
        notional_usd=notional,
        risk_usd=risk,
        entry_price=entry_price,
        stop_loss_price=stop_price,
        stop_trigger_price=entry_price if order_type == "STOP_LIMIT_BUY" else None,
        stop_limit_price=(entry_price * Decimal("1.005")) if order_type == "STOP_LIMIT_BUY" else None,
        classification="S2 LONG",
        priority=1,
        rank_score=10.0,
    )


def _sane_client() -> MagicMock:
    """Build a CoinbaseClient mock that passes every sanity check."""
    client = MagicMock(spec=CoinbaseClient)
    client.get_accounts.return_value = [
        {"uuid": "a1", "available_balance": {"value": "10000", "currency": "USDC"}},
    ]
    client.get_portfolios.return_value = [
        {"uuid": PORTFOLIO_UUID, "name": "turtle"},
        {"uuid": "other-uuid", "name": "savings"},
    ]
    client.place_market_buy.return_value = {
        "success": True,
        "order_id": "server-id-1",
    }
    client.place_stop_limit_buy.return_value = {
        "success": True,
        "order_id": "server-id-2",
    }
    client.place_stop_limit_sell.return_value = {
        "success": True,
        "order_id": "server-id-3",
    }
    client.get_product_details.return_value = {
        "product_id": "BTC-USDC",
        "base_increment": "0.00000001",
        "quote_increment": "0.01",
        "base_min_size": "0.00000001",
    }
    return client


@pytest.fixture()
def audit(tmp_path: Path) -> AuditStore:
    store = AuditStore(tmp_path / "audit.db")
    yield store
    store.close()


# -- Sanity checks -----------------------------------------------------------


def test_sanity_checks_pass(audit: AuditStore) -> None:
    client = _sane_client()
    ex = Executor(client=client, audit=audit, portfolio_uuid=PORTFOLIO_UUID, dry_run=True)
    ex.run_sanity_checks()


def test_sanity_checks_fail_on_missing_portfolio(audit: AuditStore) -> None:
    client = _sane_client()
    client.get_portfolios.return_value = [{"uuid": "other-uuid"}]
    ex = Executor(client=client, audit=audit, portfolio_uuid=PORTFOLIO_UUID, dry_run=True)
    with pytest.raises(SanityCheckError, match="not found"):
        ex.run_sanity_checks()


def test_sanity_checks_fail_on_auth_error(audit: AuditStore) -> None:
    client = _sane_client()
    client.get_accounts.side_effect = CoinbaseClientError("401 unauthorized")
    ex = Executor(client=client, audit=audit, portfolio_uuid=PORTFOLIO_UUID, dry_run=True)
    with pytest.raises(SanityCheckError, match="JWT round-trip"):
        ex.run_sanity_checks()


def test_sanity_checks_fail_on_zero_balance(audit: AuditStore) -> None:
    client = _sane_client()
    # No USD/USDC accounts — balance sums to $0.
    client.get_accounts.return_value = [
        {"uuid": "a1", "available_balance": {"value": "5.0", "currency": "BTC"}},
    ]
    ex = Executor(client=client, audit=audit, portfolio_uuid=PORTFOLIO_UUID, dry_run=True)
    with pytest.raises(SanityCheckError, match="No USD or USDC"):
        ex.run_sanity_checks()


def test_sanity_check_refuses_when_already_halted(audit: AuditStore) -> None:
    audit.set_halt("test halt")
    client = _sane_client()
    ex = Executor(client=client, audit=audit, portfolio_uuid=PORTFOLIO_UUID, dry_run=True)
    with pytest.raises(HaltedError, match="HALTED"):
        ex.run_sanity_checks()


# -- Refuses without sanity check --------------------------------------------


def test_place_order_refuses_before_sanity_check(audit: AuditStore) -> None:
    client = _sane_client()
    ex = Executor(client=client, audit=audit, portfolio_uuid=PORTFOLIO_UUID, dry_run=True)
    with pytest.raises(ExecutorError, match="sanity"):
        ex.place_order(_make_order())


# -- Dry run -----------------------------------------------------------------


def test_dry_run_logs_intent_but_does_not_call_network(audit: AuditStore) -> None:
    client = _sane_client()
    ex = Executor(client=client, audit=audit, portfolio_uuid=PORTFOLIO_UUID, dry_run=True)
    ex.run_sanity_checks()

    result = ex.place_order(_make_order())
    assert result.status == "dry_run"
    # place_market_buy must NOT be called in dry-run mode.
    client.place_market_buy.assert_not_called()
    client.place_stop_limit_buy.assert_not_called()

    # Intent row exists in audit DB.
    rows = [r for r in audit.recent_orders() if r.product_id == "BTC-USD"]
    assert len(rows) == 1
    assert rows[0].status == "dry_run"
    assert rows[0].dry_run is True


def test_dry_run_stop_limit_logs_stop_limit_payload(audit: AuditStore) -> None:
    client = _sane_client()
    ex = Executor(client=client, audit=audit, portfolio_uuid=PORTFOLIO_UUID, dry_run=True)
    ex.run_sanity_checks()
    order = _make_order(order_type="STOP_LIMIT_BUY", asset="ETH", product_id="ETH-USD")
    ex.place_order(order)
    rows = [r for r in audit.recent_orders() if r.product_id == "ETH-USD"]
    assert len(rows) == 1
    assert "stop_limit_stop_limit_gtc" in rows[0].intent_json


# -- Hard caps ---------------------------------------------------------------


def test_notional_cap_fires_before_network(audit: AuditStore) -> None:
    client = _sane_client()
    ex = Executor(client=client, audit=audit, portfolio_uuid=PORTFOLIO_UUID, dry_run=False)
    ex.run_sanity_checks()
    too_big = _make_order(notional=Decimal("4100"))  # > 4000 cap
    with pytest.raises(CapViolation, match="notional"):
        ex.place_order(too_big)
    client.place_market_buy.assert_not_called()


def test_heat_cap_fires_before_network(audit: AuditStore) -> None:
    client = _sane_client()
    ex = Executor(client=client, audit=audit, portfolio_uuid=PORTFOLIO_UUID, dry_run=True)
    ex.run_sanity_checks()
    # 40 $100-risk orders = $4000 = exactly the cap. The 41st should fail.
    for i in range(40):
        ex.place_order(_make_order(product_id=f"SYM{i}-USD"))
    with pytest.raises(CapViolation, match="heat cap"):
        ex.place_order(_make_order(product_id="OVER-USD"))


def test_daily_cap_blocks_live_orders(audit: AuditStore) -> None:
    client = _sane_client()
    ex = Executor(client=client, audit=audit, portfolio_uuid=PORTFOLIO_UUID, dry_run=False)
    ex.run_sanity_checks()
    # Fast-path: inflate the daily counter to the cap.
    for _ in range(MAX_DAILY_ORDERS):
        audit.increment_daily_order_count()
    with pytest.raises(CapViolation, match="daily order count"):
        ex.place_order(_make_order())


# -- Live success and failure ------------------------------------------------


def test_live_success_fills_and_increments_counter(audit: AuditStore) -> None:
    client = _sane_client()
    ex = Executor(client=client, audit=audit, portfolio_uuid=PORTFOLIO_UUID, dry_run=False)
    ex.run_sanity_checks()
    result = ex.place_order(_make_order())
    assert result.status == "filled"
    client.place_market_buy.assert_called_once()
    # Audit DB counter advanced.
    assert audit.daily_order_count() == 1
    # Session totals tracked.
    notional, risk, count = ex.session_totals
    assert count == 1
    assert notional == Decimal("1400")
    assert risk == Decimal("100")


def test_live_network_error_halts_executor(audit: AuditStore) -> None:
    client = _sane_client()
    client.place_market_buy.side_effect = CoinbaseClientError("500 server error")
    ex = Executor(client=client, audit=audit, portfolio_uuid=PORTFOLIO_UUID, dry_run=False)
    ex.run_sanity_checks()
    with pytest.raises(CoinbaseClientError):
        ex.place_order(_make_order())
    assert audit.is_halted() is True
    assert "500 server error" in (audit.halt_reason() or "")

    # A second order attempt is refused outright.
    with pytest.raises(HaltedError):
        ex.place_order(_make_order())


def test_live_rejection_halts_executor(audit: AuditStore) -> None:
    client = _sane_client()
    client.place_market_buy.return_value = {
        "success": False,
        "error_response": {"message": "insufficient funds"},
    }
    ex = Executor(client=client, audit=audit, portfolio_uuid=PORTFOLIO_UUID, dry_run=False)
    ex.run_sanity_checks()
    with pytest.raises(ExecutorError, match="rejected"):
        ex.place_order(_make_order())
    assert audit.is_halted() is True
    # And the response is logged.
    rows = [r for r in audit.recent_orders() if r.product_id == "BTC-USD"]
    assert rows[0].status == "rejected"
    assert "insufficient funds" in (rows[0].error_text or "")


def test_live_non_dict_response_halts_executor(audit: AuditStore) -> None:
    client = _sane_client()
    client.place_market_buy.return_value = "weird string"  # type: ignore[assignment]
    ex = Executor(client=client, audit=audit, portfolio_uuid=PORTFOLIO_UUID, dry_run=False)
    ex.run_sanity_checks()
    with pytest.raises(ExecutorError, match="non-dict"):
        ex.place_order(_make_order())
    assert audit.is_halted() is True
