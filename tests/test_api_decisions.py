"""Tests for the decisions after entry: moves, hand exits, corrections and voids, and reading
trades back. Every figure is invented."""
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from desk import api, store

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
T1 = NOW + timedelta(hours=1)
T2 = NOW + timedelta(hours=2)
T3 = NOW + timedelta(hours=3)
LATER = NOW + timedelta(days=1)  # the wall clock when a record-keeping event is written
BASE = dict(ticker="ACME", side="long", entry_type="limit", entry=100.0, stop=98.5, target=103.0,
            size=10, currency="USD", instrument="stock", reasons="Dip into support.")
RULES = "```rules\ntimeframes = [\"1h\"]\nmin_reward_risk = 2\n```\n"


def strategy(name):
    return f"---\nname: {name}\n---\n\n# {name}\n\n{RULES}"


@pytest.fixture
def con(tmp_path):
    db = tmp_path / "desk.db"
    store.create(db)
    with store.open(db) as conn:
        api.save_strategy(conn, "dip-buyer", strategy("Dip buyer"), now=NOW)
        api.save_strategy(conn, "trend", strategy("Trend"), now=NOW)
        api.start_run(conn, "dip-buyer", mode="live", currency="USD", now=NOW)
        api.start_run(conn, "dip-buyer", mode="replay", currency="USD", at=NOW, now=NOW)
        api.start_run(conn, "trend", mode="replay", currency="USD", at=NOW, now=NOW)
        yield conn


def enter(con, run="dip-buyer-r2", **kw):
    return api.trade(con, run, **{**BASE, "at": T1, "now": LATER, **kw})


def events(con, trade_id):
    return [(e["kind"], e["at"], e["corrects"], json.loads(e["payload"]))
            for e in store.events(con, trade_id=trade_id)]


def kinds(con, trade_id):
    return [e[0] for e in events(con, trade_id)]


def out(con, id, **kw):
    return api.report(con, id, **{"state": "exited", "price": 101.0, "reason": "x", "at": T3,
                                  "now": LATER, **kw})


# --- move_order -----------------------------------------------------------------------------

def test_a_move_is_one_order_moved_event_naming_only_what_moved(con):
    t = enter(con)
    v = api.move_order(con, t.id, stop=99.0, reason="Tighter.", at=T2, now=LATER)
    assert v.decisions.moves == (api.model.Move(at=T2, stop=99.0, target=None, reason="Tighter."),)
    assert events(con, t.id)[-1] == ("order_moved", T2.isoformat(), None,
                                     {"stop": 99.0, "reason": "Tighter."})
    api.move_order(con, t.id, target=104, reason="More room.", at=T3, now=LATER)
    api.move_order(con, t.id, stop=99.5, target=105.0, reason="Both.", at=T3, now=LATER)
    assert [e[3] for e in events(con, t.id)[-2:]] == [
        {"target": 104.0, "reason": "More room."},
        {"stop": 99.5, "target": 105.0, "reason": "Both."}]


def test_moves_logged_out_of_order_are_listed_in_at_order(con):
    t = enter(con)
    api.move_order(con, t.id, stop=99.5, reason="Second.", at=T3, now=LATER)
    api.move_order(con, t.id, stop=99.0, reason="First.", at=T2, now=LATER)
    assert [m.reason for m in api.get_trade(con, t.id).decisions.moves] == ["First.", "Second."]


def test_a_stop_moved_to_the_entry_is_accepted(con):
    t = enter(con)
    v = api.move_order(con, t.id, stop=100.0, reason="Breakeven.", at=T2, now=LATER)
    assert v.decisions.moves[0].stop == 100.0


def test_a_live_move_defaults_to_now(con):
    t = enter(con, "dip-buyer-r1", at=None, now=T1)
    assert api.move_order(con, t.id, stop=99.0, reason="x", now=T2).decisions.moves[0].at == T2


@pytest.mark.parametrize("kw", [
    dict(), dict(stop=None, target=None), dict(stop=0), dict(stop=-1), dict(stop=float("nan")),
    dict(target=float("inf")), dict(stop=True), dict(target="104")])
