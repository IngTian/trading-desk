"""Tests for the v3 integrity checker. Violations are written as raw SQL with foreign keys OFF,
the way a damaged or hand-edited file would hold them; the healthy log goes through the API.
Every figure is invented."""
import json
from datetime import UTC, datetime, timedelta

import pytest

from desk import api, integrity, store

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
T1, T2, T3, T4 = (NOW + timedelta(hours=h) for h in (1, 2, 3, 4))
LATER = NOW + timedelta(days=1)
BASE = dict(ticker="ACME", side="long", entry_type="limit", entry=100.0, stop=98.5, target=103.0,
            size=10, currency="USD", reasons="Dip into support.")
TEXT = "---\nname: Dip buyer\n---\n\n# Dip buyer\n\n```rules\ntimeframes = [\"1h\"]\n```\n"
RUN = "dip-buyer-r1"


@pytest.fixture
def con(tmp_path):
    """A healthy log built through the API: one replay run, a trade with a move, a correction
    and a hand close, and a second trade that is voided."""
    db = tmp_path / "desk.db"
    store.create(db)
    with store.open(db) as conn:
        api.save_strategy(conn, "dip-buyer", TEXT, now=NOW)
        api.start_run(conn, "dip-buyer", mode="replay", currency="USD", at=NOW, now=NOW)
        t = api.trade(conn, RUN, **BASE, at=T1, now=LATER)
        api.move_order(conn, t.id, stop=99.0, reason="Trail.", at=T2, now=LATER)
        api.correct_trade(conn, t.id, fields={"stop": 98.0}, reason="Typo.", now=LATER)
        api.close_trade(conn, t.id, reason="Out.", at=T3, now=LATER)
        v = api.trade(conn, RUN, **{**BASE, "ticker": "WXYZ"}, at=T1, now=LATER)
        api.void_trade(conn, v.id, reason="Wrong run.", now=LATER)
        conn.execute("PRAGMA foreign_keys = OFF")
        yield conn


def ev(con, kind, at, *, trade=None, corrects=None, payload=None, run=RUN):
    """One raw event row, as a damaged file could hold it."""
    con.execute("PRAGMA ignore_check_constraints = ON")  # the corrects < id CHECK, as a file
    cur = con.execute(                                    # written without it would lack it
        "INSERT INTO events (at, recorded_at, kind, run_id, trade_id, corrects, payload)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (at if isinstance(at, str) else at.isoformat(), LATER.isoformat(), kind, run, trade,
         corrects, json.dumps(payload or {})))
    con.execute("PRAGMA ignore_check_constraints = OFF")
    con.commit()
    return cur.lastrowid


def tid(con, ticker):
    """The id the API gave the ticker's trade (it carries the local date)."""
    return con.execute("SELECT id FROM trades WHERE ticker = ?", (ticker,)).fetchone()[0]


def raw_trade(con, id, *, at=T1, run=RUN, entered=True):
    con.execute("INSERT INTO trades (id, run_id, ticker, side, instrument, point_value, currency)"
                " VALUES (?, ?, 'ACME', 'long', 'stock', 1.0, 'USD')", (id, run))
    if entered:
        ev(con, "entered", at, trade=id, run=run, payload={
            "entry_type": "limit", "entry": 100.0, "stop": 98.5, "target": 103.0, "size": 10,
            "reasons": "x", "screenshots": []})


def found(con):
    lines: list[str] = []
    count = integrity.run(con, emit=lines.append)
    return count, [ln for ln in lines if ln.count(":") and not ln.endswith("found")]


def only(con, name):
    """The one invariant that fires, and how many times."""
    count, lines = found(con)
    assert count > 0 and all(ln.startswith(f"{name}: ") for ln in lines), lines
    assert count == len(lines)
    return count


def test_a_healthy_log_reports_nothing(con):
    assert found(con) == (0, [])


def test_the_count_is_returned_and_printed(con, capsys):
    ev(con, "closed", T4, trade=tid(con, "ACME"))  # a second close
    count = integrity.run(con)
    assert count == 1
    assert "1 integrity violation(s) found" in capsys.readouterr().out


# --- every trade has exactly one `entered` -----------------------------------------------------

def test_a_trade_with_no_entered_is_reported(con):
    raw_trade(con, "NOEN-1", entered=False)
    assert only(con, "trade_entered_once") == 1


