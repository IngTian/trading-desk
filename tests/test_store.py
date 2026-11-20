"""Tests for the v2 store: strategies, versions, runs, trades and an append-only log."""
import json
import sqlite3

import pytest

from desk import store

AT = "2026-11-10T10:00:00+00:00"


@pytest.fixture
def con(tmp_path):
    """A fresh v2 store with one strategy, one version, one run and one trade."""
    db = tmp_path / "test.db"
    store.create(db)
    with store.open(db) as conn:
        store.add_strategy(conn, "alpha", AT)
        store.add_version(conn, "alpha", 1, "text", "h1", {"size": 1}, ["15m"], AT)
        store.add_run(conn, "alpha-r1", "alpha", 1, "live", ["paper"])
        store.add_trade(conn, "ACME-20261110", "alpha-r1", "ACME", "long", "stock", 1.0, "USD")
        yield conn


def _append(con, kind="entered", trade_id="ACME-20261110", **kw):
    return store.append(con, kind=kind, run_id="alpha-r1", at=AT, payload=kw.pop("payload", {}),
                        trade_id=trade_id, recorded_at=AT, **kw)


def test_an_event_round_trips_with_its_payload(con):
    eid = _append(con, payload={"entry": 10.0, "stop": 9.0})
    (row,) = store.events(con, trade_id="ACME-20261110")
    assert row["id"] == eid and json.loads(row["payload"])["stop"] == 9.0
    assert store.events(con, run_id="alpha-r1") == [row]
    assert store.events(con, run_id="nope") == []


def test_the_log_refuses_an_update_and_a_delete(con):
    eid = _append(con)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE events SET payload='{}' WHERE id=?", (eid,))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM events WHERE id=?", (eid,))


def test_versions_and_trackers_refuse_an_update_and_a_delete(con):
    store.add_tracker(con, "alpha", "script", "sh", "40 bars after exit", AT)
    for sql in ("UPDATE versions SET text='x'", "DELETE FROM versions",
                "UPDATE trackers SET script='x'", "DELETE FROM trackers"):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(sql)


def test_a_correction_must_name_an_earlier_event(con):
    with pytest.raises(sqlite3.IntegrityError):
        _append(con, kind="corrected", corrects=9999)


def test_a_trade_event_needs_a_trade_and_a_run_event_must_not_have_one(con):
    with pytest.raises(sqlite3.IntegrityError):
        _append(con, kind="entered", trade_id=None)
    with pytest.raises(sqlite3.IntegrityError):
        _append(con, kind="run_started")  # carries a trade_id
    assert _append(con, kind="run_started", trade_id=None)


def test_a_payload_that_is_not_json_is_refused(con):
    with pytest.raises(sqlite3.IntegrityError, match="json_valid"):
        con.execute(
            "INSERT INTO events (at, recorded_at, kind, run_id, trade_id, payload)"
            " VALUES (?, ?, 'entered', 'alpha-r1', 'ACME-20261110', 'not-json')", (AT, AT))


def test_an_unknown_kind_is_refused(con):
    with pytest.raises(sqlite3.IntegrityError):
        _append(con, kind="unknown_kind")
    assert store.KINDS == ("run_started", "run_stopped", "entered", "order_moved",
                           "closed", "corrected", "voided")


def test_a_run_naming_a_missing_version_is_refused(con):
    with pytest.raises(sqlite3.IntegrityError):
        store.add_run(con, "alpha-r2", "alpha", 7, "live", [])


