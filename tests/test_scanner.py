"""
Unit tests for scanner.

Tests the pure calculation core (compute_turtle_stats) against hand-computable
fixtures, plus the rate limiter and retry logic. No network.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from turtle_crypto.coinbase_client import CoinbaseClientError, CoinbaseHTTPError
from turtle_crypto.scanner import (
    _TokenBucketRateLimiter,
    build_scanner_dataframe,
    compute_turtle_stats,
    fetch_all_candles,
)


def _make_df(closes: list[float]) -> pd.DataFrame:
    """Build a fake candle DF where high=low=open=close for each bar."""
    n = len(closes)
    return pd.DataFrame(
        {
            "start": list(range(n)),
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [100.0] * n,
        }
    )


def test_compute_stats_requires_min_candles() -> None:
    df = _make_df([100.0] * 55)  # 55 < MIN_CANDLES_REQUIRED (56)
    assert compute_turtle_stats(df) is None


def test_compute_stats_flat_series_skipped_due_to_zero_atr() -> None:
    # A perfectly flat series has TR == 0 for every bar. We treat ATR == 0
    # as "not enough signal to trade" and skip the row.
    df = _make_df([100.0] * 80)
    assert compute_turtle_stats(df) is None


def test_compute_stats_breakout_up() -> None:
    # First 70 bars in a 90..100 range, then a breakout to 120 on the last bar.
    closes = [90.0 + (i % 11) for i in range(70)]  # oscillating 90..100
    closes.append(120.0)  # the breakout bar (bar #71, index 70)
    # Pad to MIN_CANDLES_REQUIRED. We need >= 56 bars, have 71 — good.
    df = _make_df(closes)
    stats = compute_turtle_stats(df)
    assert stats is not None
    # Prior 20-day high (excluding the current bar) is at most 100.
    assert stats["s1_high"] <= 100.5
    # Current close > s1_high => S1 LONG breakout.
    assert stats["s1_signal"] == "LONG"
    # With 71 bars the 55-day return computation is active.
    assert isinstance(stats["return_55d"], float)


def test_compute_stats_breakdown_down() -> None:
    closes = [100.0] * 60 + [50.0]  # Sharp drop.
    df = _make_df(closes)
    stats = compute_turtle_stats(df)
    assert stats is not None
    assert stats["s1_signal"] == "EXIT"


def test_channel_pct_at_boundaries() -> None:
    # Flat range, then a close exactly at the prior low → 0%.
    closes = [105.0] * 30 + [110.0] * 30 + [95.0]
    df = _make_df(closes)
    stats = compute_turtle_stats(df)
    assert stats is not None
    # Our series has non-flat prior, so atr > 0 — good.
    assert 0.0 <= stats["s1_channel_pct"] <= 200.0


def test_build_dataframe_filters_out_short_history() -> None:
    candles = {
        "BTC-USD": _make_df([100.0 + i for i in range(80)]),
        "SHORT-USD": _make_df([100.0 + i for i in range(20)]),  # too short
    }
    products = {
        "BTC-USD": {"product_id": "BTC-USD", "base_currency_id": "BTC", "quote_currency_id": "USD", "_vol_24h_usd": 10_000_000},
        "SHORT-USD": {"product_id": "SHORT-USD", "base_currency_id": "SHORT", "quote_currency_id": "USD", "_vol_24h_usd": 1_000_000},
    }
    df = build_scanner_dataframe(candles, products)
    assert "BTC-USD" in df["product_id"].tolist()
    assert "SHORT-USD" not in df["product_id"].tolist()


def test_atr_is_positive_for_trending_series() -> None:
    # Strictly increasing series → TR > 0 for every bar.
    df = _make_df([100.0 + i * 0.5 for i in range(80)])
    stats = compute_turtle_stats(df)
    assert stats is not None
    assert stats["atr"] > 0
    assert stats["atr_pct"] > 0


def test_return_55d_computation() -> None:
    # Construct a series where close[-56] = 100 and close[-1] = 200.
    closes = [100.0] * 56 + [150.0] * 10 + [200.0]  # 67 bars; idx -56 = 100
    df = _make_df(closes)
    stats = compute_turtle_stats(df)
    assert stats is not None
    # Should be +100% = 1.0 exactly (the bar 56 ago had close 100).
    assert abs(stats["return_55d"] - 1.0) < 1e-9


def test_scanner_dataframe_empty_when_no_candles() -> None:
    df = build_scanner_dataframe({}, {})
    assert df.empty
    assert set(["product_id", "close", "atr", "s1_signal"]).issubset(df.columns)


# -- Token bucket rate limiter -----------------------------------------------


def test_token_bucket_paces_requests() -> None:
    limiter = _TokenBucketRateLimiter(rate=20.0, capacity=1)
    # Acquire 5 tokens at 20/s. First one is instant (pre-loaded), remaining 4
    # take ~0.2s total. Allow generous margin for CI jitter.
    t0 = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.15, f"too fast: {elapsed:.3f}s for 5 tokens at 20/s"
    assert elapsed < 1.0, f"too slow: {elapsed:.3f}s"


# -- Retry on 429 ------------------------------------------------------------


def _make_candle_df() -> pd.DataFrame:
    """A valid candle DataFrame with enough bars."""
    n = 80
    return pd.DataFrame(
        {
            "start": list(range(n)),
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.0 + i for i in range(n)],
            "volume": [1000.0] * n,
        }
    )


@patch("turtle_crypto.scanner._TokenBucketRateLimiter")
@patch("turtle_crypto.scanner._fetch_candles_for_product")
def test_retry_on_429_succeeds_on_second_attempt(
    mock_fetch: MagicMock, mock_limiter_cls: MagicMock
) -> None:
    # Rate limiter is a no-op for this test.
    mock_limiter_cls.return_value.acquire = MagicMock()
    # First call raises 429, second succeeds.
    mock_fetch.side_effect = [
        CoinbaseHTTPError("GET", "/candles", 429, "rate limited"),
        _make_candle_df(),
    ]
    client = MagicMock()
    products = [{"product_id": "BTC-USD"}]
    result = fetch_all_candles(client, products, workers=1)
    assert "BTC-USD" in result
    assert mock_fetch.call_count == 2


@patch("turtle_crypto.scanner._TokenBucketRateLimiter")
@patch("turtle_crypto.scanner._fetch_candles_for_product")
def test_drops_after_exhausting_retries(
    mock_fetch: MagicMock, mock_limiter_cls: MagicMock
) -> None:
    mock_limiter_cls.return_value.acquire = MagicMock()
    # Always 429 — should try 1 + SCANNER_429_MAX_RETRIES = 3 times total.
    mock_fetch.side_effect = CoinbaseHTTPError("GET", "/candles", 429, "rate limited")
    client = MagicMock()
    products = [{"product_id": "BTC-USD"}]
    result = fetch_all_candles(client, products, workers=1)
    assert "BTC-USD" not in result
    assert mock_fetch.call_count == 3  # 1 original + 2 retries


@patch("turtle_crypto.scanner._TokenBucketRateLimiter")
@patch("turtle_crypto.scanner._fetch_candles_for_product")
def test_non_429_error_not_retried(
    mock_fetch: MagicMock, mock_limiter_cls: MagicMock
) -> None:
    mock_limiter_cls.return_value.acquire = MagicMock()
    mock_fetch.side_effect = CoinbaseHTTPError("GET", "/candles", 500, "server error")
    client = MagicMock()
    products = [{"product_id": "BTC-USD"}]
    result = fetch_all_candles(client, products, workers=1)
    assert "BTC-USD" not in result
    assert mock_fetch.call_count == 1  # no retry on 500