def test_a_move_must_name_a_finite_positive_stop_or_target(con, kw):
    t = enter(con)
    with pytest.raises(api.Refused, match="stop|target"):
        api.move_order(con, t.id, reason="x", at=T2, now=LATER, **kw)
    assert kinds(con, t.id) == ["entered"]


# --- the hand exit, reported ----------------------------------------------------------------

def test_a_move_logged_after_an_exit_but_dated_before_it_is_accepted(con):
    t = enter(con, entry_type="market")
    out(con, t.id)
    assert api.move_order(con, t.id, stop=99.0, reason="x", at=T2, now=LATER).decisions.moves


@pytest.mark.parametrize("at", [T3, T3 + timedelta(minutes=1)])
def test_a_move_at_or_after_the_hand_exit_is_refused(con, at):
    """A hand exit is a recorded decision, so a move dated from it on has no order left to
    move."""
    t = enter(con, entry_type="market")
    out(con, t.id)
    with pytest.raises(api.Refused, match=f"at or after its exit at {re.escape(T3.isoformat())}"):
        api.move_order(con, t.id, stop=99.0, reason="x", at=at, now=LATER)
    assert kinds(con, t.id) == ["entered", "exited"]
    api.move_order(con, t.id, stop=99.0, reason="x", at=T3 - timedelta(minutes=1), now=LATER)


@pytest.mark.parametrize("at", [T2, T3 - timedelta(minutes=1), T3])
def test_an_exit_at_or_before_a_recorded_move_is_refused(con, at):
    """The same rule from the other side: the exit may not leave a move dated at or after it."""
    t = enter(con, entry_type="market")
    api.move_order(con, t.id, stop=99.0, reason="x", at=T2, now=LATER)
    api.move_order(con, t.id, stop=99.5, reason="y", at=T3, now=LATER)
    with pytest.raises(api.Refused, match="at or after its exit at"):
        out(con, t.id, at=at)
    assert kinds(con, t.id) == ["entered", "order_moved", "order_moved"]
    out(con, t.id, at=T3 + timedelta(minutes=1))


# --- rules every decision after entry follows -----------------------------------------------

def move(con, id, **kw):
    return api.move_order(con, id, stop=99.0, **{"reason": "x", "at": T2, "now": LATER, **kw})


def correct(con, id, **kw):
    return api.correct_trade(con, id, **{"fields": {"stop": 97.0}, "reason": "x", "now": LATER,
                                         **kw})


def void(con, id, **kw):
    return api.void_trade(con, id, **{"reason": "x", "now": LATER, **kw})


def fill(con, id, **kw):
    return api.report(con, id, **{"state": "filled", "reason": "x", "at": T2, "now": LATER, **kw})


def cancel(con, id, **kw):
    return api.report(con, id, **{"state": "cancelled", "why": "pulled", "reason": "x", "at": T2,
                                  "now": LATER, **kw})


DECISIONS = [move, correct, void, fill, cancel]
DATED = [move, fill, cancel]


@pytest.mark.parametrize("decide", DECISIONS)
@pytest.mark.parametrize("reason", ["", "   ", None])
def test_every_decision_needs_a_reason(con, decide, reason):
    t = enter(con)
    with pytest.raises(api.Refused, match="reason"):
        decide(con, t.id, reason=reason)
    assert kinds(con, t.id) == ["entered"]


@pytest.mark.parametrize("decide", DECISIONS)
@pytest.mark.parametrize("reason", [5, ["x"]])
def test_a_reason_that_is_not_text_is_refused(con, decide, reason):
    t = enter(con)
    with pytest.raises(api.Refused, match="reason must be text"):
        decide(con, t.id, reason=reason)
    assert kinds(con, t.id) == ["entered"]


@pytest.mark.parametrize("decide", DECISIONS)
def test_an_unknown_trade_is_refused(con, decide):
    with pytest.raises(api.Refused, match="no trade"):
        decide(con, "nope-20260302")


