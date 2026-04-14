"""
scripts/execute_live.py

Live execution with per-order interactive confirmation. This is the only
script that actually places real orders against Coinbase.

Usage:
    uv run python scripts/execute_live.py
    uv run python scripts/execute_live.py --reset-halt    # clear halt then exit
    uv run python scripts/execute_live.py --auto          # skip confirmation (dangerous)

Per-order prompt:
    Type 'EXECUTE <ASSET>' to confirm, 'SKIP' to skip, or 'ABORT' to halt.
"""

from __future__ import annotations

import argparse
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
from turtle_crypto.config import DEFAULT_AUDIT_DB_PATH  # noqa: E402
from turtle_crypto.executor import (  # noqa: E402
    CapViolation,
    Executor,
    ExecutorError,
    HaltedError,
    SanityCheckError,
)
from turtle_crypto.scanner import print_scanner_tables, run_scan  # noqa: E402
from turtle_crypto.trade_sheet import (  # noqa: E402
    TradeOrder,
    build_trade_sheet,
    print_trade_sheet,
)


def _prompt_confirmation(order: TradeOrder) -> str:
    """
    Block until the user types one of:
        EXECUTE <ASSET>   -> execute this order
        SKIP              -> skip this order
        ABORT             -> stop the whole run immediately
    Anything else prints a hint and re-prompts.
    """
    expected = f"EXECUTE {order.asset}"
    while True:
        try:
            entered = input(
                f"\n  Type '{expected}' to confirm, 'SKIP' or 'ABORT': "
            ).strip()
        except EOFError:
            return "ABORT"
        if entered == expected:
            return "EXECUTE"
        if entered == "SKIP":
            return "SKIP"
        if entered == "ABORT":
            return "ABORT"
        print("  (no match — exact string required)")


