"""
scripts/run_scanner.py

Read-only scanner + trade sheet. No network writes, no auth, no orders. Safe
to run any time without any risk of touching the portfolio.

Usage:
    uv run python scripts/run_scanner.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import configure_logging  # noqa: E402

from turtle_crypto.coinbase_client import CoinbaseClient  # noqa: E402
from turtle_crypto.scanner import print_scanner_tables, run_scan  # noqa: E402
from turtle_crypto.trade_sheet import build_trade_sheet, print_trade_sheet  # noqa: E402


def main() -> int:
    configure_logging(level=logging.INFO)
    logger = logging.getLogger("run_scanner")

    client = CoinbaseClient(None)  # public-only

    logger.info("running scanner…")
    df = run_scan(client)
    print_scanner_tables(df)

    # To build the trade sheet we need per-product details for rounding.
    # Run scanner mode does NOT make authed calls, so we build the sheet with
    # a conservative default increment (0.00000001) for display purposes only —
    # the executor will re-fetch real details before placing orders.
    candidate_ids = df["product_id"].tolist()
    fake_details = {
        pid: {"base_increment": "0.00000001", "base_min_size": "0.00000001"}
        for pid in candidate_ids
    }
    sheet = build_trade_sheet(df, fake_details)
    print_trade_sheet(sheet)

    logger.info(
        "scanner done — %d pairs, %d active entries",
        len(df),
        len(sheet.active_orders),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
