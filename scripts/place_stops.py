"""
scripts/place_stops.py

One-time script: reads the current portfolio holdings, computes 2N
stop-loss levels from today's ATR, and places protective stop-limit
sells on Coinbase for every held position.

Usage:
    uv run python scripts/place_stops.py              # dry-run (shows stops, no orders)
    uv run python scripts/place_stops.py --live       # actually place the stops

This is idempotent in practice — if a stop already exists for a position
on Coinbase, placing another one just means you have two resting stops.
Check the Coinbase UI after running to confirm no duplicates.
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import configure_logging, load_cdp_key_or_die, load_env_or_die  # noqa: E402

from turtle_crypto.audit import AuditStore  # noqa: E402
from turtle_crypto.coinbase_client import CoinbaseClient, CoinbaseClientError  # noqa: E402
from turtle_crypto.config import (  # noqa: E402
    DEFAULT_AUDIT_DB_PATH,
    STOP_LOSS_ATR_MULTIPLE,
    STOP_LOSS_SLIPPAGE,
)
from turtle_crypto.scanner import run_scan  # noqa: E402


def _floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    """Floor `value` to the nearest multiple of `increment`."""
    if increment <= 0:
        return value
    steps = (value / increment).quantize(Decimal("1"), rounding=ROUND_DOWN)
    return steps * increment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live", action="store_true",
        help="Actually place stop orders. Without this flag, just prints what would be placed.",
    )
    args = parser.parse_args()

    configure_logging(level=logging.INFO)
    logger = logging.getLogger("place_stops")

    env = load_env_or_die(["CDP_KEY_FILE_PATH", "ALLOWED_PORTFOLIO_UUID"])
    cdp_key = load_cdp_key_or_die(env["CDP_KEY_FILE_PATH"])
    client = CoinbaseClient(cdp_key)
    portfolio_uuid = env["ALLOWED_PORTFOLIO_UUID"]

    # 1) Get current holdings.
    accounts = client.get_accounts()
    holdings: dict[str, Decimal] = {}
    for account in accounts:
        if not isinstance(account, dict):
            continue
        bal = account.get("available_balance")
        if not isinstance(bal, dict):
            continue
        currency = bal.get("currency")
        value_raw = bal.get("value")
        if not currency or currency in ("USD", "USDC"):
            continue
        if value_raw is None:
            continue
        try:
            amount = Decimal(str(value_raw))
        except (ArithmeticError, ValueError):
            continue
        if amount > 0:
            holdings[currency] = amount

    if not holdings:
        print("No crypto holdings found. Nothing to do.")
        return 0

    print(f"Found {len(holdings)} holdings:")
    for cur, amt in sorted(holdings.items()):
        print(f"  {cur:10s}  {amt}")

    # 2) Run scanner to get current ATR for each held asset.
    logger.info("Running scanner for ATR data...")
    df = run_scan(client)

    # 3) Fetch product details for each USDC pair (for price/size precision).
    logger.info("Fetching product details for USDC pairs...")
    product_specs: dict[str, dict[str, Decimal]] = {}
    for currency in holdings:
        exec_pid = f"{currency}-USDC"
        try:
            details = client.get_product_details(exec_pid)
            product_specs[currency] = {
                "quote_increment": Decimal(str(details["quote_increment"])),
                "base_increment": Decimal(str(details["base_increment"])),
            }
        except CoinbaseClientError as exc:
            logger.warning("Could not fetch details for %s: %s", exec_pid, exc)

    # 4) Match holdings to scanner data and compute stops.
    stops: list[dict[str, Any]] = []
    for currency, held_amount in sorted(holdings.items()):
        product_id = f"{currency}-USD"
        row = df[df["product_id"] == product_id]
        if row.empty:
            print(f"  {currency:10s}  SKIP — not in scanner universe (no ATR data)")
            continue

        specs = product_specs.get(currency)
        if specs is None:
            print(f"  {currency:10s}  SKIP — no USDC product details")
            continue

        quote_inc = specs["quote_increment"]
        base_inc = specs["base_increment"]

        close = Decimal(str(row.iloc[0]["close"]))
        atr = Decimal(str(row.iloc[0]["atr"]))
        raw_stop = close - STOP_LOSS_ATR_MULTIPLE * atr
        raw_limit = raw_stop * (Decimal("1") - STOP_LOSS_SLIPPAGE)

        # Round prices to the product's quote_increment.
        stop_price = _floor_to_increment(raw_stop, quote_inc)
        limit_price = _floor_to_increment(raw_limit, quote_inc)
        # Round base_size to the product's base_increment.
        base_size = _floor_to_increment(held_amount, base_inc)

        if stop_price <= 0 or limit_price <= 0:
            print(f"  {currency:10s}  SKIP — computed stop price <= 0")
            continue
        if base_size <= 0:
            print(f"  {currency:10s}  SKIP — held amount rounds to 0")
            continue

        exec_pid = f"{currency}-USDC"

        stops.append({
            "currency": currency,
            "product_id": product_id,
            "exec_pid": exec_pid,
            "base_size": base_size,
            "close": close,
            "atr": atr,
            "stop_price": stop_price,
            "limit_price": limit_price,
        })

    if not stops:
        print("\nNo stops to place.")
        return 0

    # 5) Print summary.
    print(f"\n=== STOP-LOSS ORDERS TO PLACE ({len(stops)}) ===")
    print(f"{'asset':10s}  {'size':>14s}  {'close':>12s}  {'ATR':>10s}  {'stop':>12s}  {'limit':>12s}  {'pair':12s}")
    print("-" * 96)
    for s in stops:
        print(
            f"{s['currency']:10s}  {str(s['base_size']):>14s}  "
            f"${str(s['close']):>10s}  ${str(s['atr']):>8s}  "
            f"${str(s['stop_price']):>10s}  ${str(s['limit_price']):>10s}  "
            f"{s['exec_pid']:12s}"
        )

    if not args.live:
        print("\n  DRY RUN — no orders placed. Add --live to execute.")
        return 0

    # 6) Place the stops.
    print(f"\n  Placing {len(stops)} stop-loss orders...")
    audit = AuditStore(DEFAULT_AUDIT_DB_PATH)
    success_count = 0
    for s in stops:
        order_id = f"turtle-stop-backfill-{uuid.uuid4().hex}"
        intent = {
            "type": "STOP_LOSS_BACKFILL",
            "product_id": s["product_id"],
            "exec_pid": s["exec_pid"],
            "base_size": str(s["base_size"]),
            "stop_price": str(s["stop_price"]),
            "limit_price": str(s["limit_price"]),
        }
        row_id = audit.insert_intent(
            product_id=s["product_id"],
            client_order_id=order_id,
            intent=intent,
            dry_run=False,
        )

        try:
            response = client.place_stop_limit_sell(
                product_id=s["exec_pid"],
                base_size=s["base_size"],
                limit_price=s["limit_price"],
                stop_price=s["stop_price"],
                retail_portfolio_id=portfolio_uuid,
                client_order_id=order_id,
            )
        except CoinbaseClientError as exc:
            audit.update_status(row_id, status="errored", error_text=str(exc))
            print(f"  {s['currency']:10s}  ERROR: {exc}")
            continue

        if isinstance(response, dict) and response.get("success") is not False:
            audit.update_status(row_id, status="filled", response=response)
            print(f"  {s['currency']:10s}  PLACED  stop=${s['stop_price']}  limit=${s['limit_price']}")
            success_count += 1
        else:
            err = response if not isinstance(response, dict) else (
                response.get("error_response") or response
            )
            audit.update_status(row_id, status="rejected", response=response, error_text=str(err))
            print(f"  {s['currency']:10s}  REJECTED: {err}")

    print(f"\n  {success_count}/{len(stops)} stops placed successfully.")
    audit.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
