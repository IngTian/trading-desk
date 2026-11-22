"""bars.db: completed price bars, insert-only, and the spans each fetch covered.

The only module that writes bars.db. A bar's first write wins; a repeat is ignored. A span
records the range [start, end) a fetch covered, so that inside a span a missing bar is a closed
market, and outside every span the history is missing. Times are stored as UTC ISO-8601 text.

Callers close the connection: `with contextlib.closing(bars.open()) as bcon:`.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from desk import config, judge, store

VERSION = 1   # bars.db's PRAGMA user_version


def _text(t: datetime) -> str:
    if t.tzinfo is None:   # astimezone would guess the host's zone into an insert-only row
        raise ValueError(f"{t!r} has no timezone; bars.db stores UTC")
    return t.astimezone(UTC).isoformat()


def open(path: Path | None = None) -> sqlite3.Connection:
    """Open bars.db, creating it on first use. Probed like desk.db; DELETE journal.

    A new file is built under a per-process name and moved into place, so a process that dies
    half way leaves a `.partial`, never a bars.db at version 0 that the probe would refuse for
    good. Two processes creating it at once: the later replace wins, dropping a few recent bars
    that are fetched again (ruling R5; one machine writes at a time).
    """
    db = path or config.bars_path()
    if not db.exists():
        if config.icloud_placeholder(db).exists():   # evicted: a new file would bury its bars
            raise FileNotFoundError(
                f"bars.db at {db} is in iCloud but not on this Mac; open {db.parent} in Finder to "
                "download it, since it holds bars Yahoo no longer serves.")
        db.parent.mkdir(parents=True, exist_ok=True)   # with only TRADING_DESK_DB set
        partial = db.with_name(f"{db.name}.{os.getpid()}.partial")
        partial.unlink(missing_ok=True)
        con = sqlite3.connect(partial)
        try:
            con.executescript(config.bars_sql().read_text())
            con.execute(f"PRAGMA user_version = {VERSION}")
            con.commit()
        finally:
            con.close()
        os.replace(partial, db)
    store._probe(db, version=VERSION, table="bars",
                 columns=frozenset({"source", "symbol", "timeframe", "open_time"}), name="bars.db")
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = DELETE")
    return con


def add(con: sqlite3.Connection, source: str, symbol: str, minutes: int,
        rows: Sequence[judge.Bar], start: datetime, end: datetime, at: datetime) -> int:
    """Store the bars and the span [start, end) they came from, in one transaction. Returns how
    many bars were new. A bar that fails a CHECK raises, and nothing of the call is stored."""
    with store.transaction(con):
        new = con.executemany(
            "INSERT INTO bars (source, symbol, timeframe, open_time, open, high, low, close, "
            "volume) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            [(source, symbol, minutes, _text(r.open_time), r.open, r.high, r.low, r.close,
              r.volume) for r in rows]).rowcount   # the inserts' changes(): a repeat counts 0
        con.execute("INSERT INTO spans (source, symbol, timeframe, start_time, end_time, "
                    "recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (source, symbol, minutes, _text(start), _text(end), _text(at)))
    return new


def read(con: sqlite3.Connection, source: str, symbol: str, minutes: int, start: datetime,
         end: datetime | None = None) -> list[judge.Bar]:
    """The bars opening in [start, end), oldest first; no end reads to the last one kept."""
    until = None if end is None else _text(end)
    rows = con.execute(
        "SELECT open_time, open, high, low, close, volume FROM bars WHERE source = ? AND "
        "symbol = ? AND timeframe = ? AND open_time >= ? AND (? IS NULL OR open_time < ?) "
        "ORDER BY open_time", (source, symbol, minutes, _text(start), until, until))
    return [judge.Bar(datetime.fromisoformat(r["open_time"]), r["open"], r["high"], r["low"],
                      r["close"], r["volume"]) for r in rows]


def covered(con: sqlite3.Connection, source: str, symbol: str,
            minutes: int) -> list[tuple[datetime, datetime]]:
    """The ranges fetched, oldest first, with touching and overlapping spans merged."""
    merged: list[tuple[datetime, datetime]] = []
    for r in con.execute("SELECT start_time, end_time FROM spans WHERE source = ? AND symbol = ? "
                         "AND timeframe = ? ORDER BY start_time", (source, symbol, minutes)):
        start, end = datetime.fromisoformat(r[0]), datetime.fromisoformat(r[1])
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
