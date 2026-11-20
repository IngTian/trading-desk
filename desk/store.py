"""The v2 store: strategies, versions, runs, trades and an append-only log of decisions.

This module is the only one that writes SQL. Current state is derived by folding events.
No updates, no deletes. A correction is a new event naming the one it corrects.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from desk import config

# Exported so tests and later tasks do not re-declare it.
KINDS = ("run_started", "run_stopped", "entered", "order_moved", "closed", "corrected", "voided")


# PRAGMA user_version of a v2 desk.db. Stamped at create and required at open, so a file that
# is not one -- the frozen legacy book above all -- is recognised before anything touches it.
SCHEMA_VERSION = 2


class NotADeskDatabase(ValueError):
    """The file exists but is not a v2 desk.db, so the store refuses to open it."""


def create(path: Path | None = None) -> None:
    """Execute schema.sql into a new file. Refuses to overwrite an existing one."""
    db = path or config.db_path()
    if db.exists():
        raise FileExistsError(f"{db} already exists")
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    try:
        schema = config.schema_sql().read_text()
        con.executescript(schema)
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.commit()
    finally:
        con.close()


def _probe(db: Path) -> None:
    """Refuse a file that is not a v2 desk.db, without writing to it.

    Opened `immutable=1` as well as read-only: a plain read-only open of a WAL-mode file needs
    its -shm, and SQLite may create one. Immutable takes no locks and writes nothing, which is
    the point -- the likeliest wrong file is the frozen legacy book, and opening it for
    writing used to flip it to WAL before the first query failed.
    """
    try:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            columns = {row[1] for row in con.execute("PRAGMA table_info(events)")}
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        raise NotADeskDatabase(f"{db} is not a desk.db: SQLite cannot read it ({e})") from None
    if version != SCHEMA_VERSION or not {"recorded_at", "kind", "payload"} <= columns:
        raise NotADeskDatabase(
            f"{db} is not a v2 desk.db (schema version {version}, expected {SCHEMA_VERSION}); "
            f"it was left untouched. If it is the old book, it is read-only by design: point "
            f"TRADING_DESK_DB or config.toml's `db` at desk.db instead.")


def open(path: Path | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the store. Sets row_factory, PRAGMA foreign_keys = ON, and journal_mode = DELETE.

    The file is probed first (see _probe), so a file that is not a v2 desk.db is refused
    unchanged, read-only or not.

    DELETE, not WAL: the default folder is under ~/Documents, which iCloud syncs, and a
    database synced separately from its -wal file is the corruption a sync folder causes.

    read_only=True opens with mode=ro. Uses as_uri() because a path containing '?'
    silently discards the mode and opens a new writable file.

    Raises FileNotFoundError when the database does not exist, and NotADeskDatabase when it
    is not one.
    """
    db = path or config.db_path()
    if not db.exists():
        # Guard against creating an empty file that blocks store.create()
        raise FileNotFoundError(
            f"{db} does not exist; the book has not been created yet. "
            f"Run `trading-desk check` to create it."
        )
    _probe(db)
    if read_only:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    else:
        con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        con.execute("PRAGMA journal_mode = DELETE")
    return con


@contextmanager
def transaction(con: sqlite3.Connection) -> Iterator[None]:
    """Group several writes: commit on a clean exit, roll all back on any exception."""
    # IMMEDIATE: take the write lock now, so what the caller reads inside is still true at write.
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        con.rollback()
        raise
    con.commit()


def _insert(con: sqlite3.Connection, sql: str, params: tuple) -> int:
    """Run one INSERT. Inside `transaction` it is left for the group to commit; alone it commits
    at once (and rolls back if refused)."""
    alone = not con.in_transaction
    try:
        cur = con.execute(sql, params)
        if alone:
            con.commit()
    except Exception:
        if alone:
            con.rollback()
        raise
    assert cur.lastrowid is not None
    return cur.lastrowid


def add_strategy(con: sqlite3.Connection, id: str, at: str) -> None:
    _insert(con, "INSERT INTO strategies (id, created_at) VALUES (?, ?)", (id, at))


def add_version(con, strategy_id: str, version: int, text: str, text_hash: str,
                rules: dict, timeframes: list, at: str) -> None:
    _insert(con, "INSERT INTO versions (strategy_id, version, text, text_hash, rules, timeframes,"
            " saved_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (strategy_id, version, text, text_hash,
             json.dumps(rules, allow_nan=False), json.dumps(timeframes), at))


def add_tracker(con, strategy_id: str, script: str | None, script_hash: str | None,
                tracker_limit: str | None, at: str) -> None:
    _insert(con, "INSERT INTO trackers (strategy_id, script, script_hash, tracker_limit,"
            " saved_at) VALUES (?, ?, ?, ?, ?)",
            (strategy_id, script, script_hash, tracker_limit, at))


def add_run(con, id: str, strategy_id: str, version: int, mode: str, tags: list) -> None:
    _insert(con, "INSERT INTO runs (id, strategy_id, version, mode, tags) VALUES (?, ?, ?, ?, ?)",
            (id, strategy_id, version, mode, json.dumps(tags)))


def add_trade(con, id: str, run_id: str, ticker: str, side: str, instrument: str,
              point_value: float, currency: str) -> None:
    """Add a trade. Only what never changes: the mutable state derives from events."""
    _insert(con, "INSERT INTO trades (id, run_id, ticker, side, instrument, point_value,"
            " currency) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id, run_id, ticker, side, instrument, point_value, currency))


def append(con, *, kind: str, run_id: str, at: str, payload: dict,
           trade_id: str | None = None, corrects: int | None = None, recorded_at: str) -> int:
    """Append an event. Returns the new event id."""
    return _insert(con, "INSERT INTO events (at, recorded_at, kind, run_id, trade_id, corrects,"
                   " payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (at, recorded_at, kind, run_id, trade_id, corrects,
                    json.dumps(payload, allow_nan=False)))


def versions(con, strategy_id: str) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM versions WHERE strategy_id = ? ORDER BY version",
                       (strategy_id,)).fetchall()


def latest_tracker(con, strategy_id: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM trackers WHERE strategy_id = ? ORDER BY id DESC LIMIT 1",
                       (strategy_id,)).fetchone()


def run(con, id: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM runs WHERE id = ?", (id,)).fetchone()


def runs(con, strategy_id: str | None = None) -> list[sqlite3.Row]:
    """Runs in creation order (rowid), all or one strategy's."""
    where, args = ("WHERE strategy_id = ?", (strategy_id,)) if strategy_id else ("", ())
    return con.execute(f"SELECT * FROM runs {where} ORDER BY rowid", args).fetchall()


def trade(con, id: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM trades WHERE id = ?", (id,)).fetchone()


def trades(con, run_id: str | None = None) -> list[sqlite3.Row]:
    where, args = ("WHERE run_id = ?", (run_id,)) if run_id else ("", ())
    return con.execute(f"SELECT * FROM trades {where} ORDER BY rowid", args).fetchall()


def events(con, *, run_id: str | None = None, trade_id: str | None = None) -> list[sqlite3.Row]:
    """Events ordered by id, narrowed by run and/or trade when given."""
    clauses = [c for c, v in (("run_id = ?", run_id), ("trade_id = ?", trade_id)) if v is not None]
    args = tuple(v for v in (run_id, trade_id) if v is not None)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return con.execute(f"SELECT * FROM events {where} ORDER BY id", args).fetchall()
