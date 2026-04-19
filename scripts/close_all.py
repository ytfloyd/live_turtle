"""
scripts/close_all.py

Emergency close: sells 100% of every open crypto position at market
and cancels all resting stop-loss orders.

Usage:
    uv run python scripts/close_all.py              # dry-run (shows what would be sold)
    uv run python scripts/close_all.py --live       # execute all sells
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import configure_logging, load_cdp_key_or_die, load_env_or_die, parse_holdings  # noqa: E402

from turtle_crypto.audit import AuditStore  # noqa: E402
from turtle_crypto.coinbase_client import CoinbaseClient, CoinbaseClientError  # noqa: E402
from turtle_crypto.config import DEFAULT_AUDIT_DB_PATH  # noqa: E402


def _floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        return value
    steps = (value / increment).quantize(Decimal("1"), rounding=ROUND_DOWN)
    return steps * increment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live", action="store_true",
        help="Actually sell everything. Without this flag, just shows what would be sold.",
    )
    args = parser.parse_args()

    configure_logging(level=logging.INFO)
    logger = logging.getLogger("close_all")

    env = load_env_or_die(["CDP_KEY_FILE_PATH", "ALLOWED_PORTFOLIO_UUID"])
    cdp_key = load_cdp_key_or_die(env["CDP_KEY_FILE_PATH"])
    client = CoinbaseClient(cdp_key)
    portfolio_uuid = env["ALLOWED_PORTFOLIO_UUID"]

    # 1) Get holdings.
    accounts = client.get_accounts()
    holdings = parse_holdings(accounts)

    if not holdings:
        print("No crypto holdings. Nothing to close.")
        return 0

    print(f"\n=== CLOSE ALL — {len(holdings)} POSITION(S) ===")
    for cur, amt in sorted(holdings.items()):
        print(f"  {cur:12s}  {amt}")

    if not args.live:
        print("\n  DRY RUN — no orders placed. Add --live to execute.")
        return 0

    confirm = input("\n  Type 'CLOSE ALL' to confirm: ").strip()
    if confirm != "CLOSE ALL":
        print("  Aborted.")
        return 0

    # 2) Cancel ALL open orders first (stops, limits, everything).
    print("\n  Cancelling all open orders...")
    try:
        open_orders = client.list_orders(order_status=["OPEN"])
        if open_orders:
            order_ids = [o["order_id"] for o in open_orders if "order_id" in o]
            if order_ids:
                client.cancel_orders(order_ids)
                print(f"  Cancelled {len(order_ids)} open order(s)")
        else:
            print("  No open orders to cancel")
    except CoinbaseClientError as exc:
        print(f"  WARNING: cancel failed: {exc} — continuing with sells")

    # 3) Sell every position at market.
    print(f"\n  Selling {len(holdings)} position(s)...")
    audit = AuditStore(DEFAULT_AUDIT_DB_PATH)
    success_count = 0

    for currency, held_amount in sorted(holdings.items()):
        exec_pid = f"{currency}-USDC"

        # Fetch product details for base_size rounding.
        try:
            details = client.get_product_details(exec_pid)
            base_inc = Decimal(str(details["base_increment"]))
        except (CoinbaseClientError, KeyError, ValueError) as exc:
            print(f"  {currency:12s}  ERROR fetching details: {exc}")
            continue

        base_size = _floor_to_increment(held_amount, base_inc)
        if base_size <= 0:
            print(f"  {currency:12s}  SKIP — rounds to 0")
            continue

        sell_order_id = f"turtle-closeall-{uuid.uuid4().hex}"
        row_id = audit.insert_intent(
            product_id=f"{currency}-USD",
            client_order_id=sell_order_id,
            intent={"type": "CLOSE_ALL", "base_size": str(base_size), "exec_pid": exec_pid},
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
            print(f"  {currency:12s}  ERROR: {exc}")
            continue

        if isinstance(response, dict) and response.get("success") is not False:
            audit.update_status(row_id, status="filled", response=response)
            print(f"  {currency:12s}  SOLD {base_size}")
            success_count += 1
        else:
            err = response if not isinstance(response, dict) else (
                response.get("error_response") or response
            )
            audit.update_status(row_id, status="rejected", response=response, error_text=str(err))
            print(f"  {currency:12s}  REJECTED: {err}")

    print(f"\n  {success_count}/{len(holdings)} positions closed.")
    audit.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
