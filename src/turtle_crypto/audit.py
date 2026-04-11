"""
SQLite audit log for order intents and responses.

Tables
------
orders:
    id             INTEGER PRIMARY KEY AUTOINCREMENT
    ts             TEXT    ISO8601 UTC timestamp
    client_order_id TEXT
    product_id     TEXT
    intent_json    TEXT    serialized payload BEFORE the network call
    dry_run        INTEGER 0/1
    status         TEXT    'intent' -> 'dry_run' | 'filled' | 'rejected' |
                           'errored' | 'user_skipped'
    response_json  TEXT    serialized response from Coinbase (or null)
    error_text     TEXT    error message on failure (or null)

state:
    key            TEXT PRIMARY KEY
    value          TEXT

Keys in `state`:
    halt_flag         '1' means executor is halted and refuses to place orders.
    halt_reason       human-readable reason string (written alongside halt).
    daily_count_date  the UTC date for which `daily_count` applies.
    daily_count       integer count of non-dry-run orders placed today.

Design notes
------------
- Every order intent is inserted BEFORE the network call with status='intent'.
  If the script crashes between insert and response, you still have a row
  recording what was attempted.
- `update_status` takes the row id returned by `insert_intent` and updates it
  after the call completes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

logger = logging.getLogger(__name__)

Status = Literal["intent", "dry_run", "filled", "rejected", "errored", "user_skipped"]


_SCHEMA_ORDERS = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    client_order_id TEXT,
    product_id TEXT NOT NULL,
    intent_json TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    status TEXT NOT NULL,
    response_json TEXT,
    error_text TEXT
)
"""

_SCHEMA_STATE = """
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class OrderRow:
    id: int
    ts: str
    client_order_id: str | None
    product_id: str
    intent_json: str
    dry_run: bool
    status: str
    response_json: str | None
    error_text: str | None


class AuditStore:
    """
    Thin wrapper around the audit SQLite database.

    Not thread-safe. Instantiate once per process at the start of an entry
    point script and reuse.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_SCHEMA_ORDERS)
        self._conn.execute(_SCHEMA_STATE)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def insert_intent(
        self,
        *,
        product_id: str,
        client_order_id: str,
        intent: dict[str, Any],
        dry_run: bool,
    ) -> int:
        ts = datetime.now(timezone.utc).isoformat()
        intent_json = json.dumps(intent, sort_keys=True, default=str)
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO orders (ts, client_order_id, product_id, intent_json, dry_run, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts, client_order_id, product_id, intent_json, int(dry_run), "intent"),
            )
            row_id = cur.lastrowid
        if row_id is None:
            raise RuntimeError("audit insert did not return a row id")
        return int(row_id)

    def update_status(
        self,
        row_id: int,
        *,
        status: Status,
        response: dict[str, Any] | None = None,
        error_text: str | None = None,
    ) -> None:
        response_json = json.dumps(response, sort_keys=True, default=str) if response else None
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE orders SET status=?, response_json=?, error_text=? WHERE id=?",
                (status, response_json, error_text, row_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"audit update affected {cur.rowcount} rows for id={row_id}")

    def recent_orders(self, limit: int = 20) -> list[OrderRow]:
        cur = self._conn.execute(
            "SELECT id, ts, client_order_id, product_id, intent_json, dry_run, status, response_json, error_text "
            "FROM orders ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        out: list[OrderRow] = []
        for row in cur.fetchall():
            out.append(
                OrderRow(
                    id=row[0],
                    ts=row[1],
                    client_order_id=row[2],
                    product_id=row[3],
                    intent_json=row[4],
                    dry_run=bool(row[5]),
                    status=row[6],
                    response_json=row[7],
                    error_text=row[8],
                )
            )
        return out

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _get_state(self, key: str) -> str | None:
        cur = self._conn.execute("SELECT value FROM state WHERE key=?", (key,))
        row = cur.fetchone()
        return row[0] if row else None

    def _set_state(self, key: str, value: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def is_halted(self) -> bool:
        return self._get_state("halt_flag") == "1"

    def halt_reason(self) -> str | None:
        return self._get_state("halt_reason")

    def set_halt(self, reason: str) -> None:
        self._set_state("halt_flag", "1")
        self._set_state("halt_reason", reason)
        logger.error("AUDIT: HALT set with reason: %s", reason)

    def clear_halt(self) -> None:
        self._set_state("halt_flag", "0")
        self._set_state("halt_reason", "")
        logger.warning("AUDIT: HALT cleared")

    # Daily order counter, resets at UTC midnight.
    def daily_order_count(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        stored_date = self._get_state("daily_count_date")
        if stored_date != today:
            return 0
        raw = self._get_state("daily_count")
        if raw is None:
            return 0
        try:
            return int(raw)
        except ValueError:
            return 0

    def increment_daily_order_count(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        stored_date = self._get_state("daily_count_date")
        if stored_date != today:
            self._set_state("daily_count_date", today)
            self._set_state("daily_count", "0")
        current = self.daily_order_count()
        new = current + 1
        self._set_state("daily_count", str(new))
        return new
