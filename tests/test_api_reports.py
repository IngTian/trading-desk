"""Reported states through the api: one call, report, for fills, cancellations, open positions
and exits, with their corrections and withdrawals, each checked against the record alone. Every
figure is invented."""
import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from desk import api, model, store

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
T1, T2, T3, T4 = (NOW + timedelta(hours=n) for n in (1, 2, 3, 4))
LATER = NOW + timedelta(days=1)
BASE = dict(ticker="ACME", side="long", entry_type="limit", entry=100.0, stop=98.5, target=103.0,
            size=10, currency="USD", reasons="Dip into support.")
TEXT = "---\nname: Dip buyer\n---\n\n# Dip buyer\n\n```rules\ntimeframes = [\"1h\"]\n```\n"


@pytest.fixture
def con(tmp_path):
    store.create(tmp_path / "desk.db")
    with store.open(tmp_path / "desk.db") as conn:
        api.save_strategy(conn, "dip-buyer", TEXT, now=NOW)
        api.start_run(conn, "dip-buyer", mode="live", currency="USD", now=NOW)  # dip-buyer-r1
        api.start_run(conn, "dip-buyer", mode="replay", currency="USD", at=NOW,
                      now=NOW)  # dip-buyer-r2
        yield conn


def enter(con, run="dip-buyer-r2", **kw):
    return api.trade(con, run, **{**BASE, "at": T1, "now": LATER, **kw})


def fill(con, id, **kw):
    return api.report(con, id, **{"state": "filled", "reason": "Filled.", "at": T2, "now": LATER,
                                  **kw})


def cancel(con, id, **kw):
    return api.report(con, id, **{"state": "cancelled", "reason": "Pulled.", "why": "pulled",
                                  "at": T2, "now": LATER, **kw})


def held(con, id, **kw):
    return api.report(con, id, **{"state": "open", "reason": "Still in.", "at": T2, "now": LATER,
                                  **kw})


def out(con, id, **kw):
    return api.report(con, id, **{"state": "exited", "reason": "Out.", "at": T3, "now": LATER,
                                  **kw})


def close(con, id, **kw):
    return api.close_trade(con, id, **{"reason": "Out.", "at": T3, "now": LATER, **kw})


def last(con, id):
    e = store.events(con, trade_id=id)[-1]
    return e["kind"], e["at"], e["corrects"], json.loads(e["payload"])


def test_a_fill_is_one_filled_event_at_the_limit_unless_a_price_is_given(con):
    t = enter(con)
    assert fill(con, t.id).decisions.fill == model.Report(at=T2, price=None, reason="Filled.")
    assert last(con, t.id) == ("filled", T2.isoformat(), None, {"reason": "Filled."})
    u = enter(con)
    assert fill(con, u.id, price=99.75).decisions.fill.price == 99.75
    assert last(con, u.id)[3] == {"reason": "Filled.", "price": 99.75}


def test_a_cancellation_is_one_cancelled_event(con):
    t = enter(con)
    assert cancel(con, t.id).decisions.cancel == model.Report(at=T2, price=None, reason="Pulled.",
                                                              why="pulled")
    assert last(con, t.id) == ("cancelled", T2.isoformat(), None,
                               {"reason": "Pulled.", "why": "pulled"})
    u = enter(con)
    assert cancel(con, u.id, why="not filled").decisions.cancel.why == "not filled"


def test_an_exit_is_one_closed_event_with_its_kind_and_its_price(con):
    a, b = enter(con, entry_type="market"), enter(con, entry_type="market")
    d = out(con, a.id, kind="stop").decisions
    assert (d.close_at, d.close_kind, d.close_price) == (T3, "stop", None)
    assert last(con, a.id)[3] == {"reason": "Out.", "kind": "stop"}
    d = out(con, b.id, price=101.0).decisions
    assert (d.close_kind, d.close_price) == ("hand", 101.0)
    assert last(con, b.id)[3] == {"reason": "Out.", "price": 101.0}


