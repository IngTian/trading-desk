"""Tests for the v1 append-only event store."""
import json
import sqlite3

import invented
import pytest

from desk import store

T = invented.TEST_TRADE_ID


@pytest.fixture
def con(tmp_path):
    """A fresh v1 store in a temp directory."""
    db = tmp_path / "test.db"
    store.create(db)
    with store.open(db) as conn:
        # Add a test trade so events can reference it
        store.add_trade(
            conn,
            trade_id=T,
            account="paper",
            run_id=None,
            ticker="ACME",
            instrument="stock",
            point_value=1.0,
            currency="USD",
            side="long",
        )
        yield conn


def test_an_event_round_trips_with_its_payload(con):
    eid = store.append(
        con,
        kind="checked",
        actor="ing",
        source="chat",
        trade_id=T,
        payload={"entry": 10.0, "stop": 9.0},
    )
    (row,) = store.events(con, trade_id=T)
    assert row["id"] == eid and json.loads(row["payload"])["stop"] == 9.0


def test_the_log_refuses_an_update_and_a_delete(con):
    eid = store.append(
        con, kind="note", actor="ing", source="chat", trade_id=T, payload={"text": "x"}
    )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE events SET payload='{}' WHERE id=?", (eid,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM events WHERE id=?", (eid,))


def test_a_correction_must_name_an_earlier_event(con):
    with pytest.raises(sqlite3.IntegrityError):
        store.append(
            con,
            kind="corrected",
            actor="ing",
            source="chat",
            trade_id=T,
            corrects=9999,
            payload={"why": "typo"},
        )


def test_a_payload_that_is_not_json_is_refused(con):
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO events (at, actor, source, kind, payload) VALUES (?, ?, ?, ?, ?)",
            ("2026-11-10T10:00:00Z", "ing", "chat", "note", "not-json"),
        )


def test_an_unknown_kind_actor_or_source_is_refused(con):
    # Unknown kind
    with pytest.raises(sqlite3.IntegrityError):
        store.append(
            con,
            kind="unknown_kind",
            actor="ing",
            source="chat",
            trade_id=T,
            payload={},
        )
    # Unknown actor
    with pytest.raises(sqlite3.IntegrityError):
        store.append(
            con,
            kind="note",
            actor="unknown_actor",
            source="chat",
            trade_id=T,
            payload={},
        )
    # Unknown source
    with pytest.raises(sqlite3.IntegrityError):
        store.append(
            con,
            kind="note",
            actor="ing",
            source="unknown_source",
            trade_id=T,
            payload={},
        )


def test_read_only_open_cannot_write(tmp_path):
    db = tmp_path / "test.db"
    store.create(db)
    with store.open(db, read_only=True) as conn:
        with pytest.raises(sqlite3.OperationalError):
            store.append(
                conn,
                kind="note",
                actor="ing",
                source="chat",
                payload={"text": "x"},
            )


def test_trade_returns_the_immutable_identity_row(con):
    """store.trade() returns the full trade row, or None if not found."""
    row = store.trade(con, T)
    assert row is not None
    assert row["id"] == T
    assert row["ticker"] == "ACME"
    assert row["side"] == "long"
    assert row["instrument"] == "stock"
    assert row["point_value"] == 1.0
    assert row["currency"] == "USD"

    # Non-existent trade returns None
    assert store.trade(con, "nonexistent") is None


def test_trade_ids_returns_all_ids_ordered(tmp_path):
    """store.trade_ids() returns all trade IDs ordered by created_at."""
    db = tmp_path / "test.db"
    store.create(db)
    with store.open(db) as conn:
        # Add three trades
        store.add_trade(
            conn,
            trade_id="trade-1",
            account="paper",
            run_id=None,
            ticker="ACME",
            instrument="stock",
            point_value=1.0,
            currency="USD",
            side="long",
        )
        store.add_trade(
            conn,
            trade_id="trade-2",
            account="paper",
            run_id=None,
            ticker="WXYZ",
            instrument="stock",
            point_value=1.0,
            currency="USD",
            side="short",
        )
        store.add_trade(
            conn,
            trade_id="trade-3",
            account="real",
            run_id=None,
            ticker="TEST",
            instrument="etf",
            point_value=1.0,
            currency="CAD",
            side="long",
        )

        ids = store.trade_ids(conn)
        assert ids == ["trade-1", "trade-2", "trade-3"]


def test_open_raises_on_missing_database_when_not_read_only(tmp_path):
    """store.open raises FileNotFoundError when the database doesn't exist and read_only=False.
    This prevents creating an empty file that then blocks store.create()."""
    missing = tmp_path / "nonexistent.db"
    with pytest.raises(FileNotFoundError) as exc:
        store.open(missing, read_only=False)
    assert "not been created" in str(exc.value)
    assert str(missing) in str(exc.value)
    # The guard must not leave a stray file
    assert not missing.exists()


def test_add_trade_with_event_is_atomic(tmp_path):
    """If the event insert fails, no trade row is left behind (atomicity)."""
    db = tmp_path / "test.db"
    store.create(db)
    con = store.open(db)

    # Create a payload that will fail json.dumps
    # A Python object that's not JSON-serializable will cause the INSERT to fail
    class NotSerializable:
        pass

    try:
        store.add_trade_with_event(
            con,
            trade_id="test-trade",
            account="paper",
            run_id=None,
            ticker="ACME",
            instrument="stock",
            point_value=1.0,
            currency="USD",
            side="long",
            kind="checked",
            actor="ing",
            source="chat",
            payload={"bad": NotSerializable()},  # This will fail json.dumps
            rules_hash="abc123",
            at="2026-11-10T10:00:00Z",
        )
        raise AssertionError("Expected TypeError from json.dumps")
    except TypeError:
        # Expected: json.dumps cannot serialize the object
        pass

    # Verify no trade row was created (atomicity: both or neither)
    trade_ids = store.trade_ids(con)
    assert trade_ids == [], f"Expected no trades, found {trade_ids}"
