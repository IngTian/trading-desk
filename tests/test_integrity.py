"""Tests for the integrity checker."""
from __future__ import annotations

import sqlite3

import invented
import pytest

from desk import integrity, store


@pytest.fixture
def con(tmp_path):
    """A fresh database for each test."""
    db = tmp_path / "test.db"
    store.create(db)
    conn = store.open(db)
    yield conn
    conn.close()


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


def _checked(con, trade_id):
    """Add a checked event."""
    return store.append(
        con,
        kind="checked",
        actor="owner",
        source="chat",
        trade_id=trade_id,
        payload={},
    )


def _enter(con, trade_id, *, size=100, entry=None, stop=None, target=None):
    """Add an entered event."""
    return store.append(
        con,
        kind="entered",
        actor="owner",
        source="chat",
        trade_id=trade_id,
        payload={
            "entry": entry or invented.PRICES["ACME"],
            "stop": stop or 95.0,
            "target": target or 110.0,
            "size": size,
            "horizon": "2w",
            "setup": "test-setup",
            "thesis": "test thesis",
        },
    )


def _exit(con, trade_id, *, size=10, price=None):
    """Add an exited event."""
    return store.append(
        con,
        kind="exited",
        actor="owner",
        source="chat",
        trade_id=trade_id,
        payload={"price": price or 105.0, "size": size},
    )


def _close(con, trade_id):
    """Add a closed event."""
    return store.append(
        con,
        kind="closed",
        actor="owner",
        source="chat",
        trade_id=trade_id,
        payload={},
    )


# Builder functions for parametrized test
def _build_exit_exceeds_open_size(con):
    """Build a log with an exit exceeding open position."""
    _trade(con, invented.TEST_TRADE_ID)
    _checked(con, invented.TEST_TRADE_ID)
    _enter(con, invented.TEST_TRADE_ID, size=100)
    _exit(con, invented.TEST_TRADE_ID, size=120)
    return con


def _build_event_before_trade_started(con):
    """Build a log with an event before checked/imported."""
    _trade(con, invented.TEST_TRADE_ID)
    _enter(con, invented.TEST_TRADE_ID)
    _checked(con, invented.TEST_TRADE_ID)
    return con


def _build_dangling_correction(db_path):
    """Build a log with a correction naming a missing event."""
    conn = store.open(db_path)
    _trade(conn, invented.TEST_TRADE_ID)
    _checked(conn, invented.TEST_TRADE_ID)
    _enter(conn, invented.TEST_TRADE_ID)
    conn.close()

    # Open without foreign keys to insert a bad row
    bad_conn = sqlite3.connect(db_path)
    bad_conn.execute(
        """
        INSERT INTO events (id, at, actor, source, kind, trade_id, corrects, payload)
        VALUES (10, '2026-11-10T12:00:00Z', 'owner', 'chat', 'corrected', ?, 5, '{}')
        """,
        (invented.TEST_TRADE_ID,),
    )
    bad_conn.commit()
    bad_conn.close()

    conn = store.open(db_path)
    return conn


def _build_closed_trade_holding_size(con):
    """Build a log with a closed trade still holding size."""
    _trade(con, invented.TEST_TRADE_ID)
    _checked(con, invented.TEST_TRADE_ID)
    _enter(con, invented.TEST_TRADE_ID, size=100)
    _exit(con, invented.TEST_TRADE_ID, size=50)
    _close(con, invented.TEST_TRADE_ID)
    return con


def _build_trade_with_no_start(con):
    """Build a log with a trade lacking checked/imported."""
    _trade(con, invented.TEST_TRADE_ID)
    _enter(con, invented.TEST_TRADE_ID)
    return con


def _build_exit_without_entry(con):
    """Build a log with an exit before any entry."""
    _trade(con, invented.TEST_TRADE_ID)
    _checked(con, invented.TEST_TRADE_ID)
    _exit(con, invented.TEST_TRADE_ID)
    return con


_BUILDERS = {
    "exit_exceeds_open_size": _build_exit_exceeds_open_size,
    "event_before_trade_started": _build_event_before_trade_started,
    "dangling_correction": _build_dangling_correction,
    "closed_trade_holding_size": _build_closed_trade_holding_size,
    "trade_with_no_start": _build_trade_with_no_start,
    "exit_without_entry": _build_exit_without_entry,
}


@pytest.mark.parametrize("name", sorted(integrity.INVARIANTS))
def test_each_invariant_fires_only_on_its_own_violation(name, tmp_path):
    """Each invariant fires on its own violation and not others."""
    # dangling_correction needs db_path for raw sqlite3 access
    if name == "dangling_correction":
        db = tmp_path / "test.db"
        store.create(db)
        conn = _BUILDERS[name](db)
    else:
        db = tmp_path / "test.db"
        store.create(db)
        conn = store.open(db)
        conn = _BUILDERS[name](conn)

    fired = [n for n, f in integrity.INVARIANTS.items() if f(conn)]
    assert fired == [name], f"Expected only {name} to fire, but got {fired}"


