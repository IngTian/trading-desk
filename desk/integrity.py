"""Integrity checker for the v4 log.

Each invariant is a named check returning failure lines (empty when healthy). The log is
append-only, so a damaged row cannot be repaired in place; these checks find what the API
refuses to write but a file written with foreign keys off, or by hand, can hold.

Shape-of-the-log invariants read the raw rows; the date and report invariants read the fold,
because they are about effective (corrected, withdrawn) decisions.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import fields

from desk import checks, model, rules, store, strategy


def _file(con: sqlite3.Connection) -> list[str]:
    """The file itself, by SQLite's quick_check (integrity_check is the slower, fuller one)."""
    rows: list[str] = []
    try:
        for r in con.execute("PRAGMA quick_check"):
            rows.append(r[0])
    except sqlite3.DatabaseError as e:  # on one file SQLite sometimes raises, sometimes reports
        rows.append(str(e))
    return [f"PRAGMA quick_check: {r}" for r in rows if r != "ok"]


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


def _unfoldable(own: list[sqlite3.Row]) -> bool:
    """True when another invariant already reports why the trade whose events are `own` cannot be
    folded: not one `entered`, a correction naming nothing in the trade, an unreadable event
    time."""
    ids = {e["id"] for e in own}
    return (sum(e["kind"] == "entered" for e in own) != 1
            or any(e["kind"] == "corrected" and e["corrects"] not in ids for e in own)
            or any(_aware(e["at"]) is None for e in own))


def _run_times(con: sqlite3.Connection, run_id: str) -> tuple[list[dt.datetime], ...]:
    """The aware `at`s of the run's run_started events, and of its run_stopped events."""
    run = store.events(con, run_id=run_id)
    return tuple([s for e in run if e["kind"] == kind and (s := _aware(e["at"]))]
                 for kind in ("run_started", "run_stopped"))


def _decision_dates(con: sqlite3.Connection) -> list[str]:
    """By effective time: an entry is not before its run's start, nor (unless the trade is void)
    at or after its run's stop; a move is not before the entry and is strictly before the
    close; a close is strictly after the entry.

    A trade is skipped when it is `_unfoldable`. A fold that fails for any other reason, such as
    a malformed correction payload, is reported here."""
    failures = []
    for t in store.trades(con):
        if _unfoldable(store.events(con, trade_id=t["id"])):
            continue
        who = f"trade {t['id']}"
        try:
            started, stopped = _run_times(con, t["run_id"])
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
                    failures.append(f"{who}: move at {m.at.isoformat()} is not before the close")
            if d.close_at is not None and d.close_at <= d.entry.at:
                failures.append(f"{who}: closed at {d.close_at.isoformat()}, not after the entry")
        except Exception as exc:  # noqa: BLE001 -- integrity must report, never raise
            failures.append(f"{who}: fold failed: {type(exc).__name__}: {exc}")
    return failures


def _reports(con: sqlite3.Connection) -> list[str]:
    """Every non-void trade's reports fit its record, by `model.report_problem`, the api's own
    rules. A trade decision_dates skips or fails to fold is its to report, so it is skipped here;
    a report check that raises on a payload that folds (a price that is not a number) is
    reported, so integrity still never raises."""
    failures = []
    for t in store.trades(con):
        if _unfoldable(store.events(con, trade_id=t["id"])):
            continue
        if store.run(con, t["run_id"]) is None:  # run_currency reports a trade with no run
            continue
        d = None
        try:
            d = model.decisions_of(con, t["id"])
            if d.voided:
                continue
            problem = model.report_problem(
                t["id"], d, side=t["side"], mode=store.run(con, t["run_id"])["mode"],
                stopped=min(_run_times(con, t["run_id"])[1], default=None))
        except Exception as exc:  # noqa: BLE001 -- integrity must report, never raise
            if d is not None:  # a fold that fails is decision_dates' to report
                failures.append(f"trade {t['id']}: report check failed: "
                                f"{type(exc).__name__}: {exc}")
            continue
        if problem:
            failures.append(f"trade {t['id']}: {problem}")
    return failures


