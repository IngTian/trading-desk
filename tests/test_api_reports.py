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
            size=10, currency="USD", instrument="stock", reasons="Dip into support.")
TEXT = "---\nname: Dip buyer\n---\n\n# Dip buyer\n\n```rules\ntimeframes = [\"1h\"]\n```\n"
LIVE = "dip-buyer-r1"   # the live run; the fixture's replay run is dip-buyer-r2


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


def opened(con, id, **kw):
    return api.report(con, id, **{"state": "open", "reason": "Still in.", "at": T2, "now": LATER,
                                  **kw})


def out(con, id, **kw):
    return api.report(con, id, **{"state": "exited", "reason": "Out.", "at": T3, "now": LATER,
                                  **kw})


def last(con, id):
    e = store.events(con, trade_id=id)[-1]
    return e["kind"], e["at"], e["corrects"], json.loads(e["payload"])


def test_a_fill_is_one_filled_event_at_the_limit_unless_a_price_is_given(con):
    t = enter(con)
    assert fill(con, t.id).decisions.filled == model.Report(at=T2, price=None, reason="Filled.")
    assert last(con, t.id) == ("filled", T2.isoformat(), None, {"reason": "Filled."})
    u = enter(con)
    assert fill(con, u.id, price=99.75).decisions.filled.price == 99.75
    assert last(con, u.id)[3] == {"reason": "Filled.", "price": 99.75}


def test_a_cancellation_is_one_cancelled_event(con):
    t = enter(con)
    assert cancel(con, t.id).decisions.cancelled == model.Report(
        at=T2, price=None, reason="Pulled.", why="pulled")
    assert last(con, t.id) == ("cancelled", T2.isoformat(), None,
                               {"reason": "Pulled.", "why": "pulled"})
    u = enter(con)
    assert cancel(con, u.id, why="not filled").decisions.cancelled.why == "not filled"


def test_an_exit_is_one_exited_event_with_its_kind_and_its_price(con):
    a, b = enter(con, entry_type="market"), enter(con, entry_type="market")
    d = out(con, a.id, kind="stop").decisions
    assert (d.exited.at, d.exited.kind, d.exited.price) == (T3, "stop", None)
    assert last(con, a.id)[3] == {"reason": "Out.", "kind": "stop"}
    d = out(con, b.id, price=101.0).decisions
    assert (d.exited.kind, d.exited.price) == ("hand", 101.0)
    assert last(con, b.id)[3] == {"reason": "Out.", "kind": "hand", "price": 101.0}


def test_an_open_report_is_one_open_event_and_the_latest_stands(con):
    t = enter(con, entry_type="market")
    opened(con, t.id, at=T3)
    v = opened(con, t.id)                                    # dated earlier, logged later
    assert v.decisions.open == model.Report(at=T3, price=None, reason="Still in.")
    assert last(con, t.id) == ("open", T2.isoformat(), None, {"reason": "Still in."})


@pytest.mark.parametrize("run", ["dip-buyer-r1", "dip-buyer-r2"], ids=["live", "replay"])
def test_a_report_needs_its_at_live_and_replay(con, run):
    t = enter(con, run)
    with pytest.raises(api.Refused, match="needs `at`"):
        fill(con, t.id, at=None)
    with pytest.raises(api.Refused, match="after now"):
        fill(con, t.id, at=LATER + timedelta(hours=1))      # now is LATER
    assert [e["kind"] for e in store.events(con, trade_id=t.id)] == ["entered"]