def test_a_trade_with_two_entered_is_reported(con):
    ev(con, "entered", T4, trade=tid(con, "ACME"), payload={})
    assert only(con, "trade_entered_once") == 1


# --- runs --------------------------------------------------------------------------------------

def test_a_run_with_two_starts_is_reported(con):
    ev(con, "run_started", T4)
    assert only(con, "run_started_stopped") == 1


def test_a_run_with_no_start_is_reported(con):
    con.execute("INSERT INTO runs (id, strategy_id, version, mode, tags, currency)"
                " VALUES ('dip-buyer-r2', 'dip-buyer', 1, 'live', '[]', 'USD')")
    assert only(con, "run_started_stopped") == 1


def test_a_run_with_two_stops_is_reported(con):
    ev(con, "run_stopped", T4)
    ev(con, "run_stopped", T4 + timedelta(hours=1))
    assert only(con, "run_started_stopped") == 1


def test_a_stop_before_the_start_is_reported_and_one_second_after_is_not(con):
    # A run with no trades, so no entry falls after the stop (decision_dates' half).
    con.execute("INSERT INTO runs (id, strategy_id, version, mode, tags, currency)"
                " VALUES ('dip-buyer-r2', 'dip-buyer', 1, 'replay', '[]', 'USD')")
    ev(con, "run_started", NOW, run="dip-buyer-r2")
    ev(con, "run_stopped", NOW + timedelta(seconds=1), run="dip-buyer-r2")
    assert found(con)[0] == 0
    ev(con, "run_stopped", NOW - timedelta(seconds=1), run="dip-buyer-r2")  # a second, too early
    count, lines = found(con)
    assert any("before" in ln for ln in lines)


# --- no decision before its entry or its run's start ------------------------------------------

def test_a_move_before_the_entry_is_reported(con):
    ev(con, "order_moved", T1 - timedelta(seconds=1), trade=tid(con, "ACME"),
       payload={"stop": 99.0, "reason": "x"})
    assert only(con, "decision_dates") == 1


def test_a_move_at_the_entry_is_healthy(con):
    ev(con, "order_moved", T1, trade=tid(con, "ACME"), payload={"stop": 99.0, "reason": "x"})
    assert found(con)[0] == 0


def test_a_close_before_the_entry_is_reported(con):
    raw_trade(con, "CLOS-1", at=T2)
    ev(con, "closed", T2 - timedelta(seconds=1), trade="CLOS-1", payload={"reason": "x"})
    assert only(con, "decision_dates") == 1


def test_a_close_at_the_entry_is_reported_and_one_second_later_is_not(con):
    raw_trade(con, "CLOS-1", at=T2)
    ev(con, "closed", T2 + timedelta(seconds=1), trade="CLOS-1", payload={"reason": "x"})
    assert found(con)[0] == 0
    raw_trade(con, "CLOS-2", at=T2)
    ev(con, "closed", T2, trade="CLOS-2", payload={"reason": "x"})
    assert only(con, "decision_dates") == 1


def test_a_move_at_the_hand_close_is_reported_and_one_second_before_is_not(con):
    raw_trade(con, "MOVE-1", at=T1)
    ev(con, "closed", T3, trade="MOVE-1", payload={"reason": "x"})
    ev(con, "order_moved", T3 - timedelta(seconds=1), trade="MOVE-1",
       payload={"stop": 99.0, "reason": "x"})
    assert found(con)[0] == 0
    ev(con, "order_moved", T3, trade="MOVE-1", payload={"stop": 99.0, "reason": "x"})
    assert only(con, "decision_dates") == 1


@pytest.mark.parametrize(("stop", "reported"), [(T1, 1), (T1 + timedelta(seconds=1), 0)])
def test_a_trade_entered_at_or_after_its_runs_stop_is_reported_unless_void(con, stop, reported):
    """Review item 3: stop_run accepted a stop dated before a trade already on the run. ACME and
    the void WXYZ are both entered at T1; only ACME counts."""
    ev(con, "run_stopped", stop)
    if reported:
        assert only(con, "decision_dates") == reported
    else:
        assert found(con) == (0, [])


def test_an_entry_before_the_run_started_is_reported(con):
    raw_trade(con, "EARL-1", at=NOW - timedelta(seconds=1))
    assert only(con, "decision_dates") == 1


