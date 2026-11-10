"""The v1 event store: append-only log, derived state.

Every change to a trade becomes an event. Current state is derived by folding events.
No updates, no deletes. A correction is a new event naming the one it corrects.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from desk import config

# The tuples the CHECKs use, exported so tests and later tasks do not re-declare them.
ACTORS = ("ing", "tracker", "import")
SOURCES = ("chat", "export", "yahoo")
KINDS = (
    "checked",
    "imported",
    "passed",
    "entered",
    "stop_moved",
    "target_moved",
    "exit_proposed",
    "exited",
    "note",
    "closed",
    "labelled",
    "corrected",
    "session_started",
    "session_ended",
    "run_started",
    "run_ended",
)


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
        con.commit()
    finally:
        con.close()


def open(path: Path | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the store. Sets row_factory, PRAGMA foreign_keys = ON, and journal_mode = WAL.

    read_only=True opens with mode=ro. Uses as_uri() because a path containing '?'
    silently discards the mode and opens a new writable file.
    """
    db = path or config.db_path()
    if read_only:
        uri = f"{db.as_uri()}?mode=ro"
        con = sqlite3.connect(uri, uri=True)
    else:
        con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        con.execute("PRAGMA journal_mode = WAL")
    return con


def append(
    con: sqlite3.Connection,
    *,
    kind: str,
    actor: str,
    source: str,
    trade_id: str | None = None,
    session_id: int | None = None,
    corrects: int | None = None,
    payload: dict,
    rules_hash: str | None = None,
    at: str | None = None,
) -> int:
    """Append an event. Returns the new event id."""
    if at is None:
        at = datetime.now(UTC).isoformat()
    cur = con.execute(
        """
        INSERT INTO events (
            at, actor, source, kind, trade_id, session_id, corrects, rules_hash, payload
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (at, actor, source, kind, trade_id, session_id, corrects, rules_hash, json.dumps(payload)),
    )
    con.commit()
    return cur.lastrowid


def events(
    con: sqlite3.Connection,
    *,
    trade_id: str | None = None,
    session_id: int | None = None,
) -> list[sqlite3.Row]:
    """Fetch events, ordered by id. Filters by trade_id or session_id if provided."""
    if trade_id is not None:
        rows = con.execute(
            "SELECT * FROM events WHERE trade_id = ? ORDER BY id",
            (trade_id,),
        ).fetchall()
    elif session_id is not None:
        rows = con.execute(
            "SELECT * FROM events WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    else:
        rows = con.execute("SELECT * FROM events ORDER BY id").fetchall()
    return rows


def add_trade(
    con: sqlite3.Connection,
    *,
    trade_id: str,
    account: str,
    run_id: str | None,
    ticker: str,
    instrument: str,
    point_value: float,
    currency: str,
    side: str,
) -> None:
    """Add a trade. Only what never changes: the mutable state derives from events."""
    created_at = datetime.now(UTC).isoformat()
    con.execute(
        """
        INSERT INTO trades (
            id, account, account_name, run_id, ticker, instrument,
            point_value, currency, side, created_at
        )
        VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
        """,
        (trade_id, account, run_id, ticker, instrument, point_value, currency, side, created_at),
    )
    con.commit()