SETUPS = {  # what a case does before the call it expects refused
    "filled": lambda con, t: fill(con, t),
    "fill 99.75": lambda con, t: fill(con, t, price=99.75),
    "fill at T3": lambda con, t: fill(con, t, at=T3),
    "cancelled": lambda con, t: cancel(con, t),
    "cancel at T3": lambda con, t: cancel(con, t, at=T3),
    "open": lambda con, t: opened(con, t),
    "open at T3": lambda con, t: opened(con, t, at=T3),
    "hand exit": lambda con, t: out(con, t, price=101.0),
    "stop exit": lambda con, t: out(con, t, kind="stop"),
    "move at T3": lambda con, t: api.move_order(con, t, stop=99.0, reason="x", at=T3, now=LATER),
    "run stop at T2": lambda con, t: api.stop_run(con, "dip-buyer-r2", at=T2, now=LATER),
    "void": lambda con, t: api.void_trade(con, t, reason="x", now=LATER),
}
MARKET = dict(entry_type="market")
REFUSED = [  # (id, entry kwargs, setups, the call refused, a substring of the refusal)
    ("a state that is not known", {}, [], lambda con, t: fill(con, t, state="shut"), "state"),
    ("a field the state does not take", {}, [], lambda con, t: fill(con, t, kind="stop"),
     "does not go with"),
    ("a fill on a market entry", MARKET, [], lambda con, t: fill(con, t), "market entry"),
    ("a cancellation on a market entry", MARKET, [], lambda con, t: cancel(con, t), "market entry"),
    ("a second fill", {}, ["filled"], lambda con, t: fill(con, t), "already reported"),
    ("a second cancellation", {}, ["cancelled"], lambda con, t: cancel(con, t, at=T3),
     "already reported"),
    ("a fill on a cancelled order", {}, ["cancelled"], lambda con, t: fill(con, t),
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
    ("a move after a stop exit", MARKET, ["stop exit"],
     lambda con, t: api.move_order(con, t, stop=99.0, reason="x", at=T4, now=LATER),
     "at or after its exit at"),
    ("an exit on a limit with no fill", {}, [], lambda con, t: out(con, t, kind="stop"),
     "no fill reported"),
    ("a hand exit on a limit with no fill", {}, [], lambda con, t: out(con, t, price=101.0),
     "no fill reported"),
    ("a hand exit with no price", MARKET, [], lambda con, t: out(con, t), "with the price"),
    ("a live hand exit with no price", dict(MARKET, run=LIVE), [], lambda con, t: out(con, t),
     "with the price"),
    ("a live exit on a limit with no fill", dict(run=LIVE), [],
     lambda con, t: out(con, t, kind="stop"), "no fill reported"),
    ("a second exit", MARKET, ["stop exit"],
     lambda con, t: out(con, t, price=101.0, at=T4), "already exited"),
    ("a stop exit better than the stop", MARKET, [],
     lambda con, t: out(con, t, kind="stop", price=99.0), "better than its stop 98.5"),
    ("a target exit worse than the target", MARKET, [],
     lambda con, t: out(con, t, kind="target", price=102.0), "worse than its target 103.0"),
    ("a stop exit with no stop", dict(MARKET, stop=None), [],
     lambda con, t: out(con, t, kind="stop"), "no stop in force"),
    ("a cancellation at the entry's moment", {}, [], lambda con, t: cancel(con, t, at=T1),
     f"entered at {T1.isoformat()}"),
    ("a cancellation before a move", {}, ["move at T3"], lambda con, t: cancel(con, t),
     f"has a move at {T3.isoformat()}"),
    ("a move after a cancellation", {}, ["cancelled"],
     lambda con, t: api.move_order(con, t, stop=99.0, reason="x", at=T3, now=LATER),
     "has a move at"),
    ("an exit on a cancelled order", {}, ["cancelled"], lambda con, t: out(con, t, price=101.0),
     "no position to exit"),
    ("an open report on a limit with no fill", {}, [], lambda con, t: opened(con, t),
     "report the fill first"),
    ("an open report on a cancelled order", {}, ["cancelled"], lambda con, t: opened(con, t, at=T3),
     "withdraw the cancellation"),
    ("an open report before the fill", {}, ["fill at T3"], lambda con, t: opened(con, t),
     f"opened at {T3.isoformat()}, so it cannot be reported open"),
    ("an older open report before the entry", MARKET, ["open at T3"],
     lambda con, t: opened(con, t, at=NOW),
     f"opened at {T1.isoformat()}, so it cannot be reported open"),
    ("a report dated after now", MARKET, [],
     lambda con, t: opened(con, t, at=LATER + timedelta(hours=1)), "after now"),
    ("an open report after an exit", MARKET, ["stop exit"], lambda con, t: opened(con, t, at=T4),
     "and was reported open at"),
    ("an exit at an open report's moment", MARKET, ["open"],
     lambda con, t: out(con, t, kind="stop", at=T2), "and was reported open at"),
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
    v = api.correct_trade(con, t.id, fields={"report": "filled", "price": 99.5}, reason="Typo.",
                          now=LATER)
    filled = [e["id"] for e in store.events(con, trade_id=t.id) if e["kind"] == "filled"][0]
    assert v.decisions.filled.price == 99.5 and last(con, t.id)[:3] == (
        "corrected", LATER.isoformat(), filled)
    m = enter(con, entry_type="market")
    out(con, m.id, kind="stop")
    d = api.correct_trade(con, m.id, fields={"report": "exited", "kind": "hand", "price": 99.0,
                                             "at": T4}, reason="By hand.", now=LATER).decisions
    assert (d.exited.kind, d.exited.price, d.exited.at) == ("hand", 99.0, T4)
    c = enter(con)
    cancel(con, c.id)
    k = api.correct_trade(con, c.id, fields={"report": "cancelled", "at": T3, "why": "not filled"},
                          reason="It was never reached.", now=LATER).decisions.cancelled
    assert (k.at, k.why) == (T3, "not filled")
    o = enter(con, entry_type="market")
    opened(con, o.id)
    assert api.correct_trade(con, o.id, fields={"report": "open", "at": T3}, reason="Later.",
                             now=LATER).decisions.open.at == T3


CORRECTIONS = [  # (id, entry kwargs, setups, fields, a substring of the refusal)
    ("a report that was not made", {}, [], {"report": "filled", "price": 99.0},
     "no fill to correct"),
    ("a field a fill does not have", {}, ["filled"], {"report": "filled", "kind": "stop"},
     "cannot be corrected on a fill"),
    ("a field an open report does not have", MARKET, ["open"], {"report": "open", "price": 99.0},
     "cannot be corrected on an open report"),
    ("a fill price worse than the limit", {}, ["filled"], {"report": "filled", "price": 100.5},
     "worse than its limit"),
    ("the limit corrected under a fill", {}, ["fill 99.75"], {"entry": 99.5},
     "worse than its limit 99.5"),
    ("a fill moved past the exit", {}, ["filled", "hand exit"], {"report": "filled", "at": T4},
     "before its fill"),
    ("a stop exit's price past the stop", MARKET, ["stop exit"],
     {"report": "exited", "price": 99.0}, "better than its stop"),
    ("an exit moved onto the entry", MARKET, ["stop exit"], {"report": "exited", "at": T1},
     f"entered at {T1.isoformat()}"),
    ("an open report moved onto the exit", MARKET, ["open", "stop exit"],
     {"report": "open", "at": T3}, "and was reported open at"),
    ("the latest open report moved before the entry", MARKET, ["open", "open at T3"],
     {"report": "open", "at": NOW}, f"opened at {T1.isoformat()}, so it cannot be reported open"),
    ("a fill moved past an older open report", {}, ["filled", "open", "open at T3"],
     {"report": "filled", "at": T3}, f"opened at {T3.isoformat()}, so it cannot be reported open"),
    ("a market entry moved past an open report", MARKET, ["open"], {"at": T3},
     f"opened at {T3.isoformat()}, so it cannot be reported open"),
    ("a report moved after now", {}, ["filled"],
     {"report": "filled", "at": LATER + timedelta(hours=1)}, "after now"),
    ("a move and a report at once", {}, ["filled"], {"move": 0, "report": "filled", "price": 99.0},
     "one at a time"),
    ("a report that is not known", {}, [], {"report": "exit", "price": 99.0}, "report"),
    ("an exit kind that is not known", MARKET, ["stop exit"],
     {"report": "exited", "kind": "trailing"}, "kind"),
    ("a report price that is not a number", {}, ["filled"], {"report": "filled", "price": "99"},
     "price"),
    ("a cancellation's cause that is not known", {}, ["cancelled"], {"report": "cancelled",
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
    v = api.void_trade(con, t.id, reason="It never filled.", report="filled", now=LATER)
    assert (v.decisions.filled, v.decisions.voided) == (None, False)
    named = [e["id"] for e in store.events(con, trade_id=t.id) if e["kind"] == "filled"][0]
    assert last(con, t.id) == ("voided", LATER.isoformat(), named, {"reason": "It never filled."})
    assert fill(con, t.id, at=T3).decisions.filled.at == T3                 # reported again
    assert api.move_order(con, t.id, stop=99.0, reason="x", at=T3, now=LATER).decisions.moves


def test_a_withdrawn_exit_lets_the_trade_exit_again(con):
    t = enter(con, entry_type="market")
    out(con, t.id, kind="stop")
    api.void_trade(con, t.id, reason="Not stopped.", report="exited", now=LATER)
    d = out(con, t.id, price=101.0, at=T4).decisions
    assert (d.exited.kind, d.exited.price, d.exited.at) == ("hand", 101.0, T4)


def test_withdrawing_the_latest_open_report_leaves_the_one_before_it(con):
    t = enter(con, entry_type="market")
    opened(con, t.id, at=T3)
    opened(con, t.id)                                    # at T2, logged later
    v = api.void_trade(con, t.id, reason="Not then.", report="open", now=LATER)
    assert v.decisions.open.at == T2


WITHDRAWALS = [  # (id, entry kwargs, setups, report, a substring of the refusal)
    ("a fill under an exit", {}, ["filled", "hand exit"], "filled",
     "withdraw the exit before the fill"),
    ("a fill under an open report", {}, ["filled", "open at T3"], "filled",
     "report the fill first"),
    ("a report that was not made", {}, [], "cancelled", "no cancellation to withdraw"),
    ("an open report that was not made", MARKET, [], "open", "no open report to withdraw"),
    ("a report that is not known", {}, [], "exit", "report"),
    ("a report on a void trade", {}, ["filled", "void"], "filled", "is void"),
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


LIVE = "dip-buyer-r1"


def test_run_stopped_is_reported_at_the_stop_and_left_out_of_the_fill_rate(con):
    a, b = enter(con, LIVE), enter(con, LIVE, entry=99.0)
    fill(con, a.id)                                                    # at T2
    api.stop_run(con, LIVE, at=T3, now=LATER)
    o = cancel(con, b.id, why="run stopped", at=T3, reason="The run stopped.").outcome
    assert (o.status, o.cancelled_why, o.cancelled_at) == ("cancelled", "run stopped", T3)
    limits = api.stats(con, run=LIVE, now=LATER).limits
    # with why "not filled" the rate would read 0.5: b would count as a miss
    assert limits["cancelled"] == {"run stopped": (b.id,)} and limits["rate"] == 1.0


def test_run_stopped_is_the_stops_instant_in_any_offset(con):
    t = enter(con, LIVE)
    api.stop_run(con, LIVE, at=T3, now=LATER)                          # 15:00Z
    o = cancel(con, t.id, why="run stopped", at="2026-03-02T10:00:00-05:00").outcome
    assert o.cancelled_at == T3


@pytest.mark.parametrize("stop, at, says", [
    (False, T3, "has not stopped"),
    (True, T3 - timedelta(seconds=1), "2026-03-02T15:00:00+00:00"),
    (True, T2, "2026-03-02T15:00:00+00:00"),
    (True, T4, "2026-03-02T15:00:00+00:00"),   # after the stop: this rule or the existing one
])
def test_run_stopped_off_the_stop_or_on_a_running_run_is_refused(con, stop, at, says):
    t = enter(con, LIVE)
    if stop:
        api.stop_run(con, LIVE, at=T3, now=LATER)
    before = len(store.events(con))
    with pytest.raises(api.Refused, match=re.escape(says)):
        cancel(con, t.id, why="run stopped", at=at)
    assert len(store.events(con)) == before


def test_a_cancellation_at_the_stop_can_be_corrected_to_run_stopped(con):
    t = enter(con, LIVE)
    api.stop_run(con, LIVE, at=T3, now=LATER)
    cancel(con, t.id, why="not filled", at=T3)
    o = api.correct_trade(con, t.id, fields={"report": "cancelled", "why": "run stopped"},
                          reason="The stop ended it.", now=LATER).outcome
    assert o.cancelled_why == "run stopped"


@pytest.mark.parametrize("word", ['fill', 'cancel', 'close'])
def test_the_words_a_report_no_longer_uses_are_refused_as_unknown(con, word):
    t = enter(con, **MARKET)
    out(con, t.id, price=101.0)
    with pytest.raises(api.Refused, match="is not known"):
        api.correct_trade(con, t.id, fields={"report": word, "price": 102.0}, reason="x",
                          now=LATER)
    with pytest.raises(api.Refused, match="is not known"):
        api.void_trade(con, t.id, report=word, reason="x", now=LATER)
