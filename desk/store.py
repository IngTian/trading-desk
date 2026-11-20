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
ACTORS = ("owner", "tracker", "import")
SOURCES = ("chat", "cli", "export", "yahoo")
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


# PRAGMA user_version of a v1 desk.db. Stamped at create and required at open, so a file that
# is not one -- the frozen legacy book above all -- is recognised before anything touches it.
SCHEMA_VERSION = 1


class NotADeskDatabase(ValueError):
    """The file exists but is not a v1 desk.db, so the store refuses to open it."""


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
    """Refuse a file that is not a v1 desk.db, without writing to it.

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
    if version != SCHEMA_VERSION or not {"actor", "kind", "payload"} <= columns:
        raise NotADeskDatabase(
            f"{db} is not a v1 desk.db (schema version {version}, expected {SCHEMA_VERSION}); "
            f"it was left untouched. If it is the old book, it is read-only by design: point "
            f"TRADING_DESK_DB or config.toml's `db` at desk.db instead.")


def open(path: Path | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the store. Sets row_factory, PRAGMA foreign_keys = ON, and journal_mode = DELETE.

    The file is probed first (see _probe), so a file that is not a v1 desk.db is refused
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


def _append_no_commit(
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
    at: str,
) -> int:
    """Internal: append an event without committing. Used by append and add_trade_with_event."""
    cur = con.execute(
        """
        INSERT INTO events (
            at, actor, source, kind, trade_id, session_id, corrects, rules_hash, payload
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (at, actor, source, kind, trade_id, session_id, corrects, rules_hash,
         json.dumps(payload, allow_nan=False)),
    )
    if cur.lastrowid is None:
        raise RuntimeError("INSERT succeeded but lastrowid is None")
    return cur.lastrowid


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
    event_id = _append_no_commit(
        con,
        kind=kind,
        actor=actor,
        source=source,
        trade_id=trade_id,
        session_id=session_id,
        corrects=corrects,
        payload=payload,
        rules_hash=rules_hash,
        at=at,
    )
    con.commit()
    return event_id


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


def trade(con: sqlite3.Connection, trade_id: str) -> sqlite3.Row | None:
    """Fetch the immutable identity row for a trade. Returns None if not found."""
    return con.execute(
        "SELECT * FROM trades WHERE id = ?",
        (trade_id,),
    ).fetchone()


def trade_ids(con: sqlite3.Connection) -> list[str]:
    """Fetch all trade IDs, ordered by created_at."""
    rows = con.execute("SELECT id FROM trades ORDER BY created_at").fetchall()
    return [row["id"] for row in rows]


def _add_trade_no_commit(
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
    created_at: str,
) -> None:
    """Internal: add a trade without committing. Used by add_trade and add_trade_with_event."""
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
    _add_trade_no_commit(
        con,
        trade_id=trade_id,
        account=account,
        run_id=run_id,
        ticker=ticker,
        instrument=instrument,
        point_value=point_value,
        currency=currency,
        side=side,
        created_at=created_at,
    )
    con.commit()


def add_trade_with_event(
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
    kind: str,
    actor: str,
    source: str,
    payload: dict,
    rules_hash: str,
    at: str,
) -> int:
    """Add a trade and its first event atomically.

    The trade row and its checked/imported event must be written together because
    integrity._trade_with_no_start requires every trade to have one. Any failure between
    the two writes would commit the trade and lose the event, leaving a permanent integrity
    violation in the append-only log.

    Returns the new event id.
    """
    try:
        _add_trade_no_commit(
            con,
            trade_id=trade_id,
            account=account,
            run_id=run_id,
            ticker=ticker,
            instrument=instrument,
            point_value=point_value,
            currency=currency,
            side=side,
            created_at=at,
        )
        event_id = _append_no_commit(
            con,
            kind=kind,
            actor=actor,
            source=source,
            trade_id=trade_id,
            payload=payload,
            rules_hash=rules_hash,
            at=at,
        )
        con.commit()
        return event_id
    except Exception:
        con.rollback()
        raise


def ensure_run(
    con: sqlite3.Connection,
    *,
    run_id: str,
    kind: str,
    started: str,
    rules_section: str,
) -> bool:
    """Create a run row if it doesn't exist. Returns True if created, False if already exists.

    A run is identified by its kind and id together: paper run 3 and replay run 3 are two.
    """
    existing = con.execute(
        "SELECT id FROM runs WHERE kind = ? AND id = ?", (kind, run_id)).fetchone()
    if existing:
        return False
    con.execute(
        """
        INSERT INTO runs (id, kind, started, rules_section)
        VALUES (?, ?, ?, ?)
        """,
        (run_id, kind, started, rules_section),
    )
    con.commit()
    return True