def test_an_open_report_is_one_held_event_and_the_latest_stands(con):
    t = enter(con, entry_type="market")
    held(con, t.id, at=T3)
    v = held(con, t.id)                                    # dated earlier, logged later
    assert v.decisions.held == model.Report(at=T3, price=None, reason="Still in.")
    assert last(con, t.id) == ("held", T2.isoformat(), None, {"reason": "Still in."})


@pytest.mark.parametrize("run", ["dip-buyer-r1", "dip-buyer-r2"], ids=["live", "replay"])
def test_a_report_needs_its_at_live_and_replay(con, run):
    t = enter(con, run)
    with pytest.raises(api.Refused, match="needs `at`"):
        fill(con, t.id, at=None)
    with pytest.raises(api.Refused, match="after now"):
        fill(con, t.id, at=LATER + timedelta(hours=1))      # now is LATER
    assert [e["kind"] for e in store.events(con, trade_id=t.id)] == ["entered"]


def test_a_live_close_still_defaults_its_at_to_now(con):
    t = enter(con, "dip-buyer-r1", entry_type="market", at=None, now=T1)
    assert close(con, t.id, at=None, now=T2).decisions.close_at == T2


def test_a_replay_hand_close_of_a_pending_limit_needs_no_price(con):
    assert close(con, enter(con).id).decisions.close_kind == "hand"   # it cancels the order