@pytest.mark.parametrize("decide", DECISIONS)
def test_a_trade_id_that_is_not_text_is_refused(con, decide):
    n = len(store.events(con))
    with pytest.raises(api.Refused, match="trade must be text"):
        decide(con, 5)
    assert len(store.events(con)) == n


@pytest.mark.parametrize("decide", DATED)
def test_a_replay_decision_without_at_is_refused(con, decide):
    t = enter(con)
    with pytest.raises(api.Refused, match="replay"):
        decide(con, t.id, at=None)
    assert kinds(con, t.id) == ["entered"]


@pytest.mark.parametrize("decide", DATED)
def test_a_decision_before_the_entry_is_refused(con, decide):
    t = enter(con)
    with pytest.raises(api.Refused, match=re.escape(f"entered at {T1.isoformat()}")):
        decide(con, t.id, at=T1 - timedelta(minutes=1))
    decide(con, t.id, at=T1 + timedelta(minutes=1))
    assert kinds(con, t.id) == ["entered", {move: "order_moved", fill: "filled",
                                            cancel: "cancelled"}[decide]]


def test_a_move_at_the_entrys_own_moment_is_accepted(con):
    t = enter(con)
    assert move(con, t.id, at=T1).decisions.moves


def test_an_exit_at_the_entrys_moment_is_refused(con):
    """An exit needs the entry to have taken effect, so it is strictly after it."""
    t = enter(con, entry_type="market")
    with pytest.raises(api.Refused, match=re.escape(f"entered at {T1.isoformat()}")):
        out(con, t.id, at=T1)
    assert kinds(con, t.id) == ["entered"]
    out(con, t.id, at=T1 + timedelta(seconds=1))


def test_a_correction_moving_the_entry_onto_the_hand_exit_is_refused(con):
    """The same rule from the other side: a correction may not move the entry onto the exit."""
    t = enter(con, entry_type="market")
    out(con, t.id)
    with pytest.raises(api.Refused, match=re.escape(T3.isoformat())):
        correct(con, t.id, fields={"at": T3})
    assert kinds(con, t.id) == ["entered", "exited"]
    assert correct(con, t.id, fields={"at": T3 - timedelta(seconds=1)}).decisions.entry.at < T3


@pytest.mark.parametrize("decide", DATED)
def test_a_decision_on_a_corrected_entry_is_dated_against_the_corrected_at(con, decide):
    t = enter(con)
    correct(con, t.id, fields={"at": T2})
    with pytest.raises(api.Refused, match="entered at"):
        decide(con, t.id, at=T2 - timedelta(minutes=1))


@pytest.mark.parametrize("decide", DATED)
def test_a_naive_at_is_refused(con, decide):
    t = enter(con)
    with pytest.raises(api.Refused, match="ISO-8601 time with its UTC offset"):
        decide(con, t.id, at=datetime(2026, 3, 2, 14, 0))


@pytest.mark.parametrize("decide", DECISIONS)
def test_a_voided_trade_accepts_no_decision(con, decide):
    t = enter(con)
    void(con, t.id, reason="Wrong run.")
    with pytest.raises(api.Refused, match="void"):
        decide(con, t.id)
    assert kinds(con, t.id) == ["entered", "voided"]


# --- correct_trade --------------------------------------------------------------------------

def test_a_correction_names_the_entered_event_and_is_dated_now(con):
    t = enter(con)
    v = correct(con, t.id, fields={"stop": 97.5, "size": 12}, reason="Typo.")
    assert (v.decisions.entry.stop, v.decisions.entry.size, v.decisions.entry.price) == (
        97.5, 12, 100.0)
    entered_id = store.events(con, trade_id=t.id)[0]["id"]
    assert events(con, t.id)[-1] == ("corrected", LATER.isoformat(), entered_id,
                                     {"fields": {"stop": 97.5, "size": 12}, "reason": "Typo."})