def test_a_decision_is_dated_by_its_corrected_time(con):
    """A correction moves the entry earlier than the run's start: judged by effective times."""
    entered = store.events(con, trade_id=tid(con, "ACME"))[0]["id"]
    ev(con, "corrected", LATER, trade=tid(con, "ACME"), corrects=entered,
       payload={"fields": {"at": (NOW - timedelta(hours=1)).isoformat()}, "reason": "x"})
    assert only(con, "decision_dates") == 1


# --- corrections -------------------------------------------------------------------------------

def test_a_correction_naming_a_missing_event_is_reported(con):
    ev(con, "corrected", LATER, trade=tid(con, "ACME"), corrects=9999,
       payload={"fields": {"stop": 97.0}, "reason": "x"})
    assert only(con, "correction_target") == 1


def test_a_correction_naming_another_trades_event_is_reported(con):
    other = [e["id"] for e in store.events(con, trade_id=tid(con, "WXYZ"))][0]
    ev(con, "corrected", LATER, trade=tid(con, "ACME"), corrects=other,
       payload={"fields": {"stop": 97.0}, "reason": "x"})
    assert only(con, "correction_target") == 1


@pytest.mark.parametrize("kind", ["corrected"])
def test_a_correction_naming_a_correction_is_reported(con, kind):
    named = [e["id"] for e in store.events(con, trade_id=tid(con, "ACME")) if e["kind"] == kind]
    ev(con, "corrected", LATER, trade=tid(con, "ACME"), corrects=named[0],
       payload={"fields": {"stop": 97.0}, "reason": "x"})
    assert only(con, "correction_target") == 1


def test_a_withdrawal_naming_an_entry_is_reported(con):
    entered = store.events(con, trade_id=tid(con, "ACME"))[0]["id"]
    ev(con, "voided", LATER, trade=tid(con, "ACME"), corrects=entered, payload={"reason": "x"})
    assert only(con, "correction_target") == 1


def test_a_correction_naming_a_later_event_is_reported(con):
    """The schema's CHECK forbids corrects >= id on insert; a file written without it can."""
    last = store.events(con)[-1]["id"]
    ev(con, "corrected", LATER, trade=tid(con, "ACME"), corrects=last + 5,
       payload={"fields": {"stop": 97.0}, "reason": "x"})
    assert only(con, "correction_target") == 1


# --- void and close are final ------------------------------------------------------------------

def test_an_event_after_a_void_is_reported(con):
    ev(con, "order_moved", T4, trade=tid(con, "WXYZ"), payload={"stop": 99.0, "reason": "x"})
    assert only(con, "nothing_after_void") == 1


def test_two_closes_are_reported(con):
    ev(con, "closed", T4, trade=tid(con, "ACME"), payload={"reason": "again"})
    assert only(con, "one_report") == 1


def test_two_standing_fills_are_reported(con):
    raw_trade(con, "RAW-1")
    for _ in range(2):
        ev(con, "filled", T2, trade="RAW-1", payload={"reason": "x"})
    assert only(con, "one_report") == 1


# --- reports -----------------------------------------------------------------------------------

def test_a_log_with_reports_a_withdrawal_and_a_corrected_report_reports_nothing(con):
    raw_trade(con, "RAW-1")
    first = ev(con, "filled", T2, trade="RAW-1", payload={"reason": "x"})
    ev(con, "voided", LATER, trade="RAW-1", corrects=first, payload={"reason": "x"})
    second = ev(con, "filled", T2, trade="RAW-1", payload={"reason": "x", "price": 99.5})
    ev(con, "corrected", LATER, trade="RAW-1", corrects=second,
       payload={"fields": {"price": 99.75}, "reason": "x"})
    # A move after a withdrawal: not after a void. Then still open at T3.
    ev(con, "order_moved", T3, trade="RAW-1", payload={"stop": 99.0, "reason": "x"})
    ev(con, "held", T3, trade="RAW-1", payload={"reason": "x"})
    ev(con, "closed", T4, trade="RAW-1", payload={"reason": "x", "kind": "stop"})
    assert found(con) == (0, [])


def test_an_exit_with_no_position_is_reported(con):
    raw_trade(con, "RAW-1")
    ev(con, "closed", T3, trade="RAW-1", payload={"reason": "x", "kind": "stop"})
    assert only(con, "reports") == 1


def test_an_exit_kind_that_is_not_known_is_reported(con):
    raw_trade(con, "RAW-1")
    ev(con, "closed", T3, trade="RAW-1", payload={"reason": "x", "kind": "trailing"})
    assert only(con, "reports") == 1


