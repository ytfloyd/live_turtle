"""Unit tests for the audit SQLite store."""

from __future__ import annotations

from pathlib import Path

import pytest

from turtle_crypto.audit import AuditStore


@pytest.fixture()
def audit(tmp_path: Path) -> AuditStore:
    store = AuditStore(tmp_path / "audit.db")
    yield store
    store.close()


def test_insert_intent_returns_id(audit: AuditStore) -> None:
    rid = audit.insert_intent(
        product_id="BTC-USD",
        client_order_id="coid-1",
        intent={"foo": "bar"},
        dry_run=True,
    )
    assert rid > 0
    rows = audit.recent_orders()
    assert len(rows) == 1
    assert rows[0].id == rid
    assert rows[0].status == "intent"
    assert rows[0].product_id == "BTC-USD"
    assert rows[0].dry_run is True


def test_update_status_filled(audit: AuditStore) -> None:
    rid = audit.insert_intent(
        product_id="ETH-USD",
        client_order_id="coid-2",
        intent={"x": 1},
        dry_run=False,
    )
    audit.update_status(rid, status="filled", response={"order_id": "abc"})
    row = audit.recent_orders()[0]
    assert row.status == "filled"
    assert row.response_json is not None
    assert "abc" in row.response_json


def test_update_status_unknown_id_raises(audit: AuditStore) -> None:
    with pytest.raises(RuntimeError, match="affected 0"):
        audit.update_status(999_999, status="filled")


def test_halt_flag_lifecycle(audit: AuditStore) -> None:
    assert audit.is_halted() is False
    audit.set_halt("portfolio balance drift")
    assert audit.is_halted() is True
    assert audit.halt_reason() == "portfolio balance drift"
    audit.clear_halt()
    assert audit.is_halted() is False


def test_daily_counter_starts_at_zero(audit: AuditStore) -> None:
    assert audit.daily_order_count() == 0


def test_daily_counter_increments(audit: AuditStore) -> None:
    assert audit.increment_daily_order_count() == 1
    assert audit.increment_daily_order_count() == 2
    assert audit.increment_daily_order_count() == 3
    assert audit.daily_order_count() == 3


def test_audit_db_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "audit.db"
    store1 = AuditStore(path)
    rid = store1.insert_intent(
        product_id="BTC-USD",
        client_order_id="coid-persist",
        intent={"a": "b"},
        dry_run=False,
    )
    store1.set_halt("testing")
    store1.close()

    store2 = AuditStore(path)
    try:
        assert store2.is_halted() is True
        assert store2.halt_reason() == "testing"
        rows = store2.recent_orders()
        assert any(r.id == rid for r in rows)
    finally:
        store2.close()