def test_a_correction_of_a_correction_resolves_to_the_last(con):
    t = enter(con)
    correct(con, t.id, fields={"stop": 97.5, "target": 104.0})
    v = correct(con, t.id, fields={"stop": 97.0})
    assert (v.decisions.entry.stop, v.decisions.entry.target) == (97.0, 104.0)


def test_a_correction_may_change_each_entry_field(con):
    t = enter(con, ticker="GC1!", instrument=None, point_value=100.0)
    v = correct(con, t.id, fields={"entry": 101, "stop": 99.0, "target": 105.0, "size": 2,
                                   "at": T2, "point_value": 50.0})
    e = v.decisions.entry
    assert (e.price, e.stop, e.target, e.size, e.at) == (101.0, 99.0, 105.0, 2, T2)
    assert (v.point_value, v.currency) == (50.0, "USD")
    assert (v.decisions.point_value, v.decisions.currency) == (50.0, "USD")
    assert events(con, t.id)[-1][3]["fields"]["at"] == T2.isoformat()


def test_a_none_field_is_read_as_not_given(con):
    """Ruling R14: None means "not given" everywhere. An agent filling the keys it did not mean
    to touch with null used to erase the trade's stop and target."""
    t = enter(con)
    e = correct(con, t.id, fields={"entry": 101, "stop": None, "target": None}).decisions.entry
    assert (e.price, e.stop, e.target) == (101.0, 98.5, 103.0)
    assert events(con, t.id)[-1][3]["fields"] == {"entry": 101.0}


def test_a_none_move_is_read_as_not_given(con):
    t = enter(con)
    assert correct(con, t.id, fields={"move": None, "stop": 97.0}).decisions.entry.stop == 97.0


@pytest.mark.parametrize("fields", [{"stop": None}, {"target": None},
                                    {"stop": None, "target": None}, {"move": 0, "stop": None}])
def test_a_stop_or_target_cannot_be_removed_by_a_correction(con, fields):
    t = enter(con)
    api.move_order(con, t.id, stop=99.0, reason="x", at=T2, now=LATER)
    with pytest.raises(api.Refused, match="(?s)nothing to correct.*void the trade"):
        correct(con, t.id, fields=fields)
    assert kinds(con, t.id) == ["entered", "order_moved"]


@pytest.mark.parametrize("fields", [None, "stop", [("stop", 97.0)], 5])
def test_fields_that_are_not_a_mapping_are_refused(con, fields):
    t = enter(con)
    with pytest.raises(api.Refused, match="fields must be a mapping"):
        correct(con, t.id, fields=fields)
    assert kinds(con, t.id) == ["entered"]


@pytest.mark.parametrize("fields", [
    {}, {"side": "short"}, {"ticker": "WXYZ"}, {"reasons": "y"}, {"entry_type": "market"},
    {"entry": 0}, {"entry": float("nan")}, {"entry": None}, {"stop": -1}, {"target": "x"},
    {"stop": 100.0}, {"entry": 98.5}, {"size": 0}, {"size": 1.5}, {"size": True},
    {"currency": "EUR"}, {"point_value": 50.0}, {"at": "2026-03-02T14:00:00"},
    {"at": datetime(2026, 3, 2, 14, 0)}, {"at": None}])
def test_a_bad_entry_correction_is_refused_and_writes_nothing(con, fields):
    t = enter(con)  # a stock: its point value is 1, not a figure to correct
    with pytest.raises(api.Refused):
        correct(con, t.id, fields=fields)
    assert kinds(con, t.id) == ["entered"]


def test_a_corrected_at_before_the_run_started_is_refused(con):
    t = enter(con)
    with pytest.raises(api.Refused, match="started"):
        correct(con, t.id, fields={"at": NOW - timedelta(minutes=1)})
    assert kinds(con, t.id) == ["entered"]
    correct(con, t.id, fields={"at": NOW})