def _correction_target(con: sqlite3.Connection) -> list[str]:
    """A `corrected` names an earlier event of the same trade: an `entered`, an `order_moved` or
    a report. A `voided` that names an event withdraws it, so it names an earlier report of the
    same trade."""
    failures = []
    for e in store.events(con):
        if e["kind"] == "corrected":
            what, kinds = "correction", ("entered", "order_moved", *model._REPORTS)
        elif e["kind"] == "voided" and e["corrects"] is not None:
            what, kinds = "withdrawal", model._REPORTS
        else:
            continue
        named = con.execute("SELECT * FROM events WHERE id = ?", (e["corrects"],)).fetchone()
        if e["corrects"] is None or named is None:
            why = f"names event {e['corrects']}, which does not exist"
        elif named["id"] >= e["id"]:
            why = f"names event {named['id']}, which is not earlier"
        elif named["trade_id"] != e["trade_id"]:
            why = f"names event {named['id']}, which belongs to trade {named['trade_id']}"
        elif named["kind"] not in kinds:
            listed = ", ".join(kinds[:-1]) + f" or {kinds[-1]}"
            why = f"names event {named['id']}, a {named['kind']} (only {listed})"
        else:
            continue
        failures.append(f"event {e['id']} (trade {e['trade_id']}): {what} {why}")
    return failures


def _nothing_after_void(con: sqlite3.Connection) -> list[str]:
    """Nothing follows a trade's own void (`corrects` NULL); a withdrawal ends nothing."""
    return [f"trade {r['trade_id']}: event {r['id']} ({r['kind']}) follows the void"
            for r in con.execute(
                "SELECT e.id, e.kind, e.trade_id FROM events e JOIN events v ON v.trade_id ="
                " e.trade_id AND v.kind = 'voided' AND v.corrects IS NULL AND e.id > v.id"
                " ORDER BY e.id")]


def _one_report(con: sqlite3.Connection) -> list[str]:
    """At most one standing (not withdrawn) fill, cancellation and close per trade. Not `held`:
    several open reports may stand."""
    return [f"trade {r['trade_id']}: has {r['n']} standing {r['kind']} events, expected at most 1"
            for r in con.execute(
                "SELECT trade_id, kind, COUNT(*) AS n FROM events e"
                " WHERE kind IN ('filled', 'cancelled', 'closed') AND NOT EXISTS ("
                "SELECT 1 FROM events v WHERE v.kind = 'voided' AND v.corrects = e.id"
                " AND v.trade_id = e.trade_id)"
                " GROUP BY trade_id, kind HAVING n > 1 ORDER BY trade_id, kind")]


def _version_text_hash(con: sqlite3.Connection) -> list[str]:
    return [f"strategy {v['strategy_id']} version {v['version']}: text_hash does not match its text"
            for v in con.execute("SELECT * FROM versions ORDER BY strategy_id, version")
            if v["text_hash"] != hashlib.sha256(strategy.normalise(v["text"]).encode()).hexdigest()]


def _run_currency(con: sqlite3.Connection) -> list[str]:
    """Every non-void trade is in its run's currency, as corrected (a hand-written correction
    can change it). An unfoldable trade is skipped."""
    failures = []
    for t in store.trades(con):
        if _unfoldable(store.events(con, trade_id=t["id"])):
            continue
        try:
            d = model.decisions_of(con, t["id"])
        except Exception:  # noqa: BLE001 -- decision_dates reports a fold that fails
            continue
        run_row = store.run(con, t["run_id"])
        if run_row is None:
            failures.append(f"trade {t['id']}: its run {t['run_id']} does not exist")
            continue
        rc = run_row["currency"]
        if not d.voided and d.currency != rc:
            failures.append(f"trade {t['id']}: in {d.currency}, but its run {t['run_id']} is in "
                            f"{rc}")
    return failures


