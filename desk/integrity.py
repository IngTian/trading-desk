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


def _run_started_stopped(con: sqlite3.Connection) -> list[str]:
    """Every run has exactly one `run_started` and at most one `run_stopped`, not before it."""
    failures = []
    for r in store.runs(con):
        at = {k: sorted(e["at"] for e in store.events(con, run_id=r["id"]) if e["kind"] == k)
              for k in ("run_started", "run_stopped")}
        if len(at["run_started"]) != 1:
            failures.append(f"run {r['id']}: has {len(at['run_started'])} run_started events, "
                            "expected exactly 1")
        if len(at["run_stopped"]) > 1:
            failures.append(f"run {r['id']}: has {len(at['run_stopped'])} run_stopped events, "
                            "expected at most 1")
        if at["run_started"] and at["run_stopped"] and (
                dt.datetime.fromisoformat(at["run_stopped"][0])
                < dt.datetime.fromisoformat(at["run_started"][0])):
            failures.append(f"run {r['id']}: stopped before it started")
    return failures


def _decision_dates(con: sqlite3.Connection) -> list[str]:
    """By effective time: an entry is not before its run's start; a move is not before the entry
    and is strictly before the hand close (R11); a close is strictly after the entry (R12)."""
    failures = []
    for t in store.trades(con):
        started = [dt.datetime.fromisoformat(e["at"]) for e in store.events(con, run_id=t["run_id"])
                   if e["kind"] == "run_started"]
        try:
            d = model.decisions_of(con, t["id"])
        except (KeyError, IndexError, ValueError, TypeError):
            continue  # no single entry, or a broken correction: other invariants report those
        who = f"trade {t['id']}"
        if started and d.entry.at < min(started):
            failures.append(f"{who}: entered at {d.entry.at.isoformat()}, before its run started")
        for m in d.moves:
            if m.at < d.entry.at:
                failures.append(f"{who}: move at {m.at.isoformat()} is before the entry")
            if d.close_at is not None and m.at >= d.close_at:
                failures.append(f"{who}: move at {m.at.isoformat()} is not before the hand close")
        if d.close_at is not None and d.close_at <= d.entry.at:
            failures.append(f"{who}: closed at {d.close_at.isoformat()}, not after the entry")
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
