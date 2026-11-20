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
        actor="owner",
        source="chat",
        trade_id=T,
        payload={"entry": 10.0, "stop": 9.0},
    )
    (row,) = store.events(con, trade_id=T)
    assert row["id"] == eid and json.loads(row["payload"])["stop"] == 9.0


def test_the_log_refuses_an_update_and_a_delete(con):
    eid = store.append(
        con, kind="note", actor="owner", source="chat", trade_id=T, payload={"text": "x"}
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
            actor="owner",
            source="chat",
            trade_id=T,
            corrects=9999,
            payload={"why": "typo"},
        )


def test_a_payload_that_is_not_json_is_refused(con):
    # match= because every other column must be valid for this to test the JSON check: with
    # an unknown actor the row was refused by the actor CHECK and the test passed regardless.
    with pytest.raises(sqlite3.IntegrityError, match="json_valid"):
        con.execute(
            "INSERT INTO events (at, actor, source, kind, payload) VALUES (?, ?, ?, ?, ?)",
            ("2026-11-10T10:00:00Z", "owner", "chat", "note", "not-json"),
        )


def test_an_unknown_kind_actor_or_source_is_refused(con):
    # Unknown kind
    with pytest.raises(sqlite3.IntegrityError):
        store.append(
            con,
            kind="unknown_kind",
            actor="owner",
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
            actor="owner",
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
                actor="owner",
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
            actor="owner",
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


def test_cli_is_a_valid_event_source(con):
    """'cli' is in store.SOURCES and accepted by the schema CHECK."""
    assert "cli" in store.SOURCES
    # The schema CHECK must accept it
    eid = store.append(
        con,
        kind="checked",
        actor="owner",
        source="cli",
        trade_id=T,
        payload={"entry": 10.0},
    )
    row = con.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()
    assert row["source"] == "cli"


# ---------------------------------------------------------------------------------------------
# Opening a file is not allowed to change one that is not a desk.db. Pointing the store at the
# frozen legacy book by mistake (db = "hub.db" in config.toml, or TRADING_DESK_DB) used to flip
# it to WAL -- a write -- before crashing with a traceback.


def _md5(path):
    import hashlib
    return hashlib.md5(path.read_bytes()).hexdigest()


def _foreign_book(path, *, journal="delete"):
    """A sqlite file that is not a v1 desk.db: tables of the same names, other columns."""
    con = sqlite3.connect(path)
    con.execute(f"PRAGMA journal_mode = {journal}")
    con.execute("CREATE TABLE trades (id TEXT PRIMARY KEY, symbol TEXT)")
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, note TEXT)")
    con.execute("PRAGMA user_version = 17")
    con.commit()
    con.close()


@pytest.mark.parametrize("read_only", [False, True])
def test_opening_a_file_that_is_not_a_desk_db_refuses_and_changes_nothing(tmp_path, read_only):
    book = tmp_path / "hub.db"
    _foreign_book(book)
    before = _md5(book)

    with pytest.raises(store.NotADeskDatabase) as refused:
        store.open(book, read_only=read_only)

    assert str(book) in str(refused.value)
    assert _md5(book) == before, "the file must be byte-identical"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["hub.db"], "and no -wal or -shm"


def test_a_wal_mode_file_that_is_not_a_desk_db_is_refused_without_a_write(tmp_path):
    book = tmp_path / "hub.db"
    _foreign_book(book, journal="wal")
    before = _md5(book)

    with pytest.raises(store.NotADeskDatabase):
        store.open(book)

    assert _md5(book) == before


def test_a_file_that_is_not_sqlite_at_all_is_refused(tmp_path):
    junk = tmp_path / "desk.db"
    junk.write_text("not a database")
    with pytest.raises(store.NotADeskDatabase):
        store.open(junk)
    assert junk.read_text() == "not a database"


def test_a_new_store_is_stamped_with_its_schema_version(tmp_path):
    db = tmp_path / "desk.db"
    store.create(db)
    assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION


def test_the_store_uses_a_rollback_journal_not_wal(tmp_path):
    """The default folder is under ~/Documents, which iCloud syncs: a db and its -wal synced
    separately is the corruption the docs warn about. And a read leaves no sidecar files."""
    db = tmp_path / "desk.db"
    store.create(db)
    with store.open(db) as con:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    store.open(db, read_only=True).execute("SELECT count(*) FROM events").fetchone()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["desk.db"]


def test_a_paper_run_and_a_replay_run_with_the_same_number_are_two_runs(tmp_path):
    """They shared one row, so a replay trade's run named the Paper rules section."""
    db = tmp_path / "desk.db"
    store.create(db)
    con = store.open(db)
    for kind in ("paper", "replay"):
        assert store.ensure_run(con, run_id="run-3", kind=kind, started="2026-01-01T00:00:00Z",
                                rules_section=f"{kind.capitalize()} — run 3")
    assert not store.ensure_run(con, run_id="run-3", kind="replay",
                                started="2026-01-02T00:00:00Z", rules_section="Replay — run 3")
    rows = con.execute("SELECT kind, id, rules_section FROM runs ORDER BY kind").fetchall()
    assert [tuple(r) for r in rows] == [
        ("paper", "run-3", "Paper — run 3"), ("replay", "run-3", "Replay — run 3")]

    store.add_trade(con, trade_id="t-replay", account="replay", run_id="run-3", ticker="ACME",
                    instrument="stock", point_value=1.0, currency="USD", side="long")
    (section,) = con.execute(
        "SELECT r.rules_section FROM trades t JOIN runs r ON r.kind = t.account AND r.id = t.run_id"
        " WHERE t.id = 't-replay'").fetchone()
    assert section == "Replay — run 3"


def test_a_trade_cannot_name_a_run_of_another_account_kind(tmp_path):
    db = tmp_path / "desk.db"
    store.create(db)
    con = store.open(db)
    store.ensure_run(con, run_id="run-3", kind="paper", started="2026-01-01T00:00:00Z",
                     rules_section="Paper — run 3")
    with pytest.raises(sqlite3.IntegrityError):
        store.add_trade(con, trade_id="t", account="replay", run_id="run-3", ticker="ACME",
                        instrument="stock", point_value=1.0, currency="USD", side="long")
