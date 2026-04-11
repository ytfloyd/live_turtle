"""
Central configuration for the turtle-crypto system.

Every threshold, cap, and magic number in the system lives here. No business
logic anywhere else in the codebase should hardcode a number — if a new
threshold is needed, add it here first.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

# ---------------------------------------------------------------------------
# API endpoints (Coinbase Advanced)
# ---------------------------------------------------------------------------

COINBASE_API_HOST: str = "api.coinbase.com"
COINBASE_API_BASE_URL: str = f"https://{COINBASE_API_HOST}"

# Public market data endpoints (no auth required).
PUBLIC_PRODUCTS_PATH: str = "/api/v3/brokerage/market/products"
PUBLIC_PRODUCT_CANDLES_PATH: str = "/api/v3/brokerage/market/products/{product_id}/candles"

# Private endpoints (JWT-signed).
PRIVATE_ACCOUNTS_PATH: str = "/api/v3/brokerage/accounts"
PRIVATE_PORTFOLIOS_PATH: str = "/api/v3/brokerage/portfolios"
PRIVATE_PRODUCTS_PATH: str = "/api/v3/brokerage/products"
PRIVATE_ORDERS_PATH: str = "/api/v3/brokerage/orders"

# ---------------------------------------------------------------------------
# Account sizing
# ---------------------------------------------------------------------------

# Hardcoded account size. The system refuses to run if the live portfolio
# balance differs from this by more than 20% (sanity check at startup).
ACCOUNT_SIZE: Decimal = Decimal("10000")

# Acceptable drift from ACCOUNT_SIZE before the executor refuses to run.
# Expressed as a fraction (0.20 == 20%).
ACCOUNT_BALANCE_TOLERANCE: Decimal = Decimal("0.20")

# ---------------------------------------------------------------------------
# Turtle Trading parameters
# ---------------------------------------------------------------------------

# System 1: shorter-term breakout.
SYSTEM1_ENTRY_PERIOD: int = 20
SYSTEM1_EXIT_PERIOD: int = 10

# System 2: longer-term breakout.
SYSTEM2_ENTRY_PERIOD: int = 55
SYSTEM2_EXIT_PERIOD: int = 20

# ATR window (Wilder would use 20-day SMA here — matches the original Turtles).
ATR_PERIOD: int = 20

# Days of history to request per pair. Need max(entry,exit)+1 bars minimum;
# 75 gives comfortable headroom plus enough bars for the 55-day return context.
CANDLE_HISTORY_DAYS: int = 75

# Minimum candle count required before we attempt Turtle calculations.
# Need 55-day entry channel excluding current bar => 56 bars minimum.
MIN_CANDLES_REQUIRED: int = 56

# ---------------------------------------------------------------------------
# Turtle risk / sizing
# ---------------------------------------------------------------------------

# 1% of account per unit at a 2N stop. "1 Unit" therefore risks exactly
# ACCOUNT_SIZE * RISK_PER_UNIT when the 2-ATR stop is hit.
RISK_PER_UNIT: Decimal = Decimal("0.01")

# Stop-loss distance in ATRs. (Computed/displayed but NOT placed in v1.)
STOP_LOSS_ATR_MULTIPLE: Decimal = Decimal("2")

# Cap on units per individual market.
MAX_UNITS_PER_MARKET: int = 4

# Maximum total portfolio heat (sum of risk across open positions), as a
# fraction of account size. 12% of $10k = $1,200.
MAX_PORTFOLIO_HEAT: Decimal = Decimal("0.12")

# Hard cap on notional per single order, in USD. Prevents a single mispriced
# order from moving more than 15% of the account.
MAX_NOTIONAL_PER_ORDER_USD: Decimal = Decimal("1500")

# Execution filters — tighter than the scanner's universe filter.
MIN_24H_VOL_USD: Decimal = Decimal("500000")
MAX_ATR_PCT: Decimal = Decimal("12.0")

# Scanner universe filter — looser to keep a wide discovery net.
SCANNER_MIN_24H_VOL_USD: Decimal = Decimal("50000")

# Threshold on 55-day return for "S1 LONG full unit" vs. "S1 WEAK half unit".
# Expressed as a fraction (0.05 == 5%).
STRONG_TREND_55D_RETURN: Decimal = Decimal("0.05")

# Half-unit sizing multiplier.
HALF_UNIT_MULTIPLIER: Decimal = Decimal("0.5")

# S2 "extended" threshold: if S2 channel % > 140, treat as extended.
S2_EXTENDED_CHANNEL_PCT: Decimal = Decimal("140")

# BUY-STOP "approaching" threshold: S1 channel % >= 85.
BUY_STOP_TRIGGER_CHANNEL_PCT: Decimal = Decimal("85")

# WATCH range lower bound: S1 channel % >= 75.
WATCH_CHANNEL_PCT: Decimal = Decimal("75")

# Slippage tolerance on stop-limit buy orders: limit = stop * (1 + slippage).
STOP_LIMIT_SLIPPAGE: Decimal = Decimal("0.005")

# ---------------------------------------------------------------------------
# Executor hard caps (enforced inside the executor even in dry-run mode)
# ---------------------------------------------------------------------------

MAX_ORDER_NOTIONAL_USD: Decimal = Decimal("1500")
MAX_DAILY_ORDERS: int = 15
MAX_PORTFOLIO_HEAT_USD: Decimal = Decimal("1200")

# ---------------------------------------------------------------------------
# Scanner concurrency
# ---------------------------------------------------------------------------

SCANNER_THREAD_WORKERS: int = 6
SCANNER_PER_REQUEST_SLEEP_SEC: float = 0.08
SCANNER_PRODUCTS_PAGE_LIMIT: int = 250

# ---------------------------------------------------------------------------
# Stablecoin / wrapped / pegged exclusion set
# ---------------------------------------------------------------------------

EXCLUDE_BASES: frozenset[str] = frozenset(
    {
        "USDT", "USDC", "DAI", "BUSD", "TUSD", "USDP", "GUSD", "FRAX", "PYUSD", "FDUSD",
        "EURC", "EURT", "GBPT", "GYEN", "USDS", "UST", "MIM", "LUSD", "SUSD", "CRVUSD",
        "GHO", "MKUSD", "WBTC", "CBBTC", "CBETH", "WETH", "STETH", "RETH", "MSOL",
        "PAX", "HUSD", "TRIBE", "FEI", "ALUSD", "RAI",
    }
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
DATA_DIR: Path = REPO_ROOT / "data"
DEFAULT_AUDIT_DB_PATH: Path = DATA_DIR / "turtle_audit.db"

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

HTTP_TIMEOUT_SEC: float = 20.0
HTTP_USER_AGENT: str = "turtle-crypto/0.1 (+private)"

# JWT lifetime in seconds. Coinbase expires at 120s; we use the same.
JWT_LIFETIME_SEC: int = 120
