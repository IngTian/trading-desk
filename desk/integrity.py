"""Integrity checker for the v2 log.

Each invariant is a named check returning failure lines (empty when healthy). The log is
append-only, so a damaged row cannot be repaired in place; these checks find what the API
refuses to write but a file written with foreign keys off, or by hand, can hold.

Shape-of-the-log invariants read the raw rows; the date invariant reads the fold, because it is
about effective (corrected) times.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from collections.abc import Callable

from desk import model, store, strategy


def _trade_entered_once(con: sqlite3.Connection) -> list[str]:
    """Every trade has exactly one `entered` event (a correction reads in its place, so an
    `entered` is never superseded)."""
    return [f"trade {t['id']}: has {n} entered events, expected exactly 1"
            for t in store.trades(con)
            if (n := con.execute("SELECT COUNT(*) FROM events WHERE trade_id = ? AND kind ="
                                 " 'entered'", (t["id"],)).fetchone()[0]) != 1]


def _aware(text) -> dt.datetime | None:
    """`text` as an aware time, or None when it is not one (event_times reports those)."""
    try:
        t = dt.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo is not None else None


def _event_times(con: sqlite3.Connection) -> list[str]:
    """Every event's `at` and `recorded_at` is an aware ISO-8601 time."""
    return [f"event {e['id']}: {col} {e[col]!r} is not an aware ISO-8601 time"
            for e in store.events(con) for col in ("at", "recorded_at") if _aware(e[col]) is None]


def _run_started_stopped(con: sqlite3.Connection) -> list[str]:
    """Every run has exactly one `run_started` and at most one `run_stopped`, not before it."""
    failures = []
    for r in store.runs(con):
        evs = store.events(con, run_id=r["id"])
        at = {k: [e["at"] for e in evs if e["kind"] == k] for k in ("run_started", "run_stopped")}
        if len(at["run_started"]) != 1:
            failures.append(f"run {r['id']}: has {len(at['run_started'])} run_started events, "
                            "expected exactly 1")
        if len(at["run_stopped"]) > 1:
            failures.append(f"run {r['id']}: has {len(at['run_stopped'])} run_stopped events, "
                            "expected at most 1")
        start = [t for t in map(_aware, at["run_started"]) if t]
        stop = [t for t in map(_aware, at["run_stopped"]) if t]
        if start and stop and min(stop) < min(start):
            failures.append(f"run {r['id']}: stopped before it started")
    return failures


def _decision_dates(con: sqlite3.Connection) -> list[str]:
    """By effective time: an entry is not before its run's start, nor (unless the trade is void)
    at or after its run's stop; a move is not before the entry and is strictly before the hand
    close; a close is strictly after the entry.

    A trade is skipped when another invariant already reports why it cannot be folded (not one
    `entered`, a correction naming nothing in the trade, an unreadable event time). A fold that
    fails for any other reason, such as a malformed correction payload, is reported here."""
    failures = []
    for t in store.trades(con):
        own = store.events(con, trade_id=t["id"])
        ids = {e["id"] for e in own}
        if (sum(e["kind"] == "entered" for e in own) != 1
                or any(e["kind"] == "corrected" and e["corrects"] not in ids for e in own)
                or any(_aware(e["at"]) is None for e in own)):
            continue
        who = f"trade {t['id']}"
        try:
            run = store.events(con, run_id=t["run_id"])
            started, stopped = ([s for e in run if e["kind"] == kind and (s := _aware(e["at"]))]
                                for kind in ("run_started", "run_stopped"))
            d = model.decisions_of(con, t["id"])
            if started and d.entry.at < min(started):
                failures.append(f"{who}: entered at {d.entry.at.isoformat()}, before its run "
                                "started")
            if stopped and not d.voided and d.entry.at >= min(stopped):
                failures.append(f"{who}: entered at {d.entry.at.isoformat()}, at or after its "
                                f"run stopped at {min(stopped).isoformat()}")
            for m in d.moves:
                if m.at < d.entry.at:
                    failures.append(f"{who}: move at {m.at.isoformat()} is before the entry")
                if d.close_at is not None and m.at >= d.close_at:
                    failures.append(f"{who}: move at {m.at.isoformat()} is not before the hand "
                                    "close")
            if d.close_at is not None and d.close_at <= d.entry.at:
                failures.append(f"{who}: closed at {d.close_at.isoformat()}, not after the entry")
        except Exception as exc:  # noqa: BLE001 -- integrity must report, never raise
            failures.append(f"{who}: fold failed: {type(exc).__name__}: {exc}")
    return failures


def _correction_target(con: sqlite3.Connection) -> list[str]:
    """A `corrected` names an earlier event of the same trade, an `entered` or `order_moved`."""
    failures = []
    for e in store.events(con):
        if e["kind"] != "corrected":
            continue
        named = con.execute("SELECT * FROM events WHERE id = ?", (e["corrects"],)).fetchone()
        if e["corrects"] is None or named is None:
            why = f"names event {e['corrects']}, which does not exist"
        elif named["id"] >= e["id"]:
            why = f"names event {named['id']}, which is not earlier"
        elif named["trade_id"] != e["trade_id"]:
            why = f"names event {named['id']}, which belongs to trade {named['trade_id']}"
        elif named["kind"] not in ("entered", "order_moved"):
            why = f"names event {named['id']}, a {named['kind']} (only entered or order_moved)"
        else:
            continue
        failures.append(f"event {e['id']} (trade {e['trade_id']}): correction {why}")
    return failures


def _nothing_after_void(con: sqlite3.Connection) -> list[str]:
    return [f"trade {r['trade_id']}: event {r['id']} ({r['kind']}) follows the void"
            for r in con.execute(
                "SELECT e.id, e.kind, e.trade_id FROM events e JOIN events v ON v.trade_id ="
                " e.trade_id AND v.kind = 'voided' AND e.id > v.id ORDER BY e.id")]


def _one_close(con: sqlite3.Connection) -> list[str]:
    return [f"trade {r['trade_id']}: has {r['n']} closed events, expected at most 1"
            for r in con.execute("SELECT trade_id, COUNT(*) AS n FROM events WHERE kind ="
                                 " 'closed' GROUP BY trade_id HAVING n > 1")]


def _version_text_hash(con: sqlite3.Connection) -> list[str]:
    return [f"strategy {v['strategy_id']} version {v['version']}: text_hash does not match its text"
            for v in con.execute("SELECT * FROM versions ORDER BY strategy_id, version")
            if v["text_hash"] != hashlib.sha256(strategy.normalise(v["text"]).encode()).hexdigest()]


INVARIANTS: dict[str, Callable[[sqlite3.Connection], list[str]]] = {
    "event_times": _event_times,
    "trade_entered_once": _trade_entered_once,
    "run_started_stopped": _run_started_stopped,
    "decision_dates": _decision_dates,
    "correction_target": _correction_target,
    "nothing_after_void": _nothing_after_void,
    "one_close": _one_close,
    "version_text_hash": _version_text_hash,
}


def run(con: sqlite3.Connection, *, emit: Callable[[str], None] | None = None) -> int:
    """Run every check, emitting each violation (default: print). Returns how many were found."""
    emit = emit or print
    total = 0
    for name, check in INVARIANTS.items():
        for line in check(con):
            emit(f"{name}: {line}")
            total += 1
    if total:
        emit("")
    emit(f"{total} integrity violation(s) found")
    return total