SETUPS = {  # what a case does before the call it expects refused
    "fill": lambda con, t: fill(con, t),
    "fill 99.75": lambda con, t: fill(con, t, price=99.75),
    "fill at T3": lambda con, t: fill(con, t, at=T3),
    "cancel": lambda con, t: cancel(con, t),
    "cancel at T3": lambda con, t: cancel(con, t, at=T3),
    "open": lambda con, t: held(con, t),
    "open at T3": lambda con, t: held(con, t, at=T3),
    "close": lambda con, t: close(con, t),
    "close at T2": lambda con, t: close(con, t, at=T2),
    "hand exit": lambda con, t: out(con, t, price=101.0),
    "stop exit": lambda con, t: out(con, t, kind="stop"),
    "move at T3": lambda con, t: api.move_order(con, t, stop=99.0, reason="x", at=T3, now=LATER),
    "run stop at T2": lambda con, t: api.stop_run(con, "dip-buyer-r2", at=T2, now=LATER),
    "void": lambda con, t: api.void_trade(con, t, reason="x", now=LATER),
}
MARKET = dict(entry_type="market")
REFUSED = [  # (id, entry kwargs, setups, the call refused, a substring of the refusal)
    ("a state that is not known", {}, [], lambda con, t: fill(con, t, state="closed"), "state"),
    ("a field the state does not take", {}, [], lambda con, t: fill(con, t, kind="stop"),
     "does not go with"),
    ("a fill on a market entry", MARKET, [], lambda con, t: fill(con, t), "market entry"),
    ("a cancellation on a market entry", MARKET, [], lambda con, t: cancel(con, t), "market entry"),
    ("a second fill", {}, ["fill"], lambda con, t: fill(con, t), "already reported"),
    ("a second cancellation", {}, ["cancel"], lambda con, t: cancel(con, t, at=T3),
     "already reported"),
    ("a fill on a cancelled order", {}, ["cancel"], lambda con, t: fill(con, t),
     "cannot both fill"),
    ("a fill worse than the limit", {}, [], lambda con, t: fill(con, t, price=100.5),
     "worse than its limit 100.0"),
    ("a fill after the run stop", {}, ["run stop at T2"], lambda con, t: fill(con, t, at=T3),
     "after its run stopped"),
    ("a run stop before a reported fill", {}, ["fill at T3"],
     lambda con, t: api.stop_run(con, "dip-buyer-r2", at=T2, now=LATER), "after its run stopped"),
    ("a cancellation after the run stop", {}, ["run stop at T2"],
     lambda con, t: cancel(con, t, at=T3), "which had already cancelled it"),
    ("a run stop before a reported cancellation", {}, ["cancel at T3"],
     lambda con, t: api.stop_run(con, "dip-buyer-r2", at=T2, now=LATER),
     "which had already cancelled it"),
    ("a cancellation's cause that is not known", {}, [],
     lambda con, t: cancel(con, t, why="expired"), "why"),
    ("a cancellation with no cause", {}, [], lambda con, t: cancel(con, t, why=None), "why"),
    ("a fill under a priceless close", {}, ["close"], lambda con, t: fill(con, t, at=T2),
     "withdraw that close"),
    ("a move after a stop exit, naming its price", MARKET, ["stop exit"],
     lambda con, t: api.move_order(con, t, stop=99.0, reason="x", at=T4, now=LATER), "at 98.5"),
    ("an exit on a limit with no fill", {}, [], lambda con, t: out(con, t, kind="stop"),
     "no fill reported"),
    ("a hand exit on a limit with no fill", {}, [], lambda con, t: out(con, t, price=101.0),
     "no fill reported"),
    ("a hand exit with no price", MARKET, [], lambda con, t: out(con, t), "with the price"),
    ("an exit after a close", {}, ["close"], lambda con, t: out(con, t, price=101.0, at=T4),
     "already closed"),
    ("a stop exit better than the stop", MARKET, [],
     lambda con, t: out(con, t, kind="stop", price=99.0), "better than its stop 98.5"),
    ("a target exit worse than the target", MARKET, [],
     lambda con, t: out(con, t, kind="target", price=102.0), "worse than its target 103.0"),
    ("a stop exit with no stop", dict(MARKET, stop=None), [],
     lambda con, t: out(con, t, kind="stop"), "no stop in force"),
    ("a replay hand close of a position: its exit is reported", MARKET, [],
     lambda con, t: close(con, t), "state='exited'"),
    ("a cancellation at the entry's moment", {}, [], lambda con, t: cancel(con, t, at=T1),
     f"entered at {T1.isoformat()}"),
    ("a cancellation before a move", {}, ["move at T3"], lambda con, t: cancel(con, t),
     f"has a move at {T3.isoformat()}"),
    ("a move after a cancellation", {}, ["cancel"],
     lambda con, t: api.move_order(con, t, stop=99.0, reason="x", at=T3, now=LATER),
     "has a move at"),
    ("a close of a cancelled order", {}, ["cancel"], lambda con, t: close(con, t),
     "no position to close"),
    ("a fill after the close", {}, ["close at T2"], lambda con, t: fill(con, t, at=T3),
     "before its fill"),
    ("an open report on a limit with no fill", {}, [], lambda con, t: held(con, t),
     "report the fill first"),
    ("an open report on a cancelled order", {}, ["cancel"], lambda con, t: held(con, t, at=T3),
     "withdraw the cancellation"),
    ("an open report before the fill", {}, ["fill at T3"], lambda con, t: held(con, t),
     f"position opened at {T3.isoformat()}"),
    ("an older open report before the entry", MARKET, ["open at T3"],
     lambda con, t: held(con, t, at=NOW), f"position opened at {T1.isoformat()}"),
    ("a report dated after now", MARKET, [],
     lambda con, t: held(con, t, at=LATER + timedelta(hours=1)), "after now"),
    ("an open report after an exit", MARKET, ["stop exit"], lambda con, t: held(con, t, at=T4),
     "and reported open at"),
    ("an exit at an open report's moment", MARKET, ["open"],
     lambda con, t: out(con, t, kind="stop", at=T2), "and reported open at"),
    ("a kind that is not known", MARKET, [], lambda con, t: out(con, t, kind="trailing"), "kind"),
    ("a price that is not a number", {}, [], lambda con, t: fill(con, t, price="99"), "price"),
]


@pytest.mark.parametrize(("entry", "setup", "call", "says"), [c[1:] for c in REFUSED],
                         ids=[c[0] for c in REFUSED])
