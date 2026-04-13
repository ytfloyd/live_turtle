"""
Scanner: universe discovery, candle fetch, Donchian + ATR Turtle signals.

Outputs one pandas DataFrame with one row per eligible pair. Also prints a
set of human-readable tables via tabulate. The DataFrame is the contract; the
tables are a side effect.

Key rules
---------
- Entry channels exclude the current bar (look-ahead safe): we look at
  df.iloc[-(N+1):-1] to compute the prior-N-day high/low.
- ATR is simple mean of True Range over the last ATR_PERIOD bars.
- Signals: LONG if close >= prior channel high; EXIT if close <= prior channel low.
- S1 breakouts that are ALSO S2 breakouts are reported in the S2 section only
  (to avoid duplicate rows).
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pandas as pd
from tabulate import tabulate

from turtle_crypto.coinbase_client import CoinbaseClient, CoinbaseClientError, CoinbaseHTTPError
from turtle_crypto.config import (
    ATR_PERIOD,
    CANDLE_HISTORY_DAYS,
    EXCLUDE_BASES,
    MIN_CANDLES_REQUIRED,
    SCANNER_429_BASE_DELAY_SEC,
    SCANNER_429_MAX_RETRIES,
    SCANNER_MAX_REQUESTS_PER_SEC,
    SCANNER_MIN_24H_VOL_USD,
    SCANNER_PRODUCTS_PAGE_LIMIT,
    SCANNER_THREAD_WORKERS,
    SYSTEM1_ENTRY_PERIOD,
    SYSTEM1_EXIT_PERIOD,
    SYSTEM2_ENTRY_PERIOD,
    SYSTEM2_EXIT_PERIOD,
)

logger = logging.getLogger(__name__)

# Columns produced by the scanner. Documented here so downstream code
# (trade_sheet, executor) can rely on them explicitly.
SCANNER_COLUMNS: list[str] = [
    "product_id",
    "base",
    "quote",
    "close",
    "vol_24h_usd",
    "atr",
    "atr_pct",
    "s1_high",
    "s1_low",
    "s2_high",
    "s2_low",
    "s1_channel_pct",
    "s2_channel_pct",
    "s1_signal",
    "s2_signal",
    "return_55d",
    "rank_score",
]


@dataclass(frozen=True)
class UniverseFilterReport:
    total_products: int
    usd_quoted: int
    online: int
    after_exclude_bases: int
    after_volume_filter: int
    dropped_excluded_bases: int
    dropped_low_volume: int


# ---------------------------------------------------------------------------
# Universe discovery
# ---------------------------------------------------------------------------


def discover_universe(
    client: CoinbaseClient,
) -> tuple[list[dict[str, Any]], UniverseFilterReport]:
    """
    Paginate the public products list, filter to USD-quoted online spot pairs
    with enough 24h volume, and exclude stablecoins/wrapped/pegged bases.
    Returns (filtered_products, report_for_logging).
    """
    raw = client.list_all_products(page_limit=SCANNER_PRODUCTS_PAGE_LIMIT)
    logger.info("Scanner fetched %d raw products", len(raw))

    usd_quoted: list[dict[str, Any]] = []
    online = 0
    dropped_excluded = 0
    dropped_low_vol = 0

    for p in raw:
        # Defensive parsing — missing fields just skip that pair rather than raising
        # (the public catalog is large and occasionally has weird entries).
        if not isinstance(p, dict):
            continue
        quote = p.get("quote_currency_id") or p.get("quote_currency")
        status = p.get("status")
        if quote != "USD":
            continue
        if status != "online":
            continue
        usd_quoted.append(p)

    for p in usd_quoted:
        if p.get("status") == "online":
            online += 1

    # Filter out stablecoins, wrapped, etc.
    after_exclude: list[dict[str, Any]] = []
    for p in usd_quoted:
        base = p.get("base_currency_id") or p.get("base_currency") or ""
        if base.upper() in EXCLUDE_BASES:
            dropped_excluded += 1
            continue
        after_exclude.append(p)

    # Volume filter.
    after_vol: list[dict[str, Any]] = []
    for p in after_exclude:
        vol = _parse_decimal(p.get("volume_24h"))
        price = _parse_decimal(p.get("price"))
        if vol is None or price is None:
            dropped_low_vol += 1
            continue
        vol_usd = vol * price
        if vol_usd < SCANNER_MIN_24H_VOL_USD:
            dropped_low_vol += 1
            continue
        p["_vol_24h_usd"] = vol_usd
        after_vol.append(p)

    report = UniverseFilterReport(
        total_products=len(raw),
        usd_quoted=len(usd_quoted),
        online=online,
        after_exclude_bases=len(after_exclude),
        after_volume_filter=len(after_vol),
        dropped_excluded_bases=dropped_excluded,
        dropped_low_volume=dropped_low_vol,
    )
    logger.info(
        "Scanner universe: %d USD-quoted, %d after exclude, %d after vol filter",
        len(usd_quoted),
        len(after_exclude),
        len(after_vol),
    )
    return after_vol, report


def _parse_decimal(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (ArithmeticError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Candle fetch
# ---------------------------------------------------------------------------


def _fetch_candles_for_product(
    client: CoinbaseClient, product_id: str, days: int
) -> pd.DataFrame:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days + 1)
    raw = client.get_candles(
        product_id,
        start_unix=int(start.timestamp()),
        end_unix=int(end.timestamp()),
    )
    # Each candle is a dict: {start, low, high, open, close, volume}.
    rows: list[dict[str, Any]] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        try:
            rows.append(
                {
                    "start": int(c["start"]),
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                    "volume": float(c["volume"]),
                }
            )
        except (KeyError, ValueError, TypeError):
            # One bad candle shouldn't nuke the whole scan. Log and skip.
            logger.warning("Skipping malformed candle for %s: %r", product_id, c)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.sort_values("start").reset_index(drop=True)
    return df


class _TokenBucketRateLimiter:
    """Thread-safe token-bucket rate limiter. stdlib only."""

    def __init__(self, rate: float, capacity: int = 1) -> None:
        self._rate = rate
        self._capacity = capacity
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            time.sleep(wait)


def fetch_all_candles(
    client: CoinbaseClient,
    products: list[dict[str, Any]],
    *,
    days: int = CANDLE_HISTORY_DAYS,
    workers: int = SCANNER_THREAD_WORKERS,
) -> dict[str, pd.DataFrame]:
    """
    Parallel candle fetch with global rate limiting and retry on 429.

    Pairs with non-transient errors or short history are silently dropped
    (with a warning log) so one broken pair doesn't halt the full scan.
    """
    out: dict[str, pd.DataFrame] = {}
    rate_limiter = _TokenBucketRateLimiter(rate=SCANNER_MAX_REQUESTS_PER_SEC)
    max_retries = SCANNER_429_MAX_RETRIES
    base_delay = SCANNER_429_BASE_DELAY_SEC

    def _task(product_id: str) -> tuple[str, pd.DataFrame | None]:
        last_exc: Exception | None = None
        for attempt in range(1 + max_retries):
            rate_limiter.acquire()
            try:
                df = _fetch_candles_for_product(client, product_id, days)
                return product_id, df
            except CoinbaseHTTPError as exc:
                if exc.status == 429 and attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "429 for %s (attempt %d/%d), retrying in %.1fs",
                        product_id, attempt + 1, max_retries + 1, delay,
                    )
                    time.sleep(delay)
                    last_exc = exc
                    continue
                logger.warning("Candle fetch failed for %s: %s", product_id, exc)
                return product_id, None
            except CoinbaseClientError as exc:
                logger.warning("Candle fetch failed for %s: %s", product_id, exc)
                return product_id, None
        logger.warning("Candle fetch exhausted retries for %s: %s", product_id, last_exc)
        return product_id, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_task, p["product_id"]) for p in products if "product_id" in p]
        for fut in as_completed(futures):
            pid, df = fut.result()
            if df is None or df.empty:
                continue
            if len(df) < MIN_CANDLES_REQUIRED:
                logger.debug("Skipping %s: only %d candles", pid, len(df))
                continue
            out[pid] = df
    logger.info("Scanner fetched usable candles for %d pairs", len(out))
    return out


# ---------------------------------------------------------------------------
# Turtle calculations
# ---------------------------------------------------------------------------


def _true_range(df: pd.DataFrame) -> pd.Series:
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr


def compute_turtle_stats(df: pd.DataFrame) -> dict[str, float] | None:
    """
    Compute Turtle statistics for a single product's candle history.

    Returns None if there aren't enough bars. Otherwise returns a dict with all
    numeric fields (plain floats for DataFrame compatibility).
    """
    if len(df) < MIN_CANDLES_REQUIRED:
        return None

    close = float(df["close"].iloc[-1])

    # Donchian entry channels EXCLUDE the current bar — slice ends at -1.
    s1_slice = df.iloc[-(SYSTEM1_ENTRY_PERIOD + 1) : -1]
    s2_slice = df.iloc[-(SYSTEM2_ENTRY_PERIOD + 1) : -1]
    s1_exit_slice = df.iloc[-(SYSTEM1_EXIT_PERIOD + 1) : -1]
    s2_exit_slice = df.iloc[-(SYSTEM2_EXIT_PERIOD + 1) : -1]

    s1_high = float(s1_slice["high"].max())
    s1_low = float(s1_exit_slice["low"].min())
    s2_high = float(s2_slice["high"].max())
    s2_low = float(s2_exit_slice["low"].min())

    # ATR: simple mean of True Range over the last ATR_PERIOD bars.
    tr = _true_range(df).dropna()
    if len(tr) < ATR_PERIOD:
        return None
    atr = float(tr.iloc[-ATR_PERIOD:].mean())
    if atr <= 0 or close <= 0:
        return None
    atr_pct = (atr / close) * 100.0

    # Channel position % across the S1 range, using S1 high + its exit low.
    s1_range = s1_high - s1_low
    s1_channel_pct = 100.0 * (close - s1_low) / s1_range if s1_range > 0 else 0.0
    s2_range = s2_high - s2_low
    s2_channel_pct = 100.0 * (close - s2_low) / s2_range if s2_range > 0 else 0.0

    def _signal(close_: float, high_: float, low_: float) -> str:
        if close_ >= high_:
            return "LONG"
        if close_ <= low_:
            return "EXIT"
        return "—"

    s1_signal = _signal(close, s1_high, s1_low)
    s2_signal = _signal(close, s2_high, s2_low)

    # 55-day return: (close / close_55_ago) - 1, if we have the bars.
    if len(df) >= 56:
        close_55_ago = float(df["close"].iloc[-56])
        return_55d = (close / close_55_ago) - 1.0 if close_55_ago > 0 else 0.0
    else:
        return_55d = 0.0

    return {
        "close": close,
        "atr": atr,
        "atr_pct": atr_pct,
        "s1_high": s1_high,
        "s1_low": s1_low,
        "s2_high": s2_high,
        "s2_low": s2_low,
        "s1_channel_pct": s1_channel_pct,
        "s2_channel_pct": s2_channel_pct,
        "s1_signal": s1_signal,
        "s2_signal": s2_signal,
        "return_55d": return_55d,
    }


def compute_rank_score(stats: dict[str, float], vol_24h_usd: float) -> float:
    """
    Composite ranking score for prioritizing which breakouts to trade when
    more signals fire than the heat cap allows.

    score = breakout_strength × trend_confirmation × liquidity

    breakout_strength:
        S2 LONG:  2.0 + (close - s2_high) / ATR   (strongest tier, ≥ 2.0)
        S1 LONG:  1.0 + (close - s1_high) / ATR   (mid tier, 1.0–2.0)
        neither:  s1_channel_pct / 100             (approaching, 0–1.0)

    trend_confirmation:
        1.0 + max(return_55d, 0)   (multiplicative; neutral at 1.0, never < 1.0)

    liquidity:
        log10(vol_24h_usd)   (rewards liquid markets; ~5 for $100k, ~9 for $1B)
    """
    import math

    close = stats["close"]
    atr = stats["atr"]
    s1_high = stats["s1_high"]
    s2_high = stats["s2_high"]
    s1_signal = stats["s1_signal"]
    s2_signal = stats["s2_signal"]
    s1_channel_pct = stats["s1_channel_pct"]
    return_55d = stats["return_55d"]

    if atr <= 0 or close <= 0:
        return 0.0

    # Component 1: breakout strength
    if s2_signal == "LONG":
        breakout = 2.0 + (close - s2_high) / atr
    elif s1_signal == "LONG":
        breakout = 1.0 + (close - s1_high) / atr
    else:
        breakout = s1_channel_pct / 100.0

    # Component 2: trend confirmation (only rewards positive momentum)
    trend = 1.0 + max(return_55d, 0.0)

    # Component 3: liquidity
    liquidity = math.log10(max(vol_24h_usd, 1.0))

    return breakout * trend * liquidity


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def build_scanner_dataframe(
    candles_by_pair: dict[str, pd.DataFrame],
    products_by_id: dict[str, dict[str, Any]],
) -> pd.DataFrame:
    """Build the master DataFrame with one row per pair that has enough data."""
    rows: list[dict[str, Any]] = []
    for product_id, df in candles_by_pair.items():
        stats = compute_turtle_stats(df)
        if stats is None:
            continue
        product = products_by_id.get(product_id, {})
        base = product.get("base_currency_id") or product.get("base_currency") or ""
        quote = product.get("quote_currency_id") or product.get("quote_currency") or ""
        vol_usd_raw = product.get("_vol_24h_usd")
        vol_24h_usd = float(vol_usd_raw) if vol_usd_raw is not None else 0.0

        rows.append(
            {
                "product_id": product_id,
                "base": base,
                "quote": quote,
                "close": stats["close"],
                "vol_24h_usd": vol_24h_usd,
                "atr": stats["atr"],
                "atr_pct": stats["atr_pct"],
                "s1_high": stats["s1_high"],
                "s1_low": stats["s1_low"],
                "s2_high": stats["s2_high"],
                "s2_low": stats["s2_low"],
                "s1_channel_pct": stats["s1_channel_pct"],
                "s2_channel_pct": stats["s2_channel_pct"],
                "s1_signal": stats["s1_signal"],
                "s2_signal": stats["s2_signal"],
                "return_55d": stats["return_55d"],
                "rank_score": compute_rank_score(stats, vol_24h_usd),
            }
        )

    if not rows:
        return pd.DataFrame(columns=SCANNER_COLUMNS)
    return pd.DataFrame(rows, columns=SCANNER_COLUMNS)


def run_scan(client: CoinbaseClient | None = None) -> pd.DataFrame:
    """
    Run the full scanner pipeline and return the DataFrame. Pure library entry
    point — does NOT print tables. Callers that want the tables must call
    `print_scanner_tables(df)` explicitly.
    """
    if client is None:
        client = CoinbaseClient(None)
    products, report = discover_universe(client)
    logger.info("Universe filter report: %s", report)
    products_by_id = {p["product_id"]: p for p in products if "product_id" in p}
    candles = fetch_all_candles(client, products)
    df = build_scanner_dataframe(candles, products_by_id)
    logger.info("Scanner produced %d rows", len(df))
    return df


# ---------------------------------------------------------------------------
# Pretty-print tables (human-only side effect)
# ---------------------------------------------------------------------------


def print_scanner_tables(df: pd.DataFrame) -> None:
    """Pretty-print the five standard scanner views via tabulate."""
    if df.empty:
        print("Scanner produced no rows.")
        return

    s2_breakouts = df[df["s2_signal"] == "LONG"].sort_values("rank_score", ascending=False)
    s2_ids = set(s2_breakouts["product_id"])
    s1_breakouts = df[(df["s1_signal"] == "LONG") & (~df["product_id"].isin(s2_ids))].sort_values(
        "rank_score", ascending=False
    )
    approaching = df[
        (df["s1_signal"] != "LONG")
        & (df["s1_channel_pct"] >= 75.0)
        & (df["s1_channel_pct"] < 100.0)
    ].sort_values("rank_score", ascending=False)
    exits = df[(df["s1_signal"] == "EXIT") | (df["s2_signal"] == "EXIT")]

    top50 = df.sort_values("rank_score", ascending=False).head(50)
    bottom20 = df.sort_values("rank_score", ascending=True).head(20)

    def _fmt(sub: pd.DataFrame) -> str:
        if sub.empty:
            return "(none)"
        view = sub[
            [
                "product_id",
                "close",
                "atr_pct",
                "s1_channel_pct",
                "s2_channel_pct",
                "s1_signal",
                "s2_signal",
                "return_55d",
                "vol_24h_usd",
                "rank_score",
            ]
        ].copy()
        view["return_55d"] = view["return_55d"] * 100.0
        return tabulate(
            view.values.tolist(),
            headers=[
                "pair",
                "close",
                "atr%",
                "s1%",
                "s2%",
                "s1",
                "s2",
                "55d%",
                "vol24h$",
                "rank",
            ],
            floatfmt=(
                "",
                ".4f",
                ".2f",
                ".1f",
                ".1f",
                "",
                "",
                ".1f",
                ",.0f",
                ".1f",
            ),
            tablefmt="simple",
        )

    print("\n=== S2 BREAKOUTS (55d channel) ===")
    print(_fmt(s2_breakouts))
    print("\n=== S1 BREAKOUTS (S2 excluded) ===")
    print(_fmt(s1_breakouts))
    print("\n=== APPROACHING S1 BREAKOUT (75%+) ===")
    print(_fmt(approaching))
    print("\n=== EXIT SIGNALS ===")
    print(_fmt(exits))
    print("\n=== TOP 50 FULL BOARD ===")
    print(_fmt(top50))
    print("\n=== BOTTOM 20 FULL BOARD ===")
    print(_fmt(bottom20))

    total = len(df)
    s2_ct = len(s2_breakouts)
    s1_ct = len(s1_breakouts)
    print(
        "\nSummary: "
        f"{total} pairs scanned | "
        f"{s2_ct} S2 breakouts | "
        f"{s1_ct} S1 breakouts (excl S2) | "
        f"{len(approaching)} approaching | "
        f"{len(exits)} exit signals"
    )
