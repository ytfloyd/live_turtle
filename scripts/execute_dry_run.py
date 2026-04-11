"""
scripts/execute_dry_run.py

Runs scanner + trade sheet, wires the executor in dry-run mode, prints the
exact JSON payload that WOULD be posted for each order, and logs every intent
to the audit DB with status='dry_run'.

Makes zero state-changing API calls. Does perform read-only sanity checks
(auth round-trip, portfolio binding, balance drift, audit DB writable).

Usage:
    uv run python scripts/execute_dry_run.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # noqa: E402
    configure_logging,
    fetch_product_details_bulk,
    load_cdp_key_or_die,
    load_env_or_die,
)

from turtle_crypto.audit import AuditStore  # noqa: E402
from turtle_crypto.coinbase_client import CoinbaseClient  # noqa: E402
from turtle_crypto.config import ACCOUNT_SIZE, DEFAULT_AUDIT_DB_PATH  # noqa: E402
from turtle_crypto.executor import Executor, ExecutorError  # noqa: E402
from turtle_crypto.scanner import print_scanner_tables, run_scan  # noqa: E402
from turtle_crypto.trade_sheet import build_trade_sheet, print_trade_sheet  # noqa: E402


def main() -> int:
    configure_logging(level=logging.INFO)
    logger = logging.getLogger("execute_dry_run")

    env = load_env_or_die(["CDP_KEY_FILE_PATH", "ALLOWED_PORTFOLIO_UUID"])
    cdp_key = load_cdp_key_or_die(env["CDP_KEY_FILE_PATH"])
    client = CoinbaseClient(cdp_key)

    # Scanner + trade sheet.
    logger.info("running scanner…")
    df = run_scan(client)
    print_scanner_tables(df)

    # Fetch real product details for all candidate pairs before sizing.
    candidate_ids = df["product_id"].tolist()
    details = fetch_product_details_bulk(client, candidate_ids)
    sheet = build_trade_sheet(df, details)
    print_trade_sheet(sheet)

    # Executor in dry-run mode.
    audit = AuditStore(DEFAULT_AUDIT_DB_PATH)
    executor = Executor(
        client=client,
        audit=audit,
        portfolio_uuid=env["ALLOWED_PORTFOLIO_UUID"],
        dry_run=True,
    )
    try:
        executor.run_sanity_checks()
    except ExecutorError as exc:
        logger.error("sanity check failed: %s", exc)
        return 2

    all_orders = list(sheet.active_orders) + list(sheet.resting_orders)
    if not all_orders:
        logger.info("no orders to simulate")
        audit.close()
        return 0

    print(f"\n=== DRY-RUN SIMULATING {len(all_orders)} ORDER(S) ===")
    for order in all_orders:
        intent = executor._build_intent(order, client_order_id="dry-preview")  # type: ignore[attr-defined]
        print()
        print(f"--- {order.asset} ({order.classification}, {order.order_type}) ---")
        print(f"  base_size        {order.base_size}")
        print(f"  notional_usd     ${order.notional_usd:,.2f}")
        print(f"  risk_usd         ${order.risk_usd:,.2f}")
        print(f"  entry_price      ${order.entry_price}")
        print(f"  stop_loss (2N)   ${order.stop_loss_price}")
        print(f"  %account         {(order.notional_usd / ACCOUNT_SIZE * 100):.2f}%")
        print("  payload (redacted client_order_id):")
        print(json.dumps(intent, indent=4, default=str))

        try:
            executor.place_order(order)
        except ExecutorError as exc:
            logger.error("executor refused %s: %s", order.asset, exc)

    notional, risk, count = executor.session_totals
    print("\n=== DRY-RUN SUMMARY ===")
    print(f"  simulated orders  {count}")
    print(f"  total notional    ${notional:,.2f}")
    print(f"  total risk        ${risk:,.2f}")
    print(f"  audit db          {audit.path}")
    audit.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