def test_an_open_report_at_or_after_the_close_is_reported(con):
    raw_trade(con, "RAW-1")
    ev(con, "filled", T2, trade="RAW-1", payload={"reason": "x"})
    ev(con, "closed", T3, trade="RAW-1", payload={"reason": "x", "kind": "stop"})
    ev(con, "held", T4, trade="RAW-1", payload={"reason": "x"})
    assert only(con, "reports") == 1


# --- versions ----------------------------------------------------------------------------------

def test_a_version_whose_hash_does_not_match_its_text_is_reported(con):
    con.execute("INSERT INTO versions (strategy_id, version, text, text_hash, rules, timeframes,"
                " checklist, saved_at) VALUES ('dip-buyer', 2, ?, 'bad', '{}', '[\"1h\"]',"
                " '[]', ?)",
                (TEXT, NOW.isoformat()))
    assert only(con, "version_text_hash") == 1


# --- the checker reports a damaged log, it does not raise on it --------------------------------

def test_a_garbage_at_is_reported(con):
    ev(con, "order_moved", "tomorrow-ish", trade=tid(con, "ACME"),
       payload={"stop": 99.0, "reason": "x"})
    count, lines = found(con)
    assert count >= 1 and any(ln.startswith("event_times: ") and "tomorrow-ish" in ln
                              for ln in lines)


def test_a_naive_at_is_reported(con):
    ev(con, "order_moved", "2026-03-02T16:00:00", trade=tid(con, "ACME"),
       payload={"stop": 99.0, "reason": "x"})
    assert only(con, "event_times") == 1


def test_a_correction_with_a_malformed_payload_is_a_fold_failure(con):
    entered = store.events(con, trade_id=tid(con, "ACME"))[0]["id"]
    ev(con, "corrected", LATER, trade=tid(con, "ACME"), corrects=entered,
       payload={"fields": "not a dict", "reason": "x"})
    count, lines = found(con)
    assert count == 1 and "fold failed" in lines[0] and tid(con, "ACME") in lines[0]


def test_the_checker_never_raises_on_a_damaged_log(con):
    ev(con, "order_moved", "garbage", trade=tid(con, "ACME"),
       payload={"stop": 99.0, "reason": "x"})
    ev(con, "run_stopped", "2026-03-02T16:00:00")  # a naive stop next to the aware start
    raw_trade(con, "RAW-1")
    entered = store.events(con, trade_id="RAW-1")[0]["id"]
    ev(con, "corrected", LATER, trade="RAW-1", corrects=entered,
       payload={"fields": "not a dict", "reason": "x"})
    raw_trade(con, "RAW-2")  # its report folds, but its price breaks the report check
    ev(con, "filled", T2, trade="RAW-2", payload={"reason": "x", "price": "abc"})
    lines: list[str] = []
    assert integrity.run(con, emit=lines.append) >= 4
    assert any("trade RAW-2: report check failed: TypeError" in ln for ln in lines)


ENTERED = {"entry_type": "limit", "entry": 100.0, "stop": 98.5, "target": 103.0, "size": 10,
           "reasons": "x", "screenshots": []}


@pytest.mark.parametrize(("key", "value", "says"), [
    ("checks", [{"name": 1}], "checks[0]"),
    ("checks", 5, "checks 5"),
    ("market", {"x": "nan"}, "market['x']"),
    ("checklist", [{"item": "x", "verdict": "maybe", "reason": "y"}], "checklist[0]"),
])
def test_entered_checks_reports_a_malformed_record_and_never_raises(con, key, value, says):
    assert found(con)[0] == 0   # the API's own entries, results kept, report nothing
    raw_trade(con, "RAW-1", entered=False)
    id = ev(con, "entered", T1, trade="RAW-1", payload={**ENTERED, key: value})
    assert only(con, "entered_checks") == 1
    assert found(con)[1][0].startswith(f"entered_checks: event {id} (trade RAW-1): {says}")


def test_integrity_reports_a_run_currency_mismatch_written_by_hand(con):
    assert found(con)[0] == 0
    t = tid(con, "ACME")
    ev(con, "corrected", T4, trade=t, corrects=con.execute(
        "SELECT id FROM events WHERE trade_id = ? AND kind = 'entered'", (t,)).fetchone()[0],
       payload={"fields": {"currency": "CAD"}, "reason": "by hand"})
    count, lines = found(con)
    assert any(f"trade {t}: in CAD, but its run {RUN} is in USD" in ln for ln in lines)