def _print_order_preview(order: TradeOrder) -> None:
    print()
    print(f"--- {order.asset} ({order.classification}, {order.order_type}) ---")
    print(f"  base_size        {order.base_size}")
    print(f"  notional_usd     ${order.notional_usd:,.2f}")
    print(f"  risk_usd         ${order.risk_usd:,.2f}")
    print(f"  entry_price      ${order.entry_price}")
    print(f"  stop_loss (2N)   ${order.stop_loss_price}")
    if order.order_type == "STOP_LIMIT_BUY":
        print(f"  stop_trigger     ${order.stop_trigger_price}")
        print(f"  stop_limit       ${order.stop_limit_price}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reset-halt", action="store_true",
        help="Clear the halt flag in the audit DB and exit.",
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="Skip per-order confirmation. DO NOT use without dry-run validation first.",
    )
    args = parser.parse_args()

    configure_logging(level=logging.INFO)
    logger = logging.getLogger("execute_live")

    # Reset-halt mode: touches the audit DB only.
    if args.reset_halt:
        audit = AuditStore(DEFAULT_AUDIT_DB_PATH)
        if audit.is_halted():
            reason = audit.halt_reason() or "(unknown)"
            print(f"Current halt reason: {reason}")
            confirm = input("Type 'CLEAR HALT' to confirm: ").strip()
            if confirm == "CLEAR HALT":
                audit.clear_halt()
                print("Halt cleared.")
            else:
                print("Aborted — halt still in place.")
        else:
            print("No halt flag set.")
        audit.close()
        return 0

    env = load_env_or_die(["CDP_KEY_FILE_PATH", "ALLOWED_PORTFOLIO_UUID"])
    cdp_key = load_cdp_key_or_die(env["CDP_KEY_FILE_PATH"])
    client = CoinbaseClient(cdp_key)

    df = run_scan(client)
    print_scanner_tables(df)

    candidate_ids = df["product_id"].tolist()
    details = fetch_product_details_bulk(client, candidate_ids)
    sheet = build_trade_sheet(df, details)
    print_trade_sheet(sheet)

    audit = AuditStore(DEFAULT_AUDIT_DB_PATH)
    executor = Executor(
        client=client,
        audit=audit,
        portfolio_uuid=env["ALLOWED_PORTFOLIO_UUID"],
        dry_run=False,
    )
    try:
        executor.run_sanity_checks()
    except HaltedError as exc:
        logger.error("%s", exc)
        audit.close()
        return 2
    except SanityCheckError as exc:
        logger.error("sanity check failed: %s", exc)
        audit.close()
        return 2

    all_orders = list(sheet.active_orders)

    # Filter out assets already in the portfolio.
    all_orders = executor.filter_already_held(all_orders)

    if not all_orders:
        print("\nNo new orders to execute (all signals already held or none active).")
        audit.close()
        return 0

    print(f"\n=== LIVE EXECUTION — {len(all_orders)} NEW ORDER(S) TO PROCESS ===")

    # Show all orders as a summary table first.
    from tabulate import tabulate as _tabulate
    summary_table = []
    for o in all_orders:
        summary_table.append([
            o.asset,
            o.classification,
            f"${o.notional_usd:,.2f}",
            f"${o.risk_usd:,.2f}",
            f"${o.entry_price:,.4f}",
            f"${o.stop_loss_price:,.4f}",
            f"{o.rank_score:.1f}",
        ])
    print(_tabulate(
        summary_table,
        headers=["asset", "class", "notional", "risk", "entry", "stop (2N)", "rank"],
        tablefmt="simple",
    ))
    total_n = sum(o.notional_usd for o in all_orders)
    total_r = sum(o.risk_usd for o in all_orders)
    print(f"\n  total notional  ${total_n:,.2f}   total risk  ${total_r:,.2f}")

    if args.auto:
        print("  --auto flag set — executing all without confirmation")
        choice = "EXECUTE ALL"
    else:
        print("\n  Type 'EXECUTE ALL' to confirm all orders")
        print("  Type 'ABORT' to cancel")
        print("  Type 'ONE-BY-ONE' for per-order confirmation")
        try:
            choice = input("\n  > ").strip()
        except EOFError:
            choice = "ABORT"

    if choice == "ABORT":
        logger.warning("user aborted")
        audit.close()
        return 0

    one_by_one = choice == "ONE-BY-ONE"
    if choice not in ("EXECUTE ALL", "ONE-BY-ONE"):
        print("  (no match — must be 'EXECUTE ALL', 'ONE-BY-ONE', or 'ABORT')")
        audit.close()
        return 1

    results: list[tuple[str, str]] = []
    for order in all_orders:
        if one_by_one:
            _print_order_preview(order)
            confirm = _prompt_confirmation(order)
            if confirm == "ABORT":
                logger.warning("user aborted run at %s", order.asset)
                results.append((order.asset, "aborted"))
                break
            if confirm == "SKIP":
                audit.insert_intent(
                    product_id=order.product_id,
                    client_order_id="skipped-" + order.asset,
                    intent={"classification": order.classification},
                    dry_run=False,
                )
                recent = audit.recent_orders(limit=5)
                if recent and recent[0].product_id == order.product_id:
                    audit.update_status(recent[0].id, status="user_skipped")
                results.append((order.asset, "skipped"))
                continue

        try:
            result = executor.place_order(order)
            results.append((order.asset, result.status))
            print(f"  {order.asset:12s}  FILLED  ${order.notional_usd:,.2f}")
        except CapViolation as exc:
            logger.error("cap violation for %s: %s", order.asset, exc)
            results.append((order.asset, "cap_violation"))
            break
        except ExecutorError as exc:
            logger.error("executor error for %s: %s", order.asset, exc)
            results.append((order.asset, "errored"))
            break

    notional, risk, count = executor.session_totals
    print("\n=== LIVE RUN SUMMARY ===")
    for asset, status in results:
        print(f"  {asset:10s}  {status}")
    print(f"  deployed notional ${notional:,.2f}")
    print(f"  deployed risk     ${risk:,.2f}")
    print(f"  filled orders     {count}")
    if audit.is_halted():
        print(f"  HALTED: {audit.halt_reason()}")
    audit.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
