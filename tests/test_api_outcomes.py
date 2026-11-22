"""Every trade read carries its outcome, read from its record alone, on a live run as on a
replay one. Every figure is invented."""
import re
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from desk import api, store

B = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
LATER = B + timedelta(hours=24)
GOLD = "---\nname: Gold\n---\n\n# Gold\n\n```rules\ntimeframes = [\"1h\", \"4h\"]\n```\n"
BASE = dict(ticker="ACME", side="long", entry_type="market", entry=100.0, stop=98.0, target=104.0,
            size=10, currency="USD", instrument="stock", reasons="Invented.")


def h(n):
    return B + timedelta(hours=n)


@pytest.fixture
def con(tmp_path):
    store.create(tmp_path / "desk.db")
    with store.open(tmp_path / "desk.db") as c:
        api.save_strategy(c, "gold", GOLD, now=h(0))
        api.start_run(c, "gold", mode="replay", currency="USD", at=h(0), now=h(0))   # gold-r1
        api.start_run(c, "gold", mode="live", currency="USD", at=h(0), now=h(0))     # gold-r2
        yield c


def enter(con, run, **kw):
    return api.trade(con, run, **{**BASE, **kw})


def test_a_replay_trade_reads_its_reports(con):
    t = enter(con, "gold-r1", at=h(1), now=LATER)
    assert (t.outcome.status, t.outcome.fill, t.outcome.filled_at) == ("open", 100.0, h(1))
    o = api.report(con, t.id, state="exited", kind="stop", reason="Stopped.", at=h(3),
                   now=LATER).outcome
    assert (o.status, o.exit, o.exited_at, o.exit_reason, o.r, o.cash) == (
        "exited", 98.0, h(3), "stop", -1.0, -20.0)
    assert api.get_trade(con, t.id).outcome == o


def test_a_replay_limit_is_pending_until_a_fill_or_a_cancellation_is_reported(con):
    a = enter(con, "gold-r1", entry_type="limit", at=h(1), now=LATER)
    b = enter(con, "gold-r1", entry_type="limit", at=h(1), now=LATER)
    assert a.outcome.status == "pending"
    f = api.report(con, a.id, state="filled", reason="Filled.", at=h(2), price=99.5,
                   now=LATER).outcome
    assert (f.status, f.fill, f.filled_at) == ("open", 99.5, h(2))
    c = api.report(con, b.id, state="cancelled", reason="Never reached.", why="not filled",
                   at=h(3), now=LATER).outcome
    assert (c.status, c.cancelled_at, c.cancelled_why) == ("cancelled", h(3), "not filled")


def test_a_replay_run_stop_cancels_the_limits_still_pending(con):
    t = enter(con, "gold-r1", entry_type="limit", at=h(1), now=LATER)
    api.stop_run(con, "gold-r1", at=h(3), now=LATER)
    o = api.get_trade(con, t.id).outcome
    assert (o.status, o.cancelled_at, o.cancelled_why) == ("cancelled", h(3), "run stopped")


def test_a_live_market_trade_reads_open_until_an_exit_is_reported(con):
    t = enter(con, "gold-r2", at=h(1), now=h(1))
    o = t.outcome
    assert (o.status, o.fill, o.filled_at, o.exit) == ("open", 100.0, h(1), None)
    assert api.get_trade(con, t.id, now=h(5)).outcome == o   # a read changes nothing
    x = api.report(con, t.id, state="exited", kind="stop", reason="Stopped.", at=h(3),
                   now=h(5)).outcome
    assert (x.status, x.exit, x.exited_at, x.exit_reason, x.r, x.cash) == (
        "exited", 98.0, h(3), "stop", -1.0, -20.0)


def test_a_live_limit_is_pending_until_a_fill_or_a_cancellation_is_reported(con):
    a = enter(con, "gold-r2", entry_type="limit", at=h(1), now=h(1))
    b = enter(con, "gold-r2", entry_type="limit", at=h(1), now=h(1))
    c = enter(con, "gold-r2", entry_type="limit", at=h(1), now=h(1))
    assert [t.outcome.status for t in api.list_trades(con, run="gold-r2", now=h(1))] == [
        "pending"] * 3
    f = api.report(con, a.id, state="filled", reason="Filled.", at=h(2), price=99.5,
                   now=h(2)).outcome
    assert (f.status, f.fill, f.filled_at) == ("open", 99.5, h(2))
    x = api.report(con, b.id, state="cancelled", reason="Never reached.", why="not filled",
                   at=h(3), now=h(3)).outcome
    assert (x.status, x.cancelled_at, x.cancelled_why) == ("cancelled", h(3), "not filled")
    assert api.get_trade(con, c.id, now=h(3)).outcome.status == "pending"   # nothing reported
    api.stop_run(con, "gold-r2", at=h(4), now=h(4))
    o = api.get_trade(con, c.id, now=h(4)).outcome
    assert (o.status, o.cancelled_at, o.cancelled_why) == ("cancelled", h(4), "run stopped")


