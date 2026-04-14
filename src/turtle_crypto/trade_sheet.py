"""
Trade sheet: sizing, classification, and human-readable output.

Given a scanner DataFrame, produce:
  1. A structured list[TradeOrder] — the contract the executor consumes.
  2. Three printable tables (active entries, resting buy-stops, portfolio summary).

Sizing formula
--------------
    1 Unit = (ACCOUNT_SIZE * RISK_PER_UNIT) / (2 * ATR_dollars)

Rounding
--------
Quantities are rounded DOWN to the product's `base_increment` (which is a
per-product decimal like "0.00000001" for BTC). If the rounded notional falls
below `base_min_size * close`, the row is rejected.

Classification priority (first match wins)
------------------------------------------
1. S2 LONG, S2 channel ≤ 140%        → full unit   (green bold)
2. S1 LONG, 55d return > 5%          → full unit   (green)
3. S2 LONG, S2 channel > 140%        → ½ unit      (yellow "S2 EXTENDED")
2. BUY-STOP (S1 ≥85%, 55d > 5%)      → full unit   (cyan)     [same priority as #2]
4. BUY-STOP (S1 ≥85%, 55d ≤ 5%)      → ½ unit      (cyan)
4. S1 LONG, 55d return ≤ 5%          → ½ unit      (yellow "S1 WEAK")
5. WATCH (S1 75–85%)                 → no order    (blue dim, display only)

Skip with reason if:
  - vol_24h_usd < MIN_24H_VOL_USD
  - atr_pct > MAX_ATR_PCT
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any, Literal, Optional

import pandas as pd
from tabulate import tabulate

from turtle_crypto.config import (
    ACCOUNT_SIZE,
    HALF_UNIT_MULTIPLIER,
    MAX_ATR_PCT,
    MAX_DEPLOYMENT_PCT,
    MAX_PORTFOLIO_HEAT,
    MIN_24H_VOL_USD,
    RISK_PER_UNIT,
    S2_EXTENDED_CHANNEL_PCT,
    STOP_LOSS_ATR_MULTIPLE,
    STRONG_TREND_55D_RETURN,
    WATCH_CHANNEL_PCT,
)

OrderType = Literal["MARKET_BUY", "STOP_LIMIT_BUY"]


# ---------------------------------------------------------------------------
# ANSI colors
# ---------------------------------------------------------------------------


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeOrder:
    """
    A single proposed order. This is the contract between trade_sheet and
    executor — the executor accepts only validated TradeOrder instances.
    """

    asset: str
    product_id: str
    order_type: OrderType
    base_size: Decimal
    notional_usd: Decimal
    risk_usd: Decimal
    entry_price: Decimal
    stop_loss_price: Decimal
    stop_trigger_price: Optional[Decimal]
    stop_limit_price: Optional[Decimal]
    classification: str
    priority: int
    rank_score: float


@dataclass(frozen=True)
class TradeSheetRow:
    """A post-classification row, whether or not it yielded a TradeOrder."""

    product_id: str
    base: str
    classification: str
    color: str
    priority: int
    close: Decimal
    atr: Decimal
    atr_pct: Decimal
    s1_channel_pct: Decimal
    s2_channel_pct: Decimal
    return_55d: Decimal
    size_multiplier: Decimal  # 1.0 full unit, 0.5 half unit, 0.0 watch-only
    order: Optional[TradeOrder]
    skip_reason: Optional[str]


@dataclass(frozen=True)
class TradeSheet:
    """Full output of build_trade_sheet: the rows, the orders, and the heat."""

    rows: list[TradeSheetRow]
    active_orders: list[TradeOrder]
    total_notional_usd: Decimal
    total_risk_usd: Decimal
    heat_fraction: Decimal
    heat_cap_exceeded: bool
    heat_scale_ratio: Decimal  # 1.0 if no scale-down needed, else <1


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------


def compute_unit_size(
    *,
    account_size: Decimal,
    atr: Decimal,
    close: Decimal,
    base_increment: Decimal,
    base_min_size: Decimal,
    size_multiplier: Decimal = Decimal("1"),
) -> tuple[Decimal, Decimal, Decimal]:
    """
    Compute one Turtle unit for a pair.

    Returns (base_size, notional_usd, risk_usd). Any of these may be zero if
    the computed size is below `base_min_size`.

    Math:
        raw_units_base = (account_size * RISK_PER_UNIT) /
                         (STOP_LOSS_ATR_MULTIPLE * atr)
        base_size = floor(raw_units_base * size_multiplier, base_increment)
        notional_usd = base_size * close
        risk_usd = base_size * STOP_LOSS_ATR_MULTIPLE * atr
    """
    if atr <= 0:
        raise ValueError(f"ATR must be positive, got {atr}")
    if close <= 0:
        raise ValueError(f"close must be positive, got {close}")
    if base_increment <= 0:
        raise ValueError(f"base_increment must be positive, got {base_increment}")

    unit_base_raw = (account_size * RISK_PER_UNIT) / (STOP_LOSS_ATR_MULTIPLE * atr)
    scaled = unit_base_raw * size_multiplier
    base_size = _floor_to_increment(scaled, base_increment)

    if base_size < base_min_size:
        return Decimal("0"), Decimal("0"), Decimal("0")

    notional_usd = base_size * close
    risk_usd = base_size * STOP_LOSS_ATR_MULTIPLE * atr
    return base_size, notional_usd, risk_usd


def _floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    """Floor `value` to the nearest multiple of `increment`."""
    if increment == 0:
        return value
    steps = (value / increment).quantize(Decimal("1"), rounding=ROUND_DOWN)
    return steps * increment


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Classification:
    label: str
    color: str
    priority: int
    size_multiplier: Decimal
    order_type: Optional[OrderType]  # None => display-only


def classify(row: pd.Series) -> Classification | None:
    """
    Apply the classification priority rules from the spec.

    Returns None for rows that don't warrant any output (no breakout and not
    close enough to a breakout to merit the WATCH bucket).
    """
    s1_signal = row["s1_signal"]
    s2_signal = row["s2_signal"]
    s1_pct = Decimal(str(row["s1_channel_pct"]))
    s2_pct = Decimal(str(row["s2_channel_pct"]))
    ret_55d = Decimal(str(row["return_55d"]))
    strong_trend = ret_55d > STRONG_TREND_55D_RETURN

    # 1. S2 LONG, channel ≤ 140% → full unit
    if s2_signal == "LONG" and s2_pct <= S2_EXTENDED_CHANNEL_PCT:
        return Classification(
            label="S2 LONG",
            color=C.BOLD + C.GREEN,
            priority=1,
            size_multiplier=Decimal("1"),
            order_type="MARKET_BUY",
        )

    # 3. S2 LONG, channel > 140% → half unit, EXTENDED label
    if s2_signal == "LONG" and s2_pct > S2_EXTENDED_CHANNEL_PCT:
        return Classification(
            label="S2 EXTENDED",
            color=C.YELLOW,
            priority=3,
            size_multiplier=HALF_UNIT_MULTIPLIER,
            order_type="MARKET_BUY",
        )

    # 2. S1 LONG, 55d return > 5% → full unit
    if s1_signal == "LONG" and strong_trend:
        return Classification(
            label="S1 LONG",
            color=C.GREEN,
            priority=2,
            size_multiplier=Decimal("1"),
            order_type="MARKET_BUY",
        )

    # 4. S1 LONG, 55d return ≤ 5% → half unit, WEAK label
    if s1_signal == "LONG" and not strong_trend:
        return Classification(
            label="S1 WEAK",
            color=C.YELLOW,
            priority=4,
            size_multiplier=HALF_UNIT_MULTIPLIER,
            order_type="MARKET_BUY",
        )

    # Approaching breakout (≥75% of channel) but not through yet → WATCH only.
    # No resting orders. Check again at the next daily close.
    if s1_pct >= WATCH_CHANNEL_PCT:
        return Classification(
            label="WATCH",
            color=C.DIM + C.BLUE,
            priority=5,
            size_multiplier=Decimal("0"),
            order_type=None,
        )

    return None


# ---------------------------------------------------------------------------
# Build the sheet
# ---------------------------------------------------------------------------


def build_trade_sheet(
    scanner_df: pd.DataFrame,
    product_details: dict[str, dict[str, Any]],
    *,
    account_size: Decimal = ACCOUNT_SIZE,
) -> TradeSheet:
    """
    Given a scanner DataFrame and a dict of product details by product_id,
    build the trade sheet.

    product_details must contain, for each candidate product_id, a dict with:
        - base_increment  (str or Decimal)
        - base_min_size   (str or Decimal)
    Missing details -> that row is skipped with a reason.
    """
    rows: list[TradeSheetRow] = []
    for _, series in scanner_df.iterrows():
        classification = classify(series)
        if classification is None:
            continue

        pid = str(series["product_id"])
        base = str(series["base"])
        close = Decimal(str(series["close"]))
        atr = Decimal(str(series["atr"]))
        atr_pct = Decimal(str(series["atr_pct"]))
        s1_pct = Decimal(str(series["s1_channel_pct"]))
        s2_pct = Decimal(str(series["s2_channel_pct"]))
        ret_55d = Decimal(str(series["return_55d"]))
        vol_usd = Decimal(str(series["vol_24h_usd"]))
        s1_high = Decimal(str(series["s1_high"]))
        rank_score = float(series.get("rank_score", 0.0))

        base_row = TradeSheetRow(
            product_id=pid,
            base=base,
            classification=classification.label,
            color=classification.color,
            priority=classification.priority,
            close=close,
            atr=atr,
            atr_pct=atr_pct,
            s1_channel_pct=s1_pct,
            s2_channel_pct=s2_pct,
            return_55d=ret_55d,
            size_multiplier=classification.size_multiplier,
            order=None,
            skip_reason=None,
        )

        # Display-only WATCH rows have no order and no skip reason.
        if classification.order_type is None:
            rows.append(base_row)
            continue

        # Execution-side filters (tighter than scanner).
        if vol_usd < MIN_24H_VOL_USD:
            rows.append(
                _replace(base_row, skip_reason=f"24h vol ${vol_usd:,.0f} < min ${MIN_24H_VOL_USD:,.0f}")
            )
            continue
        if atr_pct > MAX_ATR_PCT:
            rows.append(
                _replace(base_row, skip_reason=f"ATR {atr_pct:.1f}% > max {MAX_ATR_PCT:.1f}%")
            )
            continue

        detail = product_details.get(pid)
        if detail is None:
            rows.append(_replace(base_row, skip_reason="no product details"))
            continue

        try:
            base_increment = Decimal(str(detail["base_increment"]))
            base_min_size = Decimal(str(detail["base_min_size"]))
        except (KeyError, ValueError) as exc:
            rows.append(_replace(base_row, skip_reason=f"bad product details: {exc}"))
            continue

        base_size, notional_usd, risk_usd = compute_unit_size(
            account_size=account_size,
            atr=atr,
            close=close,
            base_increment=base_increment,
            base_min_size=base_min_size,
            size_multiplier=classification.size_multiplier,
        )

        if base_size == 0:
            rows.append(
                _replace(
                    base_row,
                    skip_reason=f"rounded size below base_min_size ({base_min_size})",
                )
            )
            continue

        max_notional = account_size * Decimal("0.20")
        if notional_usd > max_notional:
            rows.append(
                _replace(
                    base_row,
                    skip_reason=f"notional ${notional_usd:,.2f} > 20% cap ${max_notional:,.2f}",
                )
            )
            continue

        stop_loss_price = close - STOP_LOSS_ATR_MULTIPLE * atr

        order: TradeOrder
        if classification.order_type == "MARKET_BUY":
            order = TradeOrder(
                asset=base,
                product_id=pid,
                order_type="MARKET_BUY",
                base_size=base_size,
                notional_usd=notional_usd,
                risk_usd=risk_usd,
                entry_price=close,
                stop_loss_price=stop_loss_price,
                stop_trigger_price=None,
                stop_limit_price=None,
                classification=classification.label,
                priority=classification.priority,
                rank_score=rank_score,
            )
        else:
            rows.append(base_row)
            continue

        rows.append(_replace(base_row, order=order))

    # Collect market orders only — no resting stop-limits in v1.
    # Approaching breakouts stay in WATCH; check again at next daily close.
    active_orders: list[TradeOrder] = []
    for r in rows:
        if r.order is not None and r.order.order_type == "MARKET_BUY":
            active_orders.append(r.order)

    # Sort by rank_score descending — strongest signals first.
    active_orders.sort(key=lambda o: -o.rank_score)

    # Enforce deployment cap: trim lowest-ranked orders if total notional
    # would exceed MAX_DEPLOYMENT_PCT of account. This preserves dry powder
    # for new breakouts on subsequent days.
    max_deployment = account_size * MAX_DEPLOYMENT_PCT
    capped_orders: list[TradeOrder] = []
    running_notional = Decimal("0")
    for o in active_orders:
        if running_notional + o.notional_usd > max_deployment:
            break
        capped_orders.append(o)
        running_notional += o.notional_usd
    active_orders = capped_orders

    total_notional = sum((o.notional_usd for o in active_orders), Decimal("0"))
    total_risk = sum((o.risk_usd for o in active_orders), Decimal("0"))
    heat_cap_usd = account_size * MAX_PORTFOLIO_HEAT
    heat_fraction = (total_risk / account_size) if account_size > 0 else Decimal("0")
    heat_exceeded = total_risk > heat_cap_usd
    if heat_exceeded and total_risk > 0:
        heat_scale_ratio = heat_cap_usd / total_risk
    else:
        heat_scale_ratio = Decimal("1")

    return TradeSheet(
        rows=rows,
        active_orders=active_orders,
        total_notional_usd=total_notional,
        total_risk_usd=total_risk,
        heat_fraction=heat_fraction,
        heat_cap_exceeded=heat_exceeded,
        heat_scale_ratio=heat_scale_ratio,
    )


def _replace(row: TradeSheetRow, **changes: Any) -> TradeSheetRow:
    """Minimal dataclass-replace helper (frozen dataclass)."""
    from dataclasses import replace as dc_replace

    return dc_replace(row, **changes)


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------


def print_trade_sheet(sheet: TradeSheet) -> None:
    """Pretty-print the trade sheet to stdout with ANSI color."""
    print(f"\n{C.BOLD}=== ACTIVE ENTRIES (market orders) ==={C.RESET}")
    if not sheet.active_orders:
        print("(none)")
    else:
        table = []
        for o in sheet.active_orders:
            table.append(
                [
                    o.asset,
                    o.classification,
                    f"{o.base_size:f}",
                    f"${o.entry_price:,.4f}",
                    f"${o.stop_loss_price:,.4f}",
                    f"${o.notional_usd:,.2f}",
                    f"${o.risk_usd:,.2f}",
                    f"{(o.notional_usd / ACCOUNT_SIZE * 100):.1f}%",
                    f"{o.rank_score:.1f}",
                ]
            )
        print(
            tabulate(
                table,
                headers=["asset", "class", "size", "entry", "stop (2N)", "notional", "risk", "%acct", "rank"],
                tablefmt="simple",
            )
        )

    # Watch list and skipped rows.
    watch = [r for r in sheet.rows if r.classification == "WATCH"]
    skipped = [r for r in sheet.rows if r.skip_reason is not None]
    if watch:
        print(f"\n{C.DIM}{C.BLUE}=== WATCH ==={C.RESET}")
        wtable = [
            [r.base, f"{r.s1_channel_pct:.1f}%", f"{(r.return_55d * 100):.1f}%"]
            for r in watch
        ]
        print(tabulate(wtable, headers=["asset", "s1%", "55d%"], tablefmt="simple"))
    if skipped:
        print(f"\n{C.DIM}=== SKIPPED ==={C.RESET}")
        stable = [
            [r.base, r.classification, r.skip_reason or ""] for r in skipped
        ]
        print(tabulate(stable, headers=["asset", "class", "reason"], tablefmt="simple"))

    # Portfolio summary.
    heat_color = C.GREEN
    if sheet.heat_fraction > MAX_PORTFOLIO_HEAT * Decimal("0.75"):
        heat_color = C.YELLOW
    if sheet.heat_cap_exceeded:
        heat_color = C.RED

    print(f"\n{C.BOLD}=== PORTFOLIO SUMMARY ==={C.RESET}")
    print(f"  total notional       ${sheet.total_notional_usd:,.2f}")
    print(f"  total risk           ${sheet.total_risk_usd:,.2f}")
    print(
        f"  portfolio heat       {heat_color}{(sheet.heat_fraction * 100):.2f}%{C.RESET}"
        f"  (cap {(MAX_PORTFOLIO_HEAT * 100):.1f}%)"
    )
    if sheet.heat_cap_exceeded:
        print(
            f"  {C.RED}{C.BOLD}WARNING{C.RESET} — heat cap exceeded. Required scale-down ratio: "
            f"{sheet.heat_scale_ratio:.4f}"
        )
