"""
Shared helpers for entry-point scripts.

Keeps .env loading, logging configuration, and product-detail fetching in one
place so the three entry points stay small.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from turtle_crypto.coinbase_auth import CDPKey
from turtle_crypto.coinbase_client import CoinbaseClient, CoinbaseClientError


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def load_env_or_die(required: list[str]) -> dict[str, str]:
    """
    Load .env from the repo root and verify every required key is present.
    Dies with a clear message if anything is missing.
    """
    repo_root = Path(__file__).resolve().parents[1]
    env_path = repo_root / ".env"
    if not env_path.exists():
        sys.exit(
            f"ERROR: {env_path} not found. Copy .env.example to .env and fill it in."
        )
    load_dotenv(env_path)
    values: dict[str, str] = {}
    missing: list[str] = []
    for k in required:
        v = os.environ.get(k)
        if v is None or not v.strip():
            missing.append(k)
        else:
            values[k] = v
    if missing:
        sys.exit(f"ERROR: missing required .env keys: {', '.join(missing)}")
    return values


def load_cdp_key_or_die(path: str) -> CDPKey:
    try:
        return CDPKey.from_file(path)
    except Exception as exc:
        sys.exit(f"ERROR loading CDP key at {path}: {exc}")


def parse_holdings(accounts: list[dict]) -> dict[str, "Decimal"]:
    """
    Extract non-zero crypto holdings from the accounts list.

    Sums both available_balance and hold for each account — resting
    stop-loss orders move the balance from available into hold.
    """
    from decimal import Decimal

    _CASH = frozenset({"USD", "USDC"})
    holdings: dict[str, Decimal] = {}
    for account in accounts:
        if not isinstance(account, dict):
            continue
        currency = None
        total = Decimal("0")
        for field in ("available_balance", "hold"):
            bal = account.get(field)
            if not isinstance(bal, dict):
                continue
            if currency is None:
                currency = bal.get("currency")
            value_raw = bal.get("value")
            if value_raw is None:
                continue
            try:
                total += Decimal(str(value_raw))
            except (ArithmeticError, ValueError):
                continue
        if not currency or currency in _CASH:
            continue
        if total > 0:
            holdings[currency] = total
    return holdings


def fetch_product_details_bulk(
    client: CoinbaseClient, product_ids: list[str]
) -> dict[str, dict]:
    """
    Fetch product_details for every product_id the trade sheet needs. Halts
    the whole script on any individual failure — we will NOT trade against a
    pair we can't size properly.
    """
    out: dict[str, dict] = {}
    for pid in product_ids:
        try:
            out[pid] = client.get_product_details(pid)
        except CoinbaseClientError as exc:
            sys.exit(f"ERROR fetching product details for {pid}: {exc}")
    return out