def test_a_corrected_at_at_or_after_the_run_stop_is_refused(con):
    t = enter(con)
    api.stop_run(con, "dip-buyer-r2", at=T3, now=LATER)
    with pytest.raises(api.Refused, match="stopped"):
        correct(con, t.id, fields={"at": T3})
    assert kinds(con, t.id) == ["entered"]


@pytest.mark.parametrize("decide", [move])  # an exit at the entry's `at` is tested above
def test_a_corrected_at_after_a_recorded_decision_is_refused(con, decide):
    t = enter(con)
    decide(con, t.id, at=T2)
    with pytest.raises(api.Refused, match=re.escape(T2.isoformat())):
        correct(con, t.id, fields={"at": T2 + timedelta(minutes=1)})
    assert correct(con, t.id, fields={"at": T2}).decisions.entry.at == T2


def test_a_move_is_corrected_by_its_index_in_at_order(con):
    t = enter(con)
    api.move_order(con, t.id, stop=99.5, reason="Second.", at=T3, now=LATER)
    api.move_order(con, t.id, stop=99.0, reason="First.", at=T2, now=LATER)
    v = correct(con, t.id, fields={"move": 1, "stop": 99.8, "target": 104.0}, reason="Typo.")
    assert [(m.reason, m.stop, m.target) for m in v.decisions.moves] == [
        ("First.", 99.0, None), ("Second.", 99.8, 104.0)]
    second_id = store.events(con, trade_id=t.id)[1]["id"]
    assert events(con, t.id)[-1] == ("corrected", LATER.isoformat(), second_id,
                                     {"fields": {"stop": 99.8, "target": 104.0},
                                      "reason": "Typo."})
    assert v.decisions.entry.stop == 98.5  # the entry is untouched


def test_a_corrected_move_at_reorders_the_moves(con):
    t = enter(con)
    api.move_order(con, t.id, stop=99.0, reason="a", at=T2, now=LATER)
    api.move_order(con, t.id, stop=99.5, reason="b", at=T3, now=LATER)
    v = correct(con, t.id, fields={"move": 0, "at": T3 + timedelta(hours=1)})
    assert [m.reason for m in v.decisions.moves] == ["b", "a"]


@pytest.mark.parametrize("fields", [
    {"move": 2, "stop": 99.0}, {"move": -1, "stop": 99.0}, {"move": "0", "stop": 99.0},
    {"move": True, "stop": 99.0}, {"move": 0}, {"move": 0, "size": 2},
    {"move": 0, "entry": 101.0}, {"move": 0, "stop": 0}, {"move": 0, "stop": None},
    {"move": 0, "at": T1 - timedelta(minutes=1)}, {"move": 0, "at": datetime(2026, 3, 2, 14)},
    {"move": 0, "at": None}])
def test_a_bad_move_correction_is_refused_and_writes_nothing(con, fields):
    t = enter(con)
    api.move_order(con, t.id, stop=99.0, reason="x", at=T2, now=LATER)
    api.move_order(con, t.id, target=104.0, reason="y", at=T3, now=LATER)
    with pytest.raises(api.Refused):
        correct(con, t.id, fields=fields)
    assert kinds(con, t.id) == ["entered", "order_moved", "order_moved"]


def test_a_move_correction_on_a_trade_with_no_moves_is_refused(con):
    t = enter(con)
    with pytest.raises(api.Refused, match="no move"):
        correct(con, t.id, fields={"move": 0, "stop": 99.0})


@pytest.mark.parametrize("at", [T3, T3 + timedelta(minutes=1)])
def test_a_corrected_move_at_at_or_after_the_hand_exit_is_refused(con, at):
    t = enter(con, entry_type="market")
    api.move_order(con, t.id, stop=99.0, reason="a", at=T1 + timedelta(minutes=30), now=LATER)
    api.move_order(con, t.id, stop=99.5, reason="b", at=T2, now=LATER)
    out(con, t.id)
    with pytest.raises(api.Refused, match=f"at or after its exit at {re.escape(T3.isoformat())}"):
        correct(con, t.id, fields={"move": 0, "at": at})
    assert kinds(con, t.id) == ["entered", "order_moved", "order_moved", "exited"]
    # still before the exit, and past the other move: moves re-order among themselves
    v = correct(con, t.id, fields={"move": 0, "at": T3 - timedelta(minutes=1)})
    assert [m.reason for m in v.decisions.moves] == ["b", "a"]


