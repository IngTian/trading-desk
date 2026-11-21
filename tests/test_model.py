"""Tests for the fold: a trade's decisions, corrections read in place. Every figure is invented.

The events are written straight to the store, so the fold is tested on shapes the API never
writes too (a correction naming a correction).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from desk import model, store

T0 = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
FAR = datetime(2030, 1, 1, tzinfo=UTC)  # a record-keeping event's own `at`, which the fold ignores
ENTERED = {"entry_type": "limit", "entry": 100.0, "stop": 98.5, "target": 103.0, "size": 10,
           "reasons": "Dip into support.", "screenshots": [{"link": "https://example.com/c.png"}]}


@pytest.fixture
def con(tmp_path):
    db = tmp_path / "desk.db"
    store.create(db)
    with store.open(db) as c:
        store.add_strategy(c, "dip-buyer", T0.isoformat())
        store.add_version(c, "dip-buyer", 1, "text", "hash", {}, ["1h"], [], T0.isoformat())
        store.add_run(c, "dip-buyer-r1", "dip-buyer", 1, "replay", [], "USD")
        store.add_trade(c, "gc-1", "dip-buyer-r1", "GC", "long", "future", 100.0, "USD")
        yield c


def log(con, kind, at, payload, corrects=None) -> int:
    return store.append(con, kind=kind, run_id="dip-buyer-r1", trade_id="gc-1",
                        at=at.isoformat(), payload=payload, corrects=corrects,
                        recorded_at=FAR.isoformat())


def test_an_entered_trade_is_its_entry_and_identity_row(con):
    log(con, "entered", T0, ENTERED)
    d = model.decisions_of(con, "gc-1")
    assert d.entry == model.Entry(type="limit", price=100.0, stop=98.5, target=103.0, size=10,
                                  at=T0)
    assert (d.moves, d.close_at, d.close_reason, d.voided) == ((), None, None, False)
    assert d.reasons == "Dip into support."
    assert d.screenshots == ({"link": "https://example.com/c.png"},)
    assert (d.point_value, d.currency) == (100.0, "USD")


def test_moves_recorded_out_of_order_are_read_in_at_order(con):
    log(con, "entered", T0, ENTERED)
    log(con, "order_moved", T0 + timedelta(hours=3), {"stop": 100.0, "reason": "Breakeven."})
    log(con, "order_moved", T0 + timedelta(hours=1), {"target": 104.0, "reason": "Room."})
    log(con, "order_moved", T0 + timedelta(hours=1), {"stop": 99.0, "reason": "Same bar."})
    moves = model.decisions_of(con, "gc-1").moves
    assert moves == (
        model.Move(at=T0 + timedelta(hours=1), stop=None, target=104.0, reason="Room."),
        model.Move(at=T0 + timedelta(hours=1), stop=99.0, target=None, reason="Same bar."),
        model.Move(at=T0 + timedelta(hours=3), stop=100.0, target=None, reason="Breakeven."))


def test_a_close_and_a_void_are_read(con):
    log(con, "entered", T0, ENTERED)
    log(con, "closed", T0 + timedelta(hours=2), {"reason": "Out before the report."})
    log(con, "voided", FAR, {"reason": "Wrong run."})
    d = model.decisions_of(con, "gc-1")
    assert (d.close_at, d.close_reason) == (T0 + timedelta(hours=2), "Out before the report.")
    assert d.voided


def test_a_correction_of_the_entry_stop_is_read_in_place(con):
    entered = log(con, "entered", T0, ENTERED)
    log(con, "corrected", FAR, {"fields": {"stop": 97.5}, "reason": "Typo."}, corrects=entered)
    d = model.decisions_of(con, "gc-1")
    assert d.entry == model.Entry(type="limit", price=100.0, stop=97.5, target=103.0, size=10,
                                  at=T0)
    assert d.reasons == "Dip into support."  # only the named field changes


def test_a_correction_of_a_correction_resolves_to_the_last_link(con):
    entered = log(con, "entered", T0, ENTERED)
    first = log(con, "corrected", FAR, {"fields": {"stop": 97.5, "size": 12}, "reason": "Typo."},
                corrects=entered)
    log(con, "corrected", FAR, {"fields": {"stop": 97.0}, "reason": "Typo again."},
        corrects=first)
    e = model.decisions_of(con, "gc-1").entry
    assert (e.stop, e.size, e.price, e.at) == (97.0, 12, 100.0, T0)


def test_of_two_corrections_naming_one_event_the_later_wins_field_by_field(con):
    entered = log(con, "entered", T0, ENTERED)
    log(con, "corrected", FAR, {"fields": {"stop": 97.5, "target": 104.0}, "reason": "a"},
        corrects=entered)
    log(con, "corrected", FAR, {"fields": {"stop": 97.0}, "reason": "b"}, corrects=entered)
    e = model.decisions_of(con, "gc-1").entry
    assert (e.stop, e.target) == (97.0, 104.0)


def test_a_corrected_entry_at_point_value_and_currency_are_read_in_place(con):
    entered = log(con, "entered", T0, ENTERED)
    later = T0 + timedelta(minutes=15)
    log(con, "corrected", FAR, {"fields": {"at": later.isoformat(), "point_value": 50.0,
                                           "currency": "CAD"}, "reason": "Wrong contract."},
        corrects=entered)
    d = model.decisions_of(con, "gc-1")
    assert (d.entry.at, d.point_value, d.currency) == (later, 50.0, "CAD")


def test_a_corrected_move_at_reorders_the_moves(con):
    log(con, "entered", T0, ENTERED)
    early = log(con, "order_moved", T0 + timedelta(hours=1), {"stop": 99.0, "reason": "a"})
    log(con, "order_moved", T0 + timedelta(hours=2), {"stop": 99.5, "reason": "b"})
    log(con, "corrected", FAR, {"fields": {"at": (T0 + timedelta(hours=3)).isoformat(),
                                           "target": 104.0}, "reason": "Wrong bar."},
        corrects=early)
    moves = model.decisions_of(con, "gc-1").moves
    assert [(m.reason, m.at, m.stop, m.target) for m in moves] == [
        ("b", T0 + timedelta(hours=2), 99.5, None),
        ("a", T0 + timedelta(hours=3), 99.0, 104.0)]


def test_a_correction_is_not_itself_a_decision(con):
    entered = log(con, "entered", T0, ENTERED)
    log(con, "corrected", FAR, {"fields": {"size": 5}, "reason": "Half size."}, corrects=entered)
    d = model.decisions_of(con, "gc-1")
    assert d.moves == () and d.close_at is None and not d.voided and d.entry.at == T0


def test_fold_names_the_events_a_correction_can_name(con):
    entered = log(con, "entered", T0, ENTERED)
    late = log(con, "order_moved", T0 + timedelta(hours=2), {"stop": 99.5, "reason": "b"})
    early = log(con, "order_moved", T0 + timedelta(hours=1), {"stop": 99.0, "reason": "a"})
    filled = log(con, "filled", T0 + timedelta(hours=1), {"reason": "a"})
    held = log(con, "held", T0 + timedelta(hours=2), {"reason": "c"})
    closed = log(con, "closed", T0 + timedelta(hours=3), {"reason": "b", "kind": "stop"})
    d, entered_id, move_ids, reports = model._fold(con, "gc-1")
    assert d == model.decisions_of(con, "gc-1")
    assert (entered_id, move_ids, reports) == (
        entered, (early, late), {"fill": filled, "open": held, "close": closed})


def test_reports_are_read_with_the_close_kind_and_price(con):
    log(con, "entered", T0, ENTERED)
    log(con, "filled", T0 + timedelta(hours=1), {"price": 99.5, "reason": "Filled."})
    log(con, "closed", T0 + timedelta(hours=3), {"reason": "Stopped.", "kind": "stop"})
    d = model.decisions_of(con, "gc-1")
    assert d.fill == model.Report(at=T0 + timedelta(hours=1), price=99.5, reason="Filled.")
    assert (d.cancel, d.close_kind, d.close_price) == (None, "stop", None)


def test_a_close_that_names_no_kind_is_a_hand_close(con):
    log(con, "entered", T0, ENTERED)
    log(con, "closed", T0 + timedelta(hours=3), {"reason": "Out.", "price": 101.0})
    d = model.decisions_of(con, "gc-1")
    assert (d.close_kind, d.close_price) == ("hand", 101.0)


def test_a_withdrawn_report_is_not_read_and_the_trade_is_not_void(con):
    log(con, "entered", T0, ENTERED)
    wrong = log(con, "filled", T0 + timedelta(hours=1), {"reason": "Wrong."})
    log(con, "voided", FAR, {"reason": "It never filled."}, corrects=wrong)
    d = model.decisions_of(con, "gc-1")
    assert (d.fill, d.voided) == (None, False)
    log(con, "cancelled", T0 + timedelta(hours=2), {"reason": "Pulled.", "why": "pulled"})
    assert model.decisions_of(con, "gc-1").cancel == model.Report(
        at=T0 + timedelta(hours=2), price=None, reason="Pulled.", why="pulled")


def test_a_withdrawal_naming_anything_but_a_report_is_not_read(con):
    entered = log(con, "entered", T0, ENTERED)
    log(con, "voided", FAR, {"reason": "x"}, corrects=entered)
    d = model.decisions_of(con, "gc-1")
    assert d.entry.price == 100.0 and not d.voided


def test_a_correction_of_a_report_is_read_in_place(con):
    log(con, "entered", T0, ENTERED)
    f = log(con, "filled", T0 + timedelta(hours=1), {"price": 99.5, "reason": "Filled."})
    log(con, "corrected", FAR, {"fields": {"price": 99.25,
                                           "at": (T0 + timedelta(hours=2)).isoformat()},
                                "reason": "Typo."}, corrects=f)
    assert model.decisions_of(con, "gc-1").fill == model.Report(
        at=T0 + timedelta(hours=2), price=99.25, reason="Filled.")


def test_the_open_reports_read_are_the_latest_and_the_earliest_standing(con):
    log(con, "entered", T0, ENTERED)
    log(con, "filled", T0 + timedelta(hours=1), {"reason": "Filled."})
    log(con, "held", T0 + timedelta(hours=3), {"reason": "Still in."})
    log(con, "held", T0 + timedelta(hours=2), {"reason": "Logged later, dated earlier."})
    d = model.decisions_of(con, "gc-1")
    assert d.held == model.Report(at=T0 + timedelta(hours=3), price=None, reason="Still in.")
    assert d.held_first == model.Report(at=T0 + timedelta(hours=2), price=None,
                                        reason="Logged later, dated earlier.")


def test_in_force_reads_the_moves_dated_at_or_before(con):
    log(con, "entered", T0, ENTERED)
    log(con, "order_moved", T0 + timedelta(hours=1), {"stop": 99.0, "reason": "a"})
    log(con, "order_moved", T0 + timedelta(hours=2), {"target": 104.0, "reason": "b"})
    d = model.decisions_of(con, "gc-1")
    assert [model.in_force(d, T0 + timedelta(hours=n)) for n in (0.5, 1, 3)] == [
        (98.5, 103.0), (99.0, 103.0), (99.0, 104.0)]


def test_the_plan_1_fold_is_gone():
    for name in ("Fill", "TradeState", "state_of", "open_trades", "stopped_closes",
                 "realised_r_on", "_states", "_moment", "_per_unit_r"):
        assert not hasattr(model, name), name
    assert all(hasattr(model, n) for n in ("Plan", "_signed", "canonical_ticker"))


# --- model.report_problem, rule by rule --------------------------------------------------------

P = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)


def t(n):
    return P + timedelta(hours=n)


def dec(type="limit", price=100.0, stop=98.0, target=104.0, moves=(), fill=None, cancel=None,
        why="pulled", held=None, held_first=None, close=None, kind=None, close_price=None):
    """A long entered at t(1): fill=(n, price or None), cancel=n with its why, held=n the latest
    open report and held_first=n the earliest (default: held), close=n with its kind and price."""
    first = held if held_first is None else held_first
    return model.Decisions(
        entry=model.Entry(type=type, price=price, stop=stop, target=target, size=10, at=t(1)),
        moves=tuple(model.Move(at=t(a), stop=s, target=g, reason="r") for a, s, g in moves),
        close_at=None if close is None else t(close),
        close_reason=None if close is None else "r",
        voided=False, reasons="r", screenshots=(), point_value=1.0, currency="USD",
        fill=None if fill is None else model.Report(at=t(fill[0]), price=fill[1], reason="r"),
        cancel=None if cancel is None else model.Report(at=t(cancel), price=None, reason="r",
                                                        why=why),
        held=None if held is None else model.Report(at=t(held), price=None, reason="r"),
        held_first=None if first is None else model.Report(at=t(first), price=None, reason="r"),
        close_kind=None if close is None else (kind or "hand"), close_price=close_price)


MARKET = dict(type="market")
PROBLEMS = [  # (id, dec kwargs, report_problem kwargs, a substring of the problem, or None)
    ("a consistent record has none", dict(fill=(2, 99.5), close=3, kind="stop"), {}, None),
    ("a market entry has no fill to report", dict(MARKET, fill=(2, None)), {}, "market entry"),
    ("nor a cancellation", dict(MARKET, cancel=2), {}, "market entry"),
    ("an order cannot both fill and be cancelled", dict(fill=(2, None), cancel=3), {},
     "cannot both fill"),
    ("a fill is not before the entry", dict(fill=(0.5, None)), {},
     f"entered at {t(1).isoformat()}"),
    ("a fill at the entry's moment is accepted", dict(fill=(1, None)), {}, None),
    ("a fill is not after the run stop", dict(fill=(3, None)), dict(stopped=t(2)),
     "after its run stopped"),
    ("a fill at the run stop is accepted", dict(fill=(2, None)), dict(stopped=t(2)), None),
    ("a fill is not worse than its limit", dict(fill=(2, 100.5)), {}, "worse than its limit 100.0"),
    ("a fill better than its limit is accepted", dict(fill=(2, 99.0)), {}, None),
    ("a fill at the stop is the broker's fact: accepted, its R withheld", dict(fill=(2, 98.0)), {},
     None),
    ("a fill beyond the stop is accepted too", dict(fill=(2, 97.5)), {}, None),
    ("an exit kind that is not known", dict(MARKET, close=3, kind="trailing"), {},
     "kind 'trailing'"),
    ("a cancellation's cause that is not known", dict(cancel=2, why="expired"), {},
     "why 'expired'"),
    ("a cancellation comes after the entry", dict(cancel=1), {}, f"entered at {t(1).isoformat()}"),
    ("and after every move", dict(moves=((3, 99.0, None),), cancel=2), {},
     f"has a move at {t(3).isoformat()}"),
    ("a cancellation is not after the run stop", dict(cancel=3), dict(stopped=t(2)),
     "which had already cancelled it"),
    ("a cancellation at the run stop is accepted", dict(cancel=2), dict(stopped=t(2)), None),
    ("a cancelled order has no position to close", dict(cancel=2, close=3), {},
     "no position to close"),
    ("an exit on a limit needs its fill reported", dict(close=3, kind="stop"), {},
     "no fill reported"),
    ("a priced hand close is an exit too", dict(close=3, close_price=101.0), {},
     "no fill reported"),
    ("a live hand close of a pending limit is the judge's", dict(close=3), dict(mode="live"), None),
    ("a fill comes at or before the close", dict(fill=(3, None), close=2, close_price=101.0), {},
     "before its fill"),
    ("a replay hand close of a position needs a price", dict(MARKET, close=3), {}, "replay run"),
    ("a fill under a replay hand close names the way out", dict(fill=(2, None), close=3), {},
     "withdraw that close"),
    ("a replay hand close of a pending limit cancels it", dict(close=3), {}, None),
    ("a stop exit is not better than the stop",
     dict(MARKET, close=3, kind="stop", close_price=98.5), {}, "better than its stop 98.0"),
    ("a stop exit beyond the stop is accepted",
     dict(MARKET, close=3, kind="stop", close_price=97.5), {}, None),
    ("the stop in force is the last moved",
     dict(MARKET, moves=((2, 99.0, None),), close=3, kind="stop", close_price=99.5), {},
     "better than its stop 99.0"),
    ("a target exit is not worse than the target",
     dict(MARKET, close=3, kind="target", close_price=103.5), {}, "worse than its target 104.0"),
    ("a target exit beyond the target is accepted",
     dict(MARKET, close=3, kind="target", close_price=104.5), {}, None),
    ("short: a target exit below the target is accepted",
     dict(MARKET, stop=102.0, target=96.0, close=3, kind="target", close_price=95.5),
     dict(side="short"), None),
    ("a stop exit with no price on a fill beyond the stop needs its price",
     dict(fill=(2, 97.5), close=3, kind="stop"), {}, "fired at once"),
    ("a stop exit priced on a fill beyond the stop is accepted",
     dict(fill=(2, 97.5), close=3, kind="stop", close_price=97.25), {}, None),
    ("a stop exit needs a stop", dict(MARKET, stop=None, close=3, kind="stop"), {},
     "no stop in force"),
    ("short: a stop exit below the stop is better than it",
     dict(MARKET, stop=102.0, target=96.0, close=3, kind="stop", close_price=101.5),
     dict(side="short"), "better than its stop 102.0"),
    ("short: a fill below the limit is worse than it",
     dict(fill=(2, 99.5), stop=102.0, target=96.0), dict(side="short"),
     "worse than its limit 100.0"),
    ("an open report on a position is accepted", dict(MARKET, held=2), {}, None),
    ("an open report on a limit with no fill reported", dict(held=2), {}, "report the fill first"),
    ("an open report on a cancelled order", dict(cancel=2, held=3), {},
     "withdraw the cancellation"),
    ("an open report after the fill is accepted", dict(fill=(2, None), held=3), {}, None),
    ("an open report before the fill", dict(fill=(3, None), held=2), {},
     f"position opened at {t(3).isoformat()}"),
    ("an older open report before the fill", dict(fill=(3, None), held=4, held_first=2), {},
     f"position opened at {t(3).isoformat()}"),
    ("an open report before the close is accepted", dict(MARKET, held=2, close=3, kind="stop"), {},
     None),
    ("an open report at the close", dict(MARKET, held=3, close=3, kind="stop"), {},
     "and reported open at"),
    ("an open report after a live close by hand", dict(MARKET, held=4, close=3), dict(mode="live"),
     "and reported open at"),
]


@pytest.mark.parametrize(("d", "kw", "says"), [c[1:] for c in PROBLEMS],
                         ids=[c[0] for c in PROBLEMS])
def test_report_problems(d, kw, says):
    p = model.report_problem("acme-1", dec(**d),
                             **{"side": "long", "mode": "replay", "stopped": None, **kw})
    assert (p is None) if says is None else (p is not None and says in p), p
