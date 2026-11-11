"""Integrity checker for the event log.

Each invariant is a named check that returns failure lines (empty list when healthy).
The store is append-only: rows cannot be repaired in place, only superseded by a
correction event. These checks detect what cannot be fixed that way.

## Why dangling_correction matters

SQLite's foreign key enforcement is per-connection and off by default. The schema
declares `corrects INTEGER REFERENCES events(id)`, but whether that constraint
applies depends on how the file was written, not what the file contains. A database
created with foreign keys off can hold dangling references that would be impossible
to insert with them on. This checker catches those.

## What this does not cover

Dangling foreign keys in `events.trade_id` (no row in `trades`) and `events.session_id`
(no session row) are the same class of corruption — they require foreign keys off — but
are not checked here. A trade_id with no trade makes every fold query fail immediately,
so it is self-reporting rather than silent.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable

from desk import model, store


def _exit_exceeds_open_size(con: sqlite3.Connection) -> list[str]:
    """An exit larger than the open position is an integrity violation.

    Uses the fold (model.state_of) because this spans events: a trade might have
    multiple entries and exits, and only the accumulated state knows whether an
    exit exceeds what's currently held.
    """
    failures = []
    for trade_id in store.trade_ids(con):
        state = model.state_of(con, trade_id)
        if "exit_exceeds_size" in state.taken_against:
            # The fold detected an over-exit but doesn't preserve which specific exit
            # or by how much, so we report that it happened without claiming exact figures
            failures.append(f"trade {trade_id}: an exit exceeded the open position")
    return failures


def _event_before_trade_started(con: sqlite3.Connection) -> list[str]:
    """A trade event must come after the trade's checked or imported event.

    Every trade begins with a checked or imported event. Any other event for that
    trade with a lower event id is ordering-violating.
    """
    failures = []
    rows = con.execute(
        """
        WITH trade_starts AS (
            SELECT trade_id, MIN(id) AS first_start_id
            FROM events
            WHERE kind IN ('checked', 'imported')
              AND id NOT IN (SELECT corrects FROM events WHERE corrects IS NOT NULL)
            GROUP BY trade_id
        )
        SELECT e.trade_id, e.id, e.kind
        FROM events e
        JOIN trade_starts ts ON e.trade_id = ts.trade_id
        WHERE e.id < ts.first_start_id
          AND e.kind NOT IN ('checked', 'imported')
          AND e.id NOT IN (SELECT corrects FROM events WHERE corrects IS NOT NULL)
        ORDER BY e.trade_id, e.id
        """
    ).fetchall()

    for row in rows:
        failures.append(
            f"trade {row['trade_id']}: event {row['id']} ({row['kind']}) "
            f"comes before the trade's checked or imported event"
        )
    return failures


def _dangling_correction(con: sqlite3.Connection) -> list[str]:
    """A correction event must reference an existing event.

    Foreign key `corrects REFERENCES events(id)` is only enforced when PRAGMA
    foreign_keys = ON, which is per-connection and off by default in SQLite.
    A file created with keys off can hold dangling references.
    """
    failures = []
    rows = con.execute(
        """
        SELECT e.id, e.trade_id, e.corrects
        FROM events e
        WHERE e.corrects IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM events WHERE id = e.corrects)
        ORDER BY e.id
        """
    ).fetchall()

    for row in rows:
        failures.append(
            f"event {row['id']} (trade {row['trade_id']}): "
            f"corrects references non-existent event {row['corrects']}"
        )
    return failures


def _closed_trade_holding_size(con: sqlite3.Connection) -> list[str]:
    """A trade marked closed must have zero open size.

    Uses the fold because size_open derives from accumulated entries and exits.
    """
    failures = []
    for trade_id in store.trade_ids(con):
        state = model.state_of(con, trade_id)
        if state.status == "closed" and state.size_open is not None and state.size_open > 0:
            failures.append(
                f"trade {trade_id}: marked closed but still holds {state.size_open} shares"
            )
    return failures


def _trade_with_no_start(con: sqlite3.Connection) -> list[str]:
    """Every trade must have a checked or imported event.

    A trade is created by a checked or imported event. Any trade in the trades
    table with events but no checked/imported is malformed.
    """
    failures = []
    rows = con.execute(
        """
        SELECT t.id
        FROM trades t
        WHERE EXISTS (
            SELECT 1 FROM events
            WHERE trade_id = t.id
              AND id NOT IN (SELECT corrects FROM events WHERE corrects IS NOT NULL)
          )
          AND NOT EXISTS (
              SELECT 1 FROM events
              WHERE trade_id = t.id
                AND kind IN ('checked', 'imported')
                AND id NOT IN (SELECT corrects FROM events WHERE corrects IS NOT NULL)
          )
        ORDER BY t.id
        """
    ).fetchall()

    for row in rows:
        failures.append(
            f"trade {row['id']}: has events but no checked or imported event "
            f"(every trade must start with one of these)"
        )
    return failures


def _exit_without_entry(con: sqlite3.Connection) -> list[str]:
    """An exited event must come after an entered event.

    The fold computes exit results against the entry's adjusted cost base (ACB).
    With no entry, there's no ACB, so every figure derived from that trade is wrong
    rather than missing. Detection: the first entered event id must be lower than
    every exited event id for that trade.
    """
    failures = []
    rows = con.execute(
        """
        WITH first_entries AS (
            SELECT trade_id, MIN(id) AS first_entered_id
            FROM events
            WHERE kind = 'entered'
              AND id NOT IN (SELECT corrects FROM events WHERE corrects IS NOT NULL)
            GROUP BY trade_id
        )
        SELECT e.trade_id, e.id AS exit_id
        FROM events e
        LEFT JOIN first_entries fe ON e.trade_id = fe.trade_id
        WHERE e.kind = 'exited'
          AND e.id NOT IN (SELECT corrects FROM events WHERE corrects IS NOT NULL)
          AND (fe.first_entered_id IS NULL OR e.id < fe.first_entered_id)
        ORDER BY e.trade_id, e.id
        """
    ).fetchall()

    for row in rows:
        failures.append(
            f"trade {row['trade_id']}: exit event {row['exit_id']} "
            f"has no preceding entered event (exit results require entry adjusted cost base)"
        )
    return failures


# Map invariant names to their check functions
INVARIANTS = {
    "exit_exceeds_open_size": _exit_exceeds_open_size,
    "event_before_trade_started": _event_before_trade_started,
    "dangling_correction": _dangling_correction,
    "closed_trade_holding_size": _closed_trade_holding_size,
    "trade_with_no_start": _trade_with_no_start,
    "exit_without_entry": _exit_without_entry,
}


def run(con: sqlite3.Connection, *, emit: Callable[[str], None] | None = None) -> int:
    """Run all integrity checks. Returns 0 if all pass, 1 if any fail.

    emit: optional callable to receive failure lines. Defaults to print.
    """
    if emit is None:
        emit = print

    total_failures = 0

    for name, check_fn in INVARIANTS.items():
        failures = check_fn(con)
        for line in failures:
            emit(f"{name}: {line}")
            total_failures += 1

    if total_failures > 0:
        emit("")
    emit(f"{total_failures} integrity violation(s) found")

    return 1 if total_failures > 0 else 0
