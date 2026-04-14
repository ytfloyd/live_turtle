"""
scripts/check_exits.py

Daily exit check: for each held position, if today's close is at or below
the 10-day Donchian low (S1 exit level), sell the position at market and
cancel its resting stop-loss order.

Usage:
    uv run python scripts/check_exits.py              # dry-run (shows exits, no orders)
    uv run python scripts/check_exits.py --live       # execute exits

Run this BEFORE execute_live.py in your daily workflow. Exit first,
then enter new positions with the freed capital.
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

from _common import configure_logging, load_cdp_key_or_die, load_env_or_die, parse_holdings  # noqa: E402

from turtle_crypto.audit import AuditStore  # noqa: E402
from turtle_crypto.coinbase_client import CoinbaseClient, CoinbaseClientError  # noqa: E402
from turtle_crypto.config import DEFAULT_AUDIT_DB_PATH  # noqa: E402
from turtle_crypto.scanner import run_scan  # noqa: E402


def _floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        return value
    steps = (value / increment).quantize(Decimal("1"), rounding=ROUND_DOWN)
    return steps * increment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live", action="store_true",
        help="Execute exit sells. Without this flag, just shows what would be sold.",
    )
    args = parser.parse_args()

    configure_logging(level=logging.INFO)
    logger = logging.getLogger("check_exits")

    env = load_env_or_die(["CDP_KEY_FILE_PATH", "ALLOWED_PORTFOLIO_UUID"])
    cdp_key = load_cdp_key_or_die(env["CDP_KEY_FILE_PATH"])
    client = CoinbaseClient(cdp_key)
    portfolio_uuid = env["ALLOWED_PORTFOLIO_UUID"]

    # 1) Get current holdings (available + hold, since stops lock balances).
    accounts = client.get_accounts()
    holdings = parse_holdings(accounts)

    if not holdings:
        print("No crypto holdings. Nothing to check.")
        return 0

    # 2) Run scanner to get Donchian exit levels.
    logger.info("Running scanner for exit levels...")
    df = run_scan(client)

    # 3) Find positions where close <= S1 exit low (10-day Donchian low).
    exits: list[dict[str, Any]] = []
    for currency, held_amount in sorted(holdings.items()):
        product_id = f"{currency}-USD"
        row = df[df["product_id"] == product_id]
        if row.empty:
            continue

        close = float(row.iloc[0]["close"])
        s1_low = float(row.iloc[0]["s1_low"])
        s1_signal = row.iloc[0]["s1_signal"]

        # Exit condition: close at or below the 10-day Donchian low.
        if close <= s1_low or s1_signal == "EXIT":
            exec_pid = f"{currency}-USDC"
            exits.append({
                "currency": currency,
                "product_id": product_id,
                "exec_pid": exec_pid,
                "held_amount": held_amount,
                "close": close,
                "s1_low": s1_low,
                "signal": s1_signal,
            })

    if not exits:
        print(f"Checked {len(holdings)} positions — no exits triggered.")
        return 0

    # 4) Print exit summary.
    print(f"\n=== EXIT SIGNALS ({len(exits)}) ===")
    print(f"{'asset':10s}  {'held':>14s}  {'close':>10s}  {'10d low':>10s}  {'signal':6s}  {'pair':12s}")
    print("-" * 80)
    for e in exits:
        print(
            f"{e['currency']:10s}  {str(e['held_amount']):>14s}  "
            f"${e['close']:>8.4f}  ${e['s1_low']:>8.4f}  "
            f"{e['signal']:6s}  {e['exec_pid']:12s}"
        )

    if not args.live:
        print("\n  DRY RUN — no orders placed. Add --live to execute.")
        return 0

    # 5) Execute exits: sell at market, then cancel resting stop-loss.
    print(f"\n  Executing {len(exits)} exit(s)...")
    audit = AuditStore(DEFAULT_AUDIT_DB_PATH)
    success_count = 0

    for e in exits:
        currency = e["currency"]
        exec_pid = e["exec_pid"]

        # Fetch product details for base_size rounding.
        try:
            details = client.get_product_details(exec_pid)
            base_inc = Decimal(str(details["base_increment"]))
        except (CoinbaseClientError, KeyError, ValueError) as exc:
            print(f"  {currency:10s}  ERROR fetching details: {exc}")
            continue

        base_size = _floor_to_increment(e["held_amount"], base_inc)
        if base_size <= 0:
            print(f"  {currency:10s}  SKIP — held amount rounds to 0")
            continue

        # Place market sell.
        sell_order_id = f"turtle-exit-{uuid.uuid4().hex}"
        sell_intent = {
            "type": "DONCHIAN_EXIT",
            "product_id": e["product_id"],
            "exec_pid": exec_pid,
            "base_size": str(base_size),
            "close": str(e["close"]),
            "s1_low": str(e["s1_low"]),
        }
        row_id = audit.insert_intent(
            product_id=e["product_id"],
            client_order_id=sell_order_id,
            intent=sell_intent,
            dry_run=False,
        )

        try:
            response = client.place_market_sell(
                product_id=exec_pid,
                base_size=base_size,
                retail_portfolio_id=portfolio_uuid,
                client_order_id=sell_order_id,
            )
        except CoinbaseClientError as exc:
            audit.update_status(row_id, status="errored", error_text=str(exc))
            print(f"  {currency:10s}  SELL ERROR: {exc}")
            continue

        if isinstance(response, dict) and response.get("success") is not False:
            audit.update_status(row_id, status="filled", response=response)
            print(f"  {currency:10s}  SOLD {base_size} at market")
            success_count += 1
        else:
            err = response if not isinstance(response, dict) else (
                response.get("error_response") or response
            )
            audit.update_status(row_id, status="rejected", response=response, error_text=str(err))
            print(f"  {currency:10s}  SELL REJECTED: {err}")
            continue

        # Cancel resting stop-loss for this pair.
        try:
            open_orders = client.list_orders(
                product_id=exec_pid,
                order_status=["OPEN"],
            )
            stop_ids = [
                o["order_id"] for o in open_orders
                if o.get("side") == "SELL" and "stop" in str(o.get("order_configuration", {})).lower()
            ]
            if stop_ids:
                client.cancel_orders(stop_ids)
                print(f"  {currency:10s}  CANCELLED {len(stop_ids)} stop order(s)")
            else:
                print(f"  {currency:10s}  (no resting stop found to cancel)")
        except CoinbaseClientError as exc:
            print(f"  {currency:10s}  CANCEL WARNING: {exc} — check manually")

    print(f"\n  {success_count}/{len(exits)} exits executed.")
    audit.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