def test_limit_orders_placed_filled_and_cancelled_are_countable(con):
    a, b, c, d = (enter(con, "gold-r1", entry_type="limit", at=h(1), now=LATER) for _ in range(4))
    enter(con, "gold-r1", at=h(1), now=LATER)                     # a market entry: no limit order
    api.report(con, a.id, state="filled", reason="Filled.", at=h(2), now=LATER)
    api.report(con, b.id, state="cancelled", reason="Never reached.", why="not filled", at=h(2),
               now=LATER)
    api.report(con, c.id, state="cancelled", reason="Out.", why="pulled", at=h(2),
               now=LATER)                          # still pending: pulled
    api.stop_run(con, "gold-r1", at=h(3), now=LATER)               # d: cancelled by the run stop
    limits = [t for t in api.list_trades(con, run="gold-r1") if t.decisions.entry.type == "limit"]
    assert [t.id for t in limits] == [a.id, b.id, c.id, d.id]
    assert Counter(t.outcome.status for t in limits) == {"open": 1, "cancelled": 3}
    assert [t.outcome.cancelled_why for t in limits] == [
        None, "not filled", "pulled", "run stopped"]


def test_list_trades_filters_open_trades(con):
    done = enter(con, "gold-r1", at=h(1), now=LATER)
    api.report(con, done.id, state="exited", kind="target", reason="Target.", at=h(3), now=LATER)
    held = enter(con, "gold-r1", at=h(1), now=LATER)
    waiting = enter(con, "gold-r1", entry_type="limit", at=h(1), now=LATER)
    pulled = enter(con, "gold-r1", entry_type="limit", at=h(1), now=LATER)
    api.report(con, pulled.id, state="cancelled", reason="Pulled.", why="pulled", at=h(2),
               now=LATER)
    gone = enter(con, "gold-r1", at=h(1), now=LATER)
    api.void_trade(con, gone.id, reason="Duplicate.", now=LATER)
    ids = lambda **kw: [t.id for t in api.list_trades(con, run="gold-r1", **kw)]  # noqa: E731
    assert ids(open=True) == [held.id, waiting.id]
    assert ids(open=False) == [done.id, pulled.id, gone.id]


def test_a_void_keeps_its_outcome_beside_the_void(con):
    t = enter(con, "gold-r1", at=h(1), now=LATER)
    v = api.void_trade(con, t.id, reason="Wrong run.", now=LATER)
    assert v.decisions.voided and v.outcome == t.outcome


def test_a_replay_open_report_is_kept_and_changes_no_outcome(con):
    t = enter(con, "gold-r1", at=h(1), now=LATER)
    v = api.report(con, t.id, state="open", reason="Still in.", at=h(2), now=LATER)
    assert v.outcome == t.outcome and v.decisions.held.at == h(2)


@pytest.mark.parametrize(("call", "says"), [
    (lambda con, t: api.get_trade(con, t, now=datetime(2026, 3, 2)), "ISO-8601 time with its UTC"),
    (lambda con, t: api.get_trade(con, 5), "trade"),
    (lambda con, t: api.list_trades(con, open="yes"), "open"),
    (lambda con, t: api.list_trades(con, run=5), "run")])
def test_reads_refuse_a_naive_now_and_arguments_of_the_wrong_type(con, call, says):
    t = enter(con, "gold-r1", at=h(1), now=LATER)
    with pytest.raises(api.Refused, match=says):
        call(con, t.id)


@pytest.mark.parametrize("run", ["gold-r1", "gold-r2"], ids=["replay", "live"])
def test_a_move_on_a_limit_the_run_stop_cancelled_is_refused(con, run):
    t = enter(con, run, entry_type="limit", entry=90.0, stop=88.0, target=94.0, at=h(1), now=h(1))
    api.stop_run(con, run, at=h(2), now=h(4))
    with pytest.raises(api.Refused, match=re.escape(f"cancelled at {h(2).isoformat()}")):
        api.move_order(con, t.id, stop=89.0, reason="Tighter.", at=h(3), now=h(4))
    assert api.move_order(con, t.id, stop=89.0, reason="Tighter.", at=h(1.5),
                          now=h(4)).outcome.status == "cancelled"


def test_a_move_at_the_moment_the_run_stop_cancelled_the_limit_is_refused(con):
    t = enter(con, "gold-r1", entry_type="limit", at=h(1), now=LATER)
    api.stop_run(con, "gold-r1", at=h(2), now=LATER)
    with pytest.raises(api.Refused, match="no order to move"):
        api.move_order(con, t.id, stop=97.0, reason="Tighter.", at=h(2), now=LATER)