def test_a_healthy_log_returns_zero(con):
    """A well-formed log with no violations returns 0."""
    _trade(con, invented.TEST_TRADE_ID)
    _checked(con, invented.TEST_TRADE_ID)
    _enter(con, invented.TEST_TRADE_ID, size=100)
    _exit(con, invented.TEST_TRADE_ID, size=60)
    _exit(con, invented.TEST_TRADE_ID, size=40)
    _close(con, invented.TEST_TRADE_ID)

    result = integrity.run(con)
    assert result == 0, "A healthy log should return 0"


def test_a_corrected_check_after_entry_is_clean(con):
    """A check corrected after entry does not fire event_before_trade_started.

    Log: checked(1), entered(2), checked(3, corrects=1). The entry at event 2
    came after the original check at 1, so the ordering is fine. The fact that
    check 1 was later corrected doesn't change when the trade began.
    """
    _trade(con, invented.TEST_TRADE_ID)
    check_id = _checked(con, invented.TEST_TRADE_ID)
    _enter(con, invented.TEST_TRADE_ID)
    # Append a corrected check
    store.append(
        con,
        kind="checked",
        actor="owner",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        corrects=check_id,
        payload={},
    )

    result = integrity.run(con)
    assert result == 0, "A corrected check after entry should be clean"


def test_entry_corrected_after_exit_still_fires(con):
    """Entry correction after an exit must still fire exit_without_entry.

    Regression guard for round 1: checked(1), entered(2), exited(3),
    entered(4, corrects=2). The exit at 3 came after the original entry,
    but the correction orphans it. Must still fire.
    """
    _trade(con, invented.TEST_TRADE_ID)
    _checked(con, invented.TEST_TRADE_ID)
    entry_id = _enter(con, invented.TEST_TRADE_ID, size=100)
    _exit(con, invented.TEST_TRADE_ID, size=10)
    # Correct the entry
    store.append(
        con,
        kind="entered",
        actor="owner",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        corrects=entry_id,
        payload={
            "entry": invented.PRICES["ACME"],
            "stop": 95.0,
            "target": 110.0,
            "size": 80,
            "horizon": "2w",
            "setup": "test-setup",
            "thesis": "test thesis",
        },
    )

    fired = [n for n, f in integrity.INVARIANTS.items() if f(con)]
    assert "exit_without_entry" in fired, "Should still detect orphaned exit"


def test_sql_invariants_exclude_superseded_events(con):
    """SQL invariants must skip events named by a later corrects.

    This is the finding-1 reproduction: checked(1), entered(2, size=100),
    exited(3, size=10), entered(4, size=80, corrects=2). The fold drops the
    exit because it prices against the superseded entry. The invariant must
    report this as exit_without_entry.
    """
    _trade(con, invented.TEST_TRADE_ID)
    _checked(con, invented.TEST_TRADE_ID)
    entry_id = _enter(con, invented.TEST_TRADE_ID, size=100)
    _exit(con, invented.TEST_TRADE_ID, size=10)
    # Correct the entry with a new size using proper append API
    store.append(
        con,
        kind="entered",
        actor="owner",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        corrects=entry_id,
        payload={
            "entry": invented.PRICES["ACME"],
            "stop": 95.0,
            "target": 110.0,
            "size": 80,
            "horizon": "2w",
            "setup": "test-setup",
            "thesis": "test thesis",
        },
    )

    # The fold now sees: checked, entered(size=80), exited(size=10)
    # But the exit came BEFORE the corrected entry, so it has no entry to price against
    fired = [n for n, f in integrity.INVARIANTS.items() if f(con)]
    assert "exit_without_entry" in fired, "Should detect exit orphaned by correction"


def test_trade_with_no_start_catches_trade_with_zero_events(con):
    """A trade with no events at all should be caught by _trade_with_no_start."""
    # Directly insert a trade row without any events
    # (This is what store.add_trade() produces on its own, which is why add_trade_with_event exists)
    con.execute(
        """
        INSERT INTO trades (
            id, account, account_name, run_id, ticker, instrument,
            point_value, currency, side, created_at
        )
        VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?)
        """,
        (
            invented.TEST_TRADE_ID,
            "paper",
            invented.TICKERS[0],
            "stock",
            1.0,
            "USD",
            "long",
            "2026-01-01T00:00:00Z",
        ),
    )
    con.commit()

    # This trade has zero events, which is never legitimate
    failures = integrity._trade_with_no_start(con)
    assert len(failures) == 1
    assert invented.TEST_TRADE_ID in failures[0]
    assert "no checked or imported event" in failures[0]