def test_a_report_that_does_not_fit_the_record_is_refused_and_writes_nothing(con, entry, setup,
                                                                             call, says):
    t = enter(con, **entry)
    for s in setup:
        SETUPS[s](con, t.id)
    n = len(store.events(con))
    with pytest.raises(api.Refused, match=re.escape(says)):
        call(con, t.id)
    assert len(store.events(con)) == n


def test_reports_are_corrected_in_place(con):
    t = enter(con)
    fill(con, t.id, price=99.75)
    v = api.correct_trade(con, t.id, fields={"report": "fill", "price": 99.5}, reason="Typo.",
                          now=LATER)
    filled = [e["id"] for e in store.events(con, trade_id=t.id) if e["kind"] == "filled"][0]
    assert v.decisions.fill.price == 99.5 and last(con, t.id)[:3] == (
        "corrected", LATER.isoformat(), filled)
    m = enter(con, entry_type="market")
    out(con, m.id, kind="stop")
    d = api.correct_trade(con, m.id, fields={"report": "close", "kind": "hand", "price": 99.0,
                                             "at": T4}, reason="By hand.", now=LATER).decisions
    assert (d.close_kind, d.close_price, d.close_at) == ("hand", 99.0, T4)
    c = enter(con)
    cancel(con, c.id)
    k = api.correct_trade(con, c.id, fields={"report": "cancel", "at": T3, "why": "not filled"},
                          reason="It was never reached.", now=LATER).decisions.cancel
    assert (k.at, k.why) == (T3, "not filled")
    o = enter(con, entry_type="market")
    held(con, o.id)
    assert api.correct_trade(con, o.id, fields={"report": "open", "at": T3}, reason="Later.",
                             now=LATER).decisions.held.at == T3


CORRECTIONS = [  # (id, entry kwargs, setups, fields, a substring of the refusal)
    ("a report that was not made", {}, [], {"report": "fill", "price": 99.0}, "no fill to correct"),
    ("a field a fill does not have", {}, ["fill"], {"report": "fill", "kind": "stop"},
     "cannot be corrected on a fill"),
    ("a field an open report does not have", MARKET, ["open"], {"report": "open", "price": 99.0},
     "cannot be corrected on an open report"),
    ("a fill price worse than the limit", {}, ["fill"], {"report": "fill", "price": 100.5},
     "worse than its limit"),
    ("the limit corrected under a fill", {}, ["fill 99.75"], {"entry": 99.5},
     "worse than its limit 99.5"),
    ("a fill moved past the close", {}, ["fill", "hand exit"], {"report": "fill", "at": T4},
     "before its fill"),
    ("a stop exit's price past the stop", MARKET, ["stop exit"], {"report": "close", "price": 99.0},
     "better than its stop"),
    ("a close moved onto the entry", MARKET, ["stop exit"], {"report": "close", "at": T1},
     f"entered at {T1.isoformat()}"),
    ("an open report moved onto the close", MARKET, ["open", "stop exit"],
     {"report": "open", "at": T3}, "and reported open at"),
    ("the latest open report moved before the entry", MARKET, ["open", "open at T3"],
     {"report": "open", "at": NOW}, f"position opened at {T1.isoformat()}"),
    ("a fill moved past an older open report", {}, ["fill", "open", "open at T3"],
     {"report": "fill", "at": T3}, f"position opened at {T3.isoformat()}"),
    ("a market entry moved past an open report", MARKET, ["open"], {"at": T3},
     f"position opened at {T3.isoformat()}"),
    ("a report moved after now", {}, ["fill"], {"report": "fill", "at": LATER + timedelta(hours=1)},
     "after now"),
    ("a move and a report at once", {}, ["fill"], {"move": 0, "report": "fill", "price": 99.0},
     "one at a time"),
    ("a report that is not known", {}, [], {"report": "exit", "price": 99.0}, "report"),
    ("a close kind that is not known", MARKET, ["stop exit"],
     {"report": "close", "kind": "trailing"}, "kind"),
    ("a report price that is not a number", {}, ["fill"], {"report": "fill", "price": "99"},
     "price"),
    ("a cancellation's cause that is not known", {}, ["cancel"], {"report": "cancel",
                                                                 "why": "expired"}, "why"),
]