# --- void_trade -----------------------------------------------------------------------------

def test_a_void_is_dated_now_and_keeps_the_trade(con):
    t = enter(con)
    v = void(con, t.id, reason="Wrong run.")
    assert v.decisions.voided and v.decisions.entry == t.decisions.entry
    assert events(con, t.id)[-1] == ("voided", LATER.isoformat(), None, {"reason": "Wrong run."})


def test_corrections_and_voids_end_to_end(con):
    """A correction of a correction resolves to the last; a corrected `at` is never before the
    run's start or after a decision on the trade; a void ends the trade; it is entered again
    with its original `at`."""
    t = enter(con)
    correct(con, t.id, fields={"stop": 97.5})
    assert correct(con, t.id, fields={"stop": 97.0}).decisions.entry.stop == 97.0
    api.move_order(con, t.id, stop=99.0, reason="Tighter.", at=T3, now=LATER)
    for at in (NOW - timedelta(seconds=1), T3 + timedelta(seconds=1)):
        with pytest.raises(api.Refused):
            correct(con, t.id, fields={"at": at})
    assert api.get_trade(con, t.id).decisions.entry.at == T1
    void(con, t.id, reason="Entered on the wrong run.")
    for decide in DECISIONS:
        with pytest.raises(api.Refused, match="void"):
            decide(con, t.id)
    again = enter(con, "trend-r1", at=t.decisions.entry.at)
    assert again.id != t.id and again.decisions.entry.at == T1 and not again.decisions.voided
    assert api.get_trade(con, t.id).decisions.voided


# --- reading trades back --------------------------------------------------------------------

def test_get_trade_reads_what_trade_returned(con):
    t = enter(con)
    assert replace(api.get_trade(con, t.id), checks_now=None, checks_differ=None) == t


def test_list_trades_by_run_and_by_strategy(con):
    a = enter(con, "dip-buyer-r1", at=None, now=T1)
    b = enter(con)
    c = enter(con, "trend-r1")
    void(con, b.id)
    assert [t.id for t in api.list_trades(con)] == [a.id, b.id, c.id]
    assert [t.id for t in api.list_trades(con, run="dip-buyer-r2")] == [b.id]
    assert [t.id for t in api.list_trades(con, strategy="dip-buyer")] == [a.id, b.id]
    assert [t.id for t in api.list_trades(con, strategy="trend")] == [c.id]
    assert api.list_trades(con, run="dip-buyer-r1", strategy="trend") == []
    assert api.list_trades(con)[1].decisions.voided


@pytest.mark.parametrize("call", [
    lambda con, t, at: api.move_order(con, t, stop=99.0, reason="x", at=at, now=LATER),
    lambda con, t, at: api.correct_trade(con, t, fields={"at": at}, reason="x", now=LATER),
])
def test_a_decision_dated_after_now_is_refused(con, call):
    t = enter(con)
    before = len(store.events(con))
    with pytest.raises(api.Refused, match="is after now"):
        call(con, t.id, LATER + timedelta(hours=1))
    assert len(store.events(con)) == before


@pytest.mark.parametrize("fields", [{"stop": 101.0}, {"target": 99.0}, {"entry": 98.0}])
def test_a_corrected_entry_on_the_wrong_side_is_refused(con, fields):
    t = enter(con)
    before = len(store.events(con))
    with pytest.raises(api.Refused, match="wrong side"):
        api.correct_trade(con, t.id, fields=fields, reason="x", now=LATER)
    assert len(store.events(con)) == before


def test_currency_is_not_correctable(con):
    t = enter(con)
    with pytest.raises(api.Refused, match="cannot be corrected"):
        api.correct_trade(con, t.id, fields={"currency": "USD"}, reason="x", now=LATER)
