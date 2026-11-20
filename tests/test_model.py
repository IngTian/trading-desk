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
        store.add_version(c, "dip-buyer", 1, "text", "hash", {}, ["1h"], T0.isoformat())
        store.add_run(c, "dip-buyer-r1", "dip-buyer", 1, "replay", [])
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
    d, entered_id, move_ids = model._fold(con, "gc-1")
    assert d == model.decisions_of(con, "gc-1")
    assert (entered_id, move_ids) == (entered, (early, late))


def test_the_plan_1_fold_is_gone():
    for name in ("Fill", "TradeState", "state_of", "open_trades", "stopped_closes",
                 "realised_r_on", "_states", "_moment", "_per_unit_r"):
        assert not hasattr(model, name), name
    assert all(hasattr(model, n) for n in ("Plan", "_signed", "canonical_ticker"))
