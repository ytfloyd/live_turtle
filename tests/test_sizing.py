"""
Unit tests for trade_sheet sizing and classification.

All math is done against hand-computable fixtures, covering:
  - The canonical BTC example from the spec.
  - Hard caps (notional cap, heat cap).
  - Classification priority (first match wins).
  - Base increment rounding.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from turtle_crypto.config import (
    ACCOUNT_SIZE,
    MAX_NOTIONAL_PER_ORDER_USD,
    MAX_PORTFOLIO_HEAT,
    RISK_PER_UNIT,
)
from turtle_crypto.trade_sheet import (
    Classification,
    _floor_to_increment,
    build_trade_sheet,
    classify,
    compute_unit_size,
)


# -- Sizing math -------------------------------------------------------------


def test_canonical_btc_example() -> None:
    """
    From the spec: close=$72,000, atr=$2,400, account=$10,000.
    Expected: 1 unit ≈ 0.0208 BTC ≈ $1,500 notional.

    Hand math:
        unit_raw = 10000 * 0.01 / (2 * 2400) = 100 / 4800 = 0.02083333...
        base_increment for BTC on Coinbase is 0.00000001 → essentially no
        rounding at this resolution.
        notional = 0.02083333... * 72000 = ~$1,500
        risk = 0.02083... * 2 * 2400 = ~$100 ✓ (1% of account)
    """
    base_size, notional, risk = compute_unit_size(
        account_size=Decimal("10000"),
        atr=Decimal("2400"),
        close=Decimal("72000"),
        base_increment=Decimal("0.00000001"),
        base_min_size=Decimal("0.00000001"),
    )
    # 100 / 4800 = 0.020833... ; with 8dp increment we keep 0.02083333
    assert abs(base_size - Decimal("0.02083333")) <= Decimal("0.00000001")
    # Notional should be within a few cents of $1,500 (rounding from 8dp).
    assert abs(notional - Decimal("1500")) < Decimal("0.01")
    # Risk is base_size * 2 * ATR — effectively 1% of account.
    assert abs(risk - Decimal("100")) < Decimal("0.01")


def test_risk_always_equals_1pct_before_rounding() -> None:
    """
    For any non-degenerate inputs, raw risk = account * RISK_PER_UNIT exactly.
    Rounding can only shave a tiny amount off.
    """
    base_size, _notional, risk = compute_unit_size(
        account_size=Decimal("10000"),
        atr=Decimal("5"),
        close=Decimal("100"),
        base_increment=Decimal("0.001"),
        base_min_size=Decimal("0.001"),
    )
    assert base_size > 0
    # Expected: 100 / 10 = 10 units base. notional = 1000. risk = 100.
    assert risk == Decimal("100.000")


def test_half_unit_multiplier() -> None:
    full, _, _ = compute_unit_size(
        account_size=Decimal("10000"),
        atr=Decimal("5"),
        close=Decimal("100"),
        base_increment=Decimal("0.001"),
        base_min_size=Decimal("0.001"),
    )
    half, _, _ = compute_unit_size(
        account_size=Decimal("10000"),
        atr=Decimal("5"),
        close=Decimal("100"),
        base_increment=Decimal("0.001"),
        base_min_size=Decimal("0.001"),
        size_multiplier=Decimal("0.5"),
    )
    assert half == full / 2


def test_zero_returned_below_min_size() -> None:
    # A pair where one full unit is below the min trade size: expect zeros.
    base_size, notional, risk = compute_unit_size(
        account_size=Decimal("10000"),
        atr=Decimal("100000"),  # enormous ATR → tiny unit
        close=Decimal("100000"),
        base_increment=Decimal("0.001"),
        base_min_size=Decimal("0.001"),
    )
    assert base_size == 0
    assert notional == 0
    assert risk == 0


def test_base_increment_rounds_down() -> None:
    # raw 0.02083 should floor to 0.02 with a 0.01 increment.
    assert _floor_to_increment(Decimal("0.02083333"), Decimal("0.01")) == Decimal("0.02")
    assert _floor_to_increment(Decimal("10.9999"), Decimal("1")) == Decimal("10")
    assert _floor_to_increment(Decimal("0.5"), Decimal("0.25")) == Decimal("0.50")


def test_compute_unit_size_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="ATR"):
        compute_unit_size(
            account_size=Decimal("10000"),
            atr=Decimal("0"),
            close=Decimal("100"),
            base_increment=Decimal("0.01"),
            base_min_size=Decimal("0.01"),
        )
    with pytest.raises(ValueError, match="close"):
        compute_unit_size(
            account_size=Decimal("10000"),
            atr=Decimal("5"),
            close=Decimal("0"),
            base_increment=Decimal("0.01"),
            base_min_size=Decimal("0.01"),
        )


# -- Classification priority -------------------------------------------------


def _row(**overrides: object) -> pd.Series:
    base = {
        "product_id": "BTC-USD",
        "base": "BTC",
        "quote": "USD",
        "close": 100.0,
        "vol_24h_usd": 10_000_000.0,
        "atr": 2.0,
        "atr_pct": 2.0,
        "s1_high": 99.0,
        "s1_low": 90.0,
        "s2_high": 98.0,
        "s2_low": 85.0,
        "s1_channel_pct": 110.0,
        "s2_channel_pct": 130.0,
        "s1_signal": "—",
        "s2_signal": "—",
        "return_55d": 0.10,
    }
    base.update(overrides)
    return pd.Series(base)


def test_classify_s2_long_full_unit() -> None:
    r = _row(s1_signal="LONG", s2_signal="LONG", s2_channel_pct=120.0)
    c = classify(r)
    assert c is not None
    assert c.label == "S2 LONG"
    assert c.priority == 1
    assert c.size_multiplier == Decimal("1")
    assert c.order_type == "MARKET_BUY"


def test_classify_s2_extended_half_unit() -> None:
    r = _row(s1_signal="LONG", s2_signal="LONG", s2_channel_pct=150.0)
    c = classify(r)
    assert c is not None
    assert c.label == "S2 EXTENDED"
    assert c.priority == 3
    assert c.size_multiplier == Decimal("0.5")


def test_classify_s1_long_strong_trend() -> None:
    r = _row(s1_signal="LONG", s2_signal="—", s1_channel_pct=100.0, return_55d=0.10)
    c = classify(r)
    assert c is not None
    assert c.label == "S1 LONG"
    assert c.priority == 2
    assert c.size_multiplier == Decimal("1")


def test_classify_s1_weak_half_unit() -> None:
    r = _row(s1_signal="LONG", s2_signal="—", s1_channel_pct=100.0, return_55d=0.01)
    c = classify(r)
    assert c is not None
    assert c.label == "S1 WEAK"
    assert c.priority == 4
    assert c.size_multiplier == Decimal("0.5")


def test_classify_buy_stop_strong() -> None:
    r = _row(s1_signal="—", s2_signal="—", s1_channel_pct=90.0, return_55d=0.10)
    c = classify(r)
    assert c is not None
    assert c.label == "BUY-STOP"
    assert c.priority == 2
    assert c.order_type == "STOP_LIMIT_BUY"


def test_classify_buy_stop_weak() -> None:
    r = _row(s1_signal="—", s2_signal="—", s1_channel_pct=90.0, return_55d=0.01)
    c = classify(r)
    assert c is not None
    assert c.label == "BUY-STOP WEAK"
    assert c.priority == 4
    assert c.size_multiplier == Decimal("0.5")


def test_classify_watch_range() -> None:
    r = _row(s1_signal="—", s2_signal="—", s1_channel_pct=80.0)
    c = classify(r)
    assert c is not None
    assert c.label == "WATCH"
    assert c.order_type is None
    assert c.size_multiplier == Decimal("0")


def test_classify_below_watch_returns_none() -> None:
    r = _row(s1_signal="—", s2_signal="—", s1_channel_pct=50.0)
    assert classify(r) is None


# -- End-to-end sheet building -----------------------------------------------


def _build_fixture_df(rows: list[dict]) -> pd.DataFrame:
    # Fill in missing columns with sane defaults.
    defaults = _row().to_dict()
    out = []
    for r in rows:
        merged = {**defaults, **r}
        out.append(merged)
    return pd.DataFrame(out)


def test_build_sheet_produces_typed_orders() -> None:
    df = _build_fixture_df(
        [
            {
                "product_id": "BTC-USD",
                "base": "BTC",
                "close": 72000.0,
                "atr": 2400.0,
                "atr_pct": 3.33,
                "vol_24h_usd": 500_000_000.0,
                "s1_signal": "LONG",
                "s2_signal": "LONG",
                "s1_channel_pct": 110.0,
                "s2_channel_pct": 120.0,
                "return_55d": 0.20,
            }
        ]
    )
    details = {
        "BTC-USD": {
            "base_increment": "0.00000001",
            "base_min_size": "0.00000001",
        }
    }
    sheet = build_trade_sheet(df, details)
    assert len(sheet.active_orders) == 1
    order = sheet.active_orders[0]
    assert order.asset == "BTC"
    assert order.order_type == "MARKET_BUY"
    assert order.classification == "S2 LONG"
    # Notional ≈ $1500 (canonical example).
    assert abs(order.notional_usd - Decimal("1500")) < Decimal("0.01")
    # Risk ≈ $100 (1% of account).
    assert abs(order.risk_usd - Decimal("100")) < Decimal("0.01")


def test_build_sheet_skips_low_volume() -> None:
    df = _build_fixture_df(
        [
            {
                "product_id": "THIN-USD",
                "base": "THIN",
                "close": 1.0,
                "atr": 0.01,
                "atr_pct": 1.0,
                "vol_24h_usd": 100_000.0,  # below MIN_24H_VOL_USD (500k)
                "s1_signal": "LONG",
                "s2_signal": "—",
                "s1_channel_pct": 100.0,
                "return_55d": 0.10,
            }
        ]
    )
    details = {"THIN-USD": {"base_increment": "0.01", "base_min_size": "0.01"}}
    sheet = build_trade_sheet(df, details)
    assert len(sheet.active_orders) == 0
    assert len(sheet.rows) == 1
    assert "24h vol" in (sheet.rows[0].skip_reason or "")


def test_build_sheet_skips_too_volatile() -> None:
    df = _build_fixture_df(
        [
            {
                "product_id": "VOL-USD",
                "base": "VOL",
                "close": 10.0,
                "atr": 2.0,
                "atr_pct": 20.0,  # above MAX_ATR_PCT (12.0)
                "vol_24h_usd": 10_000_000.0,
                "s1_signal": "LONG",
                "s2_signal": "—",
                "s1_channel_pct": 100.0,
                "return_55d": 0.10,
            }
        ]
    )
    details = {"VOL-USD": {"base_increment": "0.01", "base_min_size": "0.01"}}
    sheet = build_trade_sheet(df, details)
    assert len(sheet.active_orders) == 0
    assert "ATR" in (sheet.rows[0].skip_reason or "")


def test_build_sheet_skips_oversize_notional() -> None:
    # Pair with a tiny ATR would produce a huge position; make it fail the
    # notional cap but NOT the atr cap.
    df = _build_fixture_df(
        [
            {
                "product_id": "FAT-USD",
                "base": "FAT",
                "close": 100.0,
                "atr": 0.05,  # tiny ATR → huge position
                "atr_pct": 0.05,
                "vol_24h_usd": 10_000_000.0,
                "s1_signal": "LONG",
                "s2_signal": "—",
                "s1_channel_pct": 100.0,
                "return_55d": 0.10,
            }
        ]
    )
    details = {"FAT-USD": {"base_increment": "0.001", "base_min_size": "0.001"}}
    sheet = build_trade_sheet(df, details)
    # Raw math: unit_raw = 100 / (2*0.05) = 1000 base; notional = 100,000.
    # That blows the $1,500 cap per order.
    assert len(sheet.active_orders) == 0
    assert "notional" in (sheet.rows[0].skip_reason or "")


def test_build_sheet_heat_aggregation() -> None:
    # Two full-unit trades → total risk ≈ $200, heat 2% — OK.
    df = _build_fixture_df(
        [
            {
                "product_id": "BTC-USD", "base": "BTC",
                "close": 72000.0, "atr": 2400.0, "atr_pct": 3.33,
                "vol_24h_usd": 500_000_000.0,
                "s1_signal": "LONG", "s2_signal": "LONG",
                "s1_channel_pct": 110.0, "s2_channel_pct": 120.0,
                "return_55d": 0.20,
            },
            {
                "product_id": "ETH-USD", "base": "ETH",
                "close": 3000.0, "atr": 100.0, "atr_pct": 3.33,
                "vol_24h_usd": 200_000_000.0,
                "s1_signal": "LONG", "s2_signal": "LONG",
                "s1_channel_pct": 110.0, "s2_channel_pct": 120.0,
                "return_55d": 0.20,
            },
        ]
    )
    details = {
        "BTC-USD": {"base_increment": "0.00000001", "base_min_size": "0.00000001"},
        "ETH-USD": {"base_increment": "0.00000001", "base_min_size": "0.00000001"},
    }
    sheet = build_trade_sheet(df, details)
    assert len(sheet.active_orders) == 2
    assert abs(sheet.total_risk_usd - Decimal("200")) < Decimal("0.01")
    assert not sheet.heat_cap_exceeded
    assert sheet.heat_scale_ratio == Decimal("1")


def test_build_sheet_heat_cap_breach_reports_scale_down() -> None:
    # Construct 20 fixture pairs each with ~1% risk → 20% total → breach of 12%.
    rows = []
    details = {}
    for i in range(20):
        pid = f"SYM{i}-USD"
        rows.append(
            {
                "product_id": pid,
                "base": f"SYM{i}",
                "close": 100.0,
                "atr": 5.0,
                "atr_pct": 5.0,
                "vol_24h_usd": 10_000_000.0,
                "s1_signal": "LONG",
                "s2_signal": "LONG",
                "s1_channel_pct": 110.0,
                "s2_channel_pct": 120.0,
                "return_55d": 0.20,
            }
        )
        details[pid] = {"base_increment": "0.001", "base_min_size": "0.001"}
    df = _build_fixture_df(rows)
    sheet = build_trade_sheet(df, details)
    assert sheet.heat_cap_exceeded
    # Total risk should be ~20 * $100 = $2000 in principle; cap = $1200.
    assert sheet.heat_scale_ratio < Decimal("1")
    assert sheet.heat_scale_ratio > Decimal("0")