def _finite_number(v) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and rules._finite(v)


# What each of checks.Result's fields holds, as trade() writes it.
_RESULT = {"name": (lambda v: isinstance(v, str), "text"),
           "ok": (lambda v: v is None or isinstance(v, bool), "true, false or null"),
           "figure": (lambda v: v is None or _finite_number(v), "a finite number or null"),
           "limit": (lambda v: isinstance(v, str), "text"),
           "says": (lambda v: isinstance(v, str), "text"),
           "unit": (lambda v: v is None or isinstance(v, str), "text or null")}


def _shown_problems(p: dict) -> list[str]:
    """What is wrong with an entered payload's `checks`, `market` and `checklist`, each read only
    when present."""
    names = [f.name for f in fields(checks.Result)]
    out = []
    if "checks" in p:
        c = p["checks"]
        if not isinstance(c, list):
            out.append(f"checks {c!r} is not a list of results")
        for i, r in enumerate(c if isinstance(c, list) else ()):
            if not isinstance(r, dict) or set(r) != set(names):
                out.append(f"checks[{i}] {r!r} does not hold exactly {', '.join(names)}")
            else:
                out += [f"checks[{i}] {k} {r[k]!r} is not {what}"
                        for k, (fits, what) in _RESULT.items() if not fits(r[k])]
    if "market" in p:
        m = p["market"]
        if not isinstance(m, dict):
            out.append(f"market {m!r} is not a mapping of market keys to numbers")
        else:
            out += [f"market[{k!r}] {v!r} is not a finite number"
                    for k, v in m.items() if not _finite_number(v)]
    if "checklist" in p:
        c = p["checklist"]
        if not isinstance(c, list):
            out.append(f"checklist {c!r} is not a list of answers")
        out += [f"checklist[{i}] {a!r} is not {{item, verdict pass or fail, reason}}, as text"
                for i, a in enumerate(c if isinstance(c, list) else ()) if not model.is_answer(a)]
    return out


def _entered_checks(con: sqlite3.Connection) -> list[str]:
    """What an entry kept of what it showed: `checks`, a list of results each with exactly
    checks.Result's fields and their types; `market`, finite numbers by key; `checklist`, a list
    of {item, verdict pass or fail, reason}."""
    failures = []
    for e in store.events(con):
        if e["kind"] != "entered":
            continue
        try:
            lines = _shown_problems(json.loads(e["payload"]))
        except Exception as exc:  # noqa: BLE001 -- integrity must report, never raise
            lines = [f"payload could not be read: {type(exc).__name__}: {exc}"]
        failures += [f"event {e['id']} (trade {e['trade_id']}): {ln}" for ln in lines]
    return failures


INVARIANTS: dict[str, Callable[[sqlite3.Connection], list[str]]] = {
    "file": _file,
    "event_times": _event_times,
    "trade_entered_once": _trade_entered_once,
    "run_started_stopped": _run_started_stopped,
    "decision_dates": _decision_dates,
    "reports": _reports,
    "correction_target": _correction_target,
    "nothing_after_void": _nothing_after_void,
    "one_report": _one_report,
    "version_text_hash": _version_text_hash,
    "run_currency": _run_currency,
    "entered_checks": _entered_checks,
}


def run(con: sqlite3.Connection, *, emit: Callable[[str], None] | None = None) -> int:
    """Run every check, emitting each violation (default: print). Returns how many were found."""
    emit = emit or print
    total = 0
    for name, check in INVARIANTS.items():
        try:
            lines = check(con)
        except sqlite3.DatabaseError as e:  # a damaged file, or a check SQLite cannot run
            lines = [f"could not be checked: {type(e).__name__}: {e}"]
        for line in lines:
            emit(f"{name}: {line}")
            total += 1
    if total:
        emit("")
    emit(f"{total} integrity violation(s) found")
    return total
