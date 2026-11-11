"""Tests for the integrity checker."""
from __future__ import annotations

import sqlite3

import invented
import pytest

from desk import integrity, model, store


# Track which invariants we've tested
_TESTED_INVARIANTS = {
    "exit_exceeds_open_size",
    "event_before_trade_started",
    "dangling_correction",
    "closed_trade_holding_size",
    "trade_with_no_start",
    "exit_without_entry",
}


@pytest.fixture
def con(tmp_path):
    """A fresh database for each test. Returns (path, connection)."""
    db = tmp_path / "test.db"
    store.create(db)
    return db, store.open(db)


def _trade(con, trade_id, *, side="long", ticker=None):
    """Add a trade with standard fields."""
    store.add_trade(
        con,
        trade_id=trade_id,
        account="paper",
        run_id=None,
        ticker=ticker or invented.TICKERS[0],
        instrument="stock",
        point_value=1.0,
        currency="USD",
        side=side,
    )


def _enter(con, trade_id, *, size=100):
    """Add an entered event."""
    return store.append(
        con,
        kind="entered",
        actor="ing",
        source="chat",
        trade_id=trade_id,
        payload={
            "entry": 100.0,
            "stop": 95.0,
            "target": 110.0,
            "size": size,
            "horizon": "2w",
            "setup": "test-setup",
            "thesis": "test thesis",
        },
    )


def _exit(con, trade_id, *, size=10):
    """Add an exited event."""
    return store.append(
        con,
        kind="exited",
        actor="ing",
        source="chat",
        trade_id=trade_id,
        payload={"price": 105.0, "size": size},
    )


def _close(con, trade_id):
    """Add a closed event."""
    return store.append(
        con,
        kind="closed",
        actor="ing",
        source="chat",
        trade_id=trade_id,
        payload={},
    )


def test_an_exit_larger_than_the_open_size_is_a_failure(con):
    """Exit size exceeding open position is an integrity violation."""
    db, conn = con
    _trade(conn, invented.TEST_TRADE_ID)
    _enter(conn, invented.TEST_TRADE_ID, size=100)
    _exit(conn, invented.TEST_TRADE_ID, size=120)  # exit more than entered

    result = integrity.run(conn)
    assert result == 1, "Should fail when exit exceeds open size"


def test_an_event_before_its_trades_checked_or_imported_event_is_a_failure(con):
    """A trade event must come after the trade's checked or imported event."""
    db, conn = con
    _trade(conn, invented.TEST_TRADE_ID)
    # Insert a checked event with a later timestamp
    store.append(
        conn,
        kind="checked",
        actor="ing",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={},
        at="2026-11-10T10:00:00Z",
    )
    # Insert an entered event with an earlier timestamp (but later event id)
    # This is actually caught by id ordering, not timestamp
    # The real issue is: an event with id < the checked/imported event's id
    # But that's impossible to create through the API since ids are sequential
    # So we need to test: a trade has events but no checked/imported event at all
    # Actually, re-reading the brief: "an event before its trade's checked or imported event"
    # This means: an event exists when there's NO checked/imported event yet
    # OR the event's id is lower than the checked/imported event's id

    # Let me rewrite: create entered event, then checked event
    # The entered (id 2) comes before checked (id 3) in terms of event IDs
    conn.close()
    db2 = db.parent / "test2.db"
    store.create(db2)
    conn2 = store.open(db2)

    _trade(conn2, invented.TEST_TRADE_ID)
    _enter(conn2, invented.TEST_TRADE_ID)  # id 1
    store.append(
        conn2,
        kind="checked",
        actor="ing",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={},
    )  # id 2

    result = integrity.run(conn2)
    assert result == 1, "Should fail when event comes before checked/imported"


def test_a_correction_naming_a_missing_event_is_a_failure(con):
    """A correction event must reference an existing event.

    SQLite foreign key enforcement is per-connection and off by default.
    This invariant catches dangling corrects references.
    """
    db, conn = con
    _trade(conn, invented.TEST_TRADE_ID)
    _enter(conn, invented.TEST_TRADE_ID)  # This will be event id 1

    # Insert a correction naming a non-existent event id
    # We need to bypass foreign key checks to do this
    conn.close()

    # Open without foreign keys to insert a bad row
    bad_conn = sqlite3.connect(db)
    # Do NOT enable foreign keys
    # Insert with explicit id=10 and corrects=5 (which doesn't exist)
    # This satisfies the CHECK (5 < 10) but violates the FK
    bad_conn.execute(
        """
        INSERT INTO events (id, at, actor, source, kind, trade_id, corrects, payload)
        VALUES (10, '2026-11-10T12:00:00Z', 'ing', 'chat', 'corrected', ?, 5, '{}')
        """,
        (invented.TEST_TRADE_ID,),
    )
    bad_conn.commit()
    bad_conn.close()

    # Now open properly and check
    conn = store.open(db)
    result = integrity.run(conn)
    assert result == 1, "Should fail when corrects references missing event"


def test_a_closed_trade_still_holding_size_is_a_failure(con):
    """A trade marked closed must have zero open size."""
    db, conn = con
    _trade(conn, invented.TEST_TRADE_ID)
    _enter(conn, invented.TEST_TRADE_ID, size=100)
    _exit(conn, invented.TEST_TRADE_ID, size=50)  # only partial exit
    _close(conn, invented.TEST_TRADE_ID)  # but marked as closed

    result = integrity.run(conn)
    assert result == 1, "Should fail when closed trade still holds size"


def test_a_trade_with_no_checked_or_imported_event_is_a_failure(con):
    """Every trade must have a checked or imported event."""
    db, conn = con
    _trade(conn, invented.TEST_TRADE_ID)
    _enter(conn, invented.TEST_TRADE_ID)  # entered without checked/imported

    result = integrity.run(conn)
    assert result == 1, "Should fail when trade has no checked/imported event"


def test_an_exit_with_no_preceding_entry_is_a_failure(con):
    """An exited event must come after an entered event."""
    db, conn = con
    _trade(conn, invented.TEST_TRADE_ID)
    # Add a checked event so the trade is "started"
    store.append(
        conn,
        kind="checked",
        actor="ing",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={},
    )
    # Exit without entering
    _exit(conn, invented.TEST_TRADE_ID)

    result = integrity.run(conn)
    assert result == 1, "Should fail when exit has no preceding entry"


def test_every_invariant_has_a_test(con):
    """The guard against adding an invariant with nothing exercising it."""
    db, conn = con
    assert set(integrity.INVARIANTS) == _TESTED_INVARIANTS
