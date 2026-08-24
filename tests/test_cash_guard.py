"""
Tests for the cash guard used by the unattended runner.

`available_cash` decides how much a batch of entries may spend. Getting it
wrong in the permissive direction means orders die with INSUFFICIENT_FUND
and trip the halt flag, which stops the automated job — so the important
property is that it never over-counts.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from _common import available_cash  # noqa: E402

from turtle_crypto.config import CASH_BUFFER_PCT  # noqa: E402


def _acct(currency: str, available: str, hold: str = "0") -> dict:
    return {
        "available_balance": {"value": available, "currency": currency},
        "hold": {"value": hold, "currency": currency},
    }


def test_sums_usd_and_usdc() -> None:
    accounts = [_acct("USD", "1000"), _acct("USDC", "2500.50")]
    assert available_cash(accounts) == Decimal("3500.50")


def test_excludes_crypto() -> None:
    accounts = [_acct("USDC", "500"), _acct("BTC", "1.5"), _acct("PEPE", "1000000")]
    assert available_cash(accounts) == Decimal("500")


def test_excludes_held_cash() -> None:
    # Cash on hold backs a resting order and cannot fund a new buy.
    accounts = [_acct("USDC", "300", hold="900")]
    assert available_cash(accounts) == Decimal("300")


def test_empty_and_malformed_are_zero() -> None:
    assert available_cash([]) == Decimal("0")
    assert available_cash([{}, {"available_balance": None}, "junk"]) == Decimal("0")
    assert available_cash([{"available_balance": {"value": "x", "currency": "USD"}}]) == Decimal("0")


def test_buffer_leaves_room_for_fees() -> None:
    # 0.6% taker each way plus slippage must fit inside the buffer.
    assert CASH_BUFFER_PCT >= Decimal("0.012")


def _trim(orders: list[Decimal], cash: Decimal) -> tuple[list[Decimal], list[Decimal]]:
    """Mirror of the trimming loop in execute_live.py."""
    budget = cash * (Decimal("1") - CASH_BUFFER_PCT)
    kept: list[Decimal] = []
    dropped: list[Decimal] = []
    running = Decimal("0")
    for n in orders:
        if running + n > budget:
            dropped.append(n)
            continue
        kept.append(n)
        running += n
    return kept, dropped


def test_trim_keeps_everything_when_affordable() -> None:
    kept, dropped = _trim([Decimal("100")] * 5, Decimal("10000"))
    assert len(kept) == 5 and not dropped


def test_trim_drops_tail_when_short() -> None:
    # $1,000 cash, 3% buffer -> $970 budget. Three $400 orders: two fit.
    kept, dropped = _trim([Decimal("400")] * 3, Decimal("1000"))
    assert kept == [Decimal("400"), Decimal("400")]
    assert dropped == [Decimal("400")]


def test_trim_total_never_exceeds_budget() -> None:
    cash = Decimal("5000")
    kept, _ = _trim([Decimal("1200"), Decimal("900"), Decimal("2400"), Decimal("700")], cash)
    assert sum(kept) <= cash * (Decimal("1") - CASH_BUFFER_PCT)


def test_trim_with_no_cash_keeps_nothing() -> None:
    kept, dropped = _trim([Decimal("100"), Decimal("50")], Decimal("0"))
    assert kept == [] and len(dropped) == 2