def test_versions_trackers_and_runs_read_back(con):
    store.add_version(con, "alpha", 2, "text2", "h2", {}, ["1h"], AT)
    assert [v["version"] for v in store.versions(con, "alpha")] == [1, 2]
    assert store.latest_tracker(con, "alpha") is None
    store.add_tracker(con, "alpha", "a", "ha", None, AT)
    store.add_tracker(con, "alpha", None, None, None, AT)
    assert store.latest_tracker(con, "alpha")["script"] is None
    assert store.run(con, "alpha-r1")["mode"] == "live" and store.run(con, "x") is None
    assert [r["id"] for r in store.runs(con)] == ["alpha-r1"]
    assert store.runs(con, "beta") == []
    assert json.loads(store.run(con, "alpha-r1")["tags"]) == ["paper"]
    assert store.trade(con, "ACME-20261110")["ticker"] == "ACME"
    assert store.trade(con, "nonexistent") is None
    assert [t["id"] for t in store.trades(con, "alpha-r1")] == ["ACME-20261110"]
    assert store.trades(con, "other") == [] and len(store.trades(con)) == 1


def test_a_refused_second_write_rolls_back_the_group(tmp_path):
    db = tmp_path / "t.db"
    store.create(db)
    con = store.open(db)
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction(con):
            store.add_strategy(con, "beta", AT)
            store.add_version(con, "beta", 0, "t", "h", {}, [], AT)  # version >= 1
    assert con.execute("SELECT count(*) FROM strategies").fetchone()[0] == 0


def test_a_successful_group_commits_both_rows(tmp_path):
    db = tmp_path / "t.db"
    store.create(db)
    con = store.open(db)
    with store.transaction(con):
        store.add_strategy(con, "beta", AT)
        store.add_version(con, "beta", 1, "t", "h", {}, [], AT)
    other = store.open(db, read_only=True)
    assert len(store.versions(other, "beta")) == 1


def test_a_failed_commit_leaves_the_connection_usable(tmp_path):
    """Review item 7: a COMMIT refused because a reader held the file left the connection inside
    its transaction, holding the write lock, so every later write failed with "cannot start a
    transaction within a transaction" until the process restarted."""
    db = tmp_path / "t.db"
    store.create(db)
    con = store.open(db)
    con.execute("PRAGMA busy_timeout = 50")
    reader = sqlite3.connect(db, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM strategies").fetchone()  # SHARED, held until COMMIT
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        with store.transaction(con):
            store.add_strategy(con, "beta", AT)
    assert not con.in_transaction
    reader.execute("COMMIT")
    with store.transaction(con):
        store.add_strategy(con, "gamma", AT)
    assert [r["id"] for r in con.execute("SELECT id FROM strategies")] == ["gamma"]
    reader.close()


def test_a_lone_append_is_durable(con, tmp_path):
    _append(con)
    other = store.open(tmp_path / "test.db", read_only=True)
    assert len(store.events(other)) == 1


def test_read_only_open_cannot_write(tmp_path):
    db = tmp_path / "test.db"
    store.create(db)
    with store.open(db, read_only=True) as conn:
        with pytest.raises(sqlite3.OperationalError):
            store.add_strategy(conn, "alpha", AT)


def test_a_v1_file_is_refused_as_the_wrong_schema_version(tmp_path):
    old = tmp_path / "desk.db"
    c = sqlite3.connect(old)
    c.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, actor TEXT, kind TEXT, payload TEXT)")
    c.execute("PRAGMA user_version = 1")
    c.commit()
    c.close()
    before = _md5(old)
    with pytest.raises(store.NotADeskDatabase, match="schema version 1, expected 2"):
        store.open(old)
    assert _md5(old) == before


def test_open_raises_on_missing_database_when_not_read_only(tmp_path):
    missing = tmp_path / "nonexistent.db"
    with pytest.raises(FileNotFoundError) as exc:
        store.open(missing, read_only=False)
    assert "not been created" in str(exc.value)
    assert not missing.exists()


# ---------------------------------------------------------------------------------------------
# Opening a file is not allowed to change one that is not a desk.db. Pointing the store at the
# frozen legacy book by mistake (db = "hub.db" in config.toml, or TRADING_DESK_DB) used to flip
# it to WAL -- a write -- before crashing with a traceback.


def _md5(path):
    import hashlib
    return hashlib.md5(path.read_bytes()).hexdigest()


def _foreign_book(path, *, journal="delete"):
    """A sqlite file that is not a desk.db: tables of the same names, other columns."""
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