@pytest.mark.parametrize(("entry", "setup", "fields", "says"), [c[1:] for c in CORRECTIONS],
                         ids=[c[0] for c in CORRECTIONS])
def test_a_correction_that_does_not_fit_the_record_is_refused_and_writes_nothing(con, entry, setup,
                                                                                 fields, says):
    t = enter(con, **entry)
    for s in setup:
        SETUPS[s](con, t.id)
    n = len(store.events(con))
    with pytest.raises(api.Refused, match=re.escape(says)):
        api.correct_trade(con, t.id, fields=fields, reason="x", now=LATER)
    assert len(store.events(con)) == n


def test_a_withdrawn_report_stops_counting_and_the_trade_stands(con):
    t = enter(con)
    fill(con, t.id)
    v = api.void_trade(con, t.id, reason="It never filled.", report="fill", now=LATER)
    assert (v.decisions.fill, v.decisions.voided) == (None, False)
    named = [e["id"] for e in store.events(con, trade_id=t.id) if e["kind"] == "filled"][0]
    assert last(con, t.id) == ("voided", LATER.isoformat(), named, {"reason": "It never filled."})
    assert fill(con, t.id, at=T3).decisions.fill.at == T3                 # reported again
    assert api.move_order(con, t.id, stop=99.0, reason="x", at=T3, now=LATER).decisions.moves


def test_a_withdrawn_close_lets_the_trade_close_again(con):
    t = enter(con, entry_type="market")
    out(con, t.id, kind="stop")
    api.void_trade(con, t.id, reason="Not stopped.", report="close", now=LATER)
    d = out(con, t.id, price=101.0, at=T4).decisions
    assert (d.close_kind, d.close_price, d.close_at) == ("hand", 101.0, T4)


def test_a_fill_found_under_a_priceless_close_is_reported_by_withdrawing_that_close(con):
    t = enter(con)
    close(con, t.id)                                   # at T3, while the order seemed pending
    api.void_trade(con, t.id, reason="It had filled.", report="close", now=LATER)
    fill(con, t.id, at=T2)
    d = out(con, t.id, price=101.0).decisions
    assert (d.fill.at, d.close_at, d.close_kind, d.close_price) == (T2, T3, "hand", 101.0)


def test_withdrawing_the_latest_open_report_leaves_the_one_before_it(con):
    t = enter(con, entry_type="market")
    held(con, t.id, at=T3)
    held(con, t.id)                                    # at T2, logged later
    v = api.void_trade(con, t.id, reason="Not then.", report="open", now=LATER)
    assert v.decisions.held.at == T2


WITHDRAWALS = [  # (id, entry kwargs, setups, report, a substring of the refusal)
    ("a fill under an exit", {}, ["fill", "hand exit"], "fill",
     "withdraw the exit before the fill"),
    ("a fill under an open report", {}, ["fill", "open at T3"], "fill", "report the fill first"),
    ("a report that was not made", {}, [], "cancel", "no cancellation to withdraw"),
    ("an open report that was not made", MARKET, [], "open", "no open report to withdraw"),
    ("a report that is not known", {}, [], "exit", "report"),
    ("a report on a void trade", {}, ["fill", "void"], "fill", "is void"),
]


@pytest.mark.parametrize(("entry", "setup", "report", "says"), [c[1:] for c in WITHDRAWALS],
                         ids=[c[0] for c in WITHDRAWALS])
def test_a_withdrawal_that_does_not_fit_the_record_is_refused_and_writes_nothing(con, entry, setup,
                                                                                 report, says):
    t = enter(con, **entry)
    for s in setup:
        SETUPS[s](con, t.id)
    n = len(store.events(con))
    with pytest.raises(api.Refused, match=re.escape(says)):
        api.void_trade(con, t.id, reason="x", report=report, now=LATER)
    assert len(store.events(con)) == n
