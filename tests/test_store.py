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
