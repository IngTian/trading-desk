"""Every trade read carries its outcome: a replay trade's from its reports alone, a live trade's
judged from Yahoo bars (a stub here) with the reports laid over it. Every figure is invented."""
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from desk import api, bars, config, judge, store, yahoo

B = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
LATER = B + timedelta(hours=24)
GOLD = "---\nname: Gold\n---\n\n# Gold\n\n```rules\ntimeframes = [\"1h\", \"4h\"]\n```\n"
BASE = dict(ticker="ACME", side="long", entry_type="market", entry=100.0, stop=98.0, target=104.0,
            size=10, currency="USD", instrument="stock", reasons="Invented.")


def h(n):
    return B + timedelta(hours=n)


def m(n):
    """Minute n: a live test's clock, so its 1-minute bars keep the order of its decisions."""
    return B + timedelta(minutes=n)


def minute(n, o, hi, lo, c):
    return judge.Bar(h(0) + timedelta(minutes=n), o, hi, lo, c, None)


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


STOPPED = [minute(0, 100, 101, 99, 100.5), minute(1, 100, 101, 97.5, 99),
           minute(2, 99, 100, 98.5, 99.5),
           minute(3, 99.5, 100, 99, 99.5)]   # a market long at m(1) exits at the stop, 98, at m(2)


def test_a_replay_trade_reads_its_reports(con):
    t = enter(con, "gold-r1", at=h(1), now=LATER)
    assert (t.outcome.status, t.outcome.fill, t.outcome.filled_at, t.judged, t.differs) == (
        "open", 100.0, h(1), None, ())
    o = api.report(con, t.id, state="exited", kind="stop", reason="Stopped.", at=h(3),
                   now=LATER).outcome
    assert (o.status, o.exit, o.exited_at, o.exit_reason, o.r, o.cash, o.reported) == (
        "exited", 98.0, h(3), "stop", -1.0, -20.0, ("close",))
    assert api.get_trade(con, t.id).outcome == o


def test_a_replay_limit_is_pending_until_a_fill_or_a_cancellation_is_reported(con):
    a = enter(con, "gold-r1", entry_type="limit", at=h(1), now=LATER)
    b = enter(con, "gold-r1", entry_type="limit", at=h(1), now=LATER)
    assert a.outcome.status == "pending"
    f = api.report(con, a.id, state="filled", reason="Filled.", at=h(2), price=99.5,
                   now=LATER).outcome
    assert (f.status, f.fill, f.filled_at, f.reported) == ("open", 99.5, h(2), ("fill",))
    c = api.report(con, b.id, state="cancelled", reason="Never reached.", why="not filled",
                   at=h(3), now=LATER).outcome
    assert (c.status, c.cancelled_at, c.cancelled_why, c.reported) == (
        "cancelled", h(3), "not filled", ("cancel",))


def test_a_replay_run_stop_cancels_the_limits_still_pending(con):
    t = enter(con, "gold-r1", entry_type="limit", at=h(1), now=LATER)
    api.stop_run(con, "gold-r1", at=h(3), now=LATER)
    o = api.get_trade(con, t.id).outcome
    assert (o.status, o.cancelled_at, o.cancelled_why, o.reported) == (
        "cancelled", h(3), "run stopped", ())


def test_a_replay_read_opens_no_bars(con, monkeypatch):
    def nope(*a, **k):
        raise AssertionError("a replay read opened bars.db")
    monkeypatch.setattr(bars, "open", nope)
    t = enter(con, "gold-r1", at=h(1), now=LATER)
    assert api.get_trade(con, t.id).outcome.status == "open"


def test_limit_orders_placed_filled_and_cancelled_are_countable(con):
    a, b, c, d = (enter(con, "gold-r1", entry_type="limit", at=h(1), now=LATER) for _ in range(4))
    enter(con, "gold-r1", at=h(1), now=LATER)                     # a market entry: no limit order
    api.report(con, a.id, state="filled", reason="Filled.", at=h(2), now=LATER)
    api.report(con, b.id, state="cancelled", reason="Never reached.", why="not filled", at=h(2),
               now=LATER)
    api.close_trade(con, c.id, reason="Out.", at=h(2), now=LATER)  # still pending: pulled
    api.stop_run(con, "gold-r1", at=h(3), now=LATER)               # d: cancelled by the run stop
    limits = [t for t in api.list_trades(con, run="gold-r1") if t.decisions.entry.type == "limit"]
    assert [t.id for t in limits] == [a.id, b.id, c.id, d.id]
    assert Counter(t.outcome.status for t in limits) == {"open": 1, "cancelled": 3}
    assert [(t.outcome.cancelled_why, t.outcome.reported) for t in limits] == [
        (None, ("fill",)), ("not filled", ("cancel",)), ("pulled", ()), ("run stopped", ())]


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


def test_a_live_trade_not_judged_is_open_only_while_the_record_has_no_end(con):
    a = enter(con, "gold-r2", at=h(1), now=h(1))                  # the network is off
    b = enter(con, "gold-r2", at=h(1), now=h(1))
    api.close_trade(con, b.id, reason="Out.", at=h(2), now=h(4))
    ids = lambda **kw: [t.id for t in api.list_trades(con, run="gold-r2", now=h(4), **kw)]  # noqa: E731
    assert {t.outcome.status for t in api.list_trades(con, run="gold-r2", now=h(4))} == {
        "not judged"}
    assert ids(open=True) == [a.id] and ids(open=False) == [b.id]


def test_a_void_keeps_its_outcome_beside_the_void(con):
    t = enter(con, "gold-r1", at=h(1), now=LATER)
    v = api.void_trade(con, t.id, reason="Wrong run.", now=LATER)
    assert v.decisions.voided and v.outcome == t.outcome


def test_a_live_trade_is_judged_from_yahoo_bars_fetched_on_read(con, fake_yahoo):
    y = fake_yahoo(STOPPED)
    t = enter(con, "gold-r2", at=m(1), now=m(1))
    v = api.get_trade(con, t.id, now=m(4))
    o = v.outcome
    assert (o.status, o.exit, o.exited_at, o.exit_reason, o.timeframe) == (
        "exited", 98.0, m(2), "stop", "1m")
    assert (v.judged, v.differs) == (o, ())
    assert y.calls == [("ACME", "1m", m(0), m(1)), ("ACME", "1m", m(1), m(4))]
    assert api.get_trade(con, t.id, now=m(48)).outcome == o and len(y.calls) == 2  # ended: no fetch


def test_a_live_trade_whose_bars_db_is_in_icloud_is_not_judged_and_no_file_is_made(con):
    (config.home() / ".bars.db.icloud").write_bytes(b"")
    t = enter(con, "gold-r2", at=m(1), now=m(1))
    o = api.get_trade(con, t.id, now=m(4)).outcome
    assert o.status == "not judged"
    assert "bars could not be read" in o.notes[0]
    assert "in iCloud but not on this Mac" in o.notes[0]
    assert not config.bars_path().exists()


def test_a_live_reported_fill_is_the_entry_and_the_judge_goes_on_from_it(con, fake_yahoo):
    fake_yahoo([minute(1, 101, 101.5, 99.5, 100.5), minute(2, 100.5, 104.5, 100, 104),
                minute(3, 104, 104.5, 103, 104)])
    t = enter(con, "gold-r2", entry_type="limit", at=m(1), now=m(1))
    v = api.report(con, t.id, state="filled", reason="The broker's fill.", at=m(1.5), price=99.75,
                   now=m(4))
    o, j = v.outcome, v.judged
    assert (o.fill, o.filled_at, o.exit, o.exited_at, o.exit_reason, o.reported) == (
        99.75, m(1.5), 104.0, m(3), "target", ("fill",))
    assert (j.fill, j.filled_at, j.exit, j.exited_at) == (100.0, m(2), 104.0, m(3))
    assert v.differs == ("fill", "r", "cash")      # m(1.5) lies in the judged fill bar


def test_a_live_reported_exit_replaces_an_earlier_judged_exit_and_both_are_shown(con, fake_yahoo):
    fake_yahoo(STOPPED)
    t = enter(con, "gold-r2", at=m(1), now=m(1))
    assert api.get_trade(con, t.id, now=m(4)).outcome.exit_reason == "stop"   # the bars: out at 98
    v = api.report(con, t.id, state="exited", reason="The broker never filled the stop.",
                   price=101.0, at=m(3.5), now=m(4))
    assert (v.outcome.exit, v.outcome.exited_at, v.outcome.exit_reason, v.outcome.reported) == (
        101.0, m(3.5), "hand", ("close",))
    assert (v.judged.exit, v.judged.exited_at, v.judged.exit_reason) == (98.0, m(2), "stop")
    assert v.differs == ("exit", "exited_at", "exit_reason", "r", "cash")


def test_a_live_open_report_sets_aside_the_judged_exit_and_the_judge_goes_on(con, fake_yahoo):
    fake_yahoo(STOPPED)
    t = enter(con, "gold-r2", at=m(1), now=m(1))
    v = api.report(con, t.id, state="open", reason="Still in at the broker.", at=m(2.5),
                   now=m(4))
    assert (v.outcome.status, v.outcome.fill, v.outcome.judged_through) == ("open", 100.0, m(4))
    assert (v.judged.status, v.judged.exit, v.judged.exited_at) == ("exited", 98.0, m(2))
    assert v.differs == ("status", "exit", "exited_at", "exit_reason", "r", "cash")
    assert [e["kind"] for e in store.events(con, trade_id=t.id)] == ["entered", "held"]


def test_withdrawing_a_live_report_returns_the_judged_outcome(con, fake_yahoo):
    def seen(v):
        return v.outcome.status, v.outcome.exit, v.outcome.exited_at, v.outcome.reported, v.differs

    fake_yahoo(STOPPED)
    t = enter(con, "gold-r2", at=m(1), now=m(1))
    v = api.report(con, t.id, state="open", reason="Still in.", at=m(2.5), now=m(4))
    assert v.outcome.status == "open"
    v = api.void_trade(con, t.id, reason="Not so.", report="open", now=m(4))
    assert seen(v) == ("exited", 98.0, m(2), (), ())               # the bars' answer again
    api.report(con, t.id, state="exited", reason="Out.", price=101.0, at=m(3.5), now=m(4))
    v = api.void_trade(con, t.id, reason="Not so.", report="close", now=m(4))
    assert seen(v) == ("exited", 98.0, m(2), (), ())


def test_a_replay_open_report_is_kept_and_changes_no_outcome(con):
    t = enter(con, "gold-r1", at=h(1), now=LATER)
    v = api.report(con, t.id, state="open", reason="Still in.", at=h(2), now=LATER)
    assert v.outcome == t.outcome and v.decisions.held.at == h(2)


def test_a_live_report_made_before_the_bars_is_read_and_nothing_is_compared(con):
    t = enter(con, "gold-r2", at=h(1), now=h(1))
    v = api.report(con, t.id, state="exited", reason="Stopped.", kind="stop", price=97.5,
                   at=h(1.5), now=h(3))                             # the network is off
    assert (v.outcome.status, v.outcome.exit, v.outcome.r) == ("exited", 97.5, -1.25)
    assert (v.judged.status, v.differs) == ("not judged", ())


def test_a_trade_not_judged_for_want_of_the_network_is_judged_once_bars_come(con, fake_yahoo):
    t = enter(con, "gold-r2", at=m(1), now=m(1))
    o = api.get_trade(con, t.id, now=m(4)).outcome                  # the network is off
    assert (o.status, o.missing) == ("not judged", (m(1), m(4)))
    assert any("the network is off in tests" in n for n in o.notes)
    with closing(bars.open()) as b:
        assert bars.read(b, "yahoo", "ACME", 1, m(0)) == []
    fake_yahoo(STOPPED)
    assert api.get_trade(con, t.id, now=m(5)).outcome.status == "exited"
    assert [e["kind"] for e in store.events(con, trade_id=t.id)] == ["entered"]


def test_a_reported_end_bounds_the_fetch_while_the_judged_side_cannot_finish(con, fake_yahoo):
    t = enter(con, "gold-r2", at=m(1), now=m(1))                    # the network is off
    api.report(con, t.id, state="exited", reason="The broker's fill.", price=101.0, at=m(6),
               now=m(6))
    y = fake_yahoo([])                  # Yahoo gives nothing, so the judged side never finishes
    for n in (100, 200):
        assert api.get_trade(con, t.id, now=m(n)).judged.status == "closing"
    assert [c[3] for c in y.calls] == [m(7), m(7)]   # through the bar holding the end, not now


def test_a_live_read_after_the_close_reads_open_through_the_last_complete_bar(con, fake_yahoo):
    fake_yahoo([minute(n, 100, 101, 99.5, 100.5) for n in range(4)], market_time=m(3.5))
    t = enter(con, "gold-r2", at=m(1), now=m(1))
    o = api.get_trade(con, t.id, now=m(70)).outcome   # long after: nothing new, the last forming
    assert (o.status, o.judged_through, o.missing) == ("open", m(3), None)


def test_there_is_no_coarser_fallback(con, fake_yahoo):
    y = fake_yahoo({"4h": [minute(0, 100, 104.5, 99, 104)]})
    o = enter(con, "gold-r2", at=m(1), now=m(8)).outcome
    assert o.status == "open" and {c[1] for c in y.calls} == {"1m"}


def test_a_correction_is_judged_in_place(con, fake_yahoo):
    fake_yahoo([minute(0, 100, 101, 99, 100.5), minute(1, 100, 101, 99.5, 100.5),
                minute(2, 100.5, 104.5, 100, 104), minute(3, 104, 104.5, 103, 104)])
    t = enter(con, "gold-r2", at=m(1), now=m(4))
    assert t.outcome.exit_reason == "target"
    o = api.correct_trade(con, t.id, fields={"stop": 99.5}, reason="Typo.", now=m(4)).outcome
    assert (o.exit, o.exited_at, o.exit_reason, o.r) == (99.5, m(2), "stop", -1.0)


def test_bars_that_cannot_be_read_leave_one_recorded_trade_not_judged(con, monkeypatch):
    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(bars, "open", locked)
    t = enter(con, "gold-r2", at=h(1), now=h(1))        # returns: the entry is committed
    assert t.outcome.status == "not judged"
    assert any("database is locked" in n for n in t.outcome.notes)
    assert [e["kind"] for e in store.events(con, trade_id=t.id)] == ["entered"]
    v = api.report(con, t.id, state="exited", reason="The broker's fill.", price=101.0, at=h(3),
                   now=h(4))
    assert (v.outcome.status, v.outcome.exit, v.outcome.reported) == ("exited", 101.0, ("close",))
    assert (v.judged.status, v.differs) == ("not judged", ())   # the report stands without bars


def test_a_live_future_says_it_was_judged_on_yahoos_rolling_series(con):
    t = enter(con, "gold-r2", ticker="GC1!", instrument=None, point_value=100.0, at=h(1),
              now=h(1))
    notes = api.get_trade(con, t.id, now=h(4)).outcome.notes
    assert any("GC=F" in n and "roll" in n for n in notes)


DIP = [minute(60, 100, 101, 99, 100.5), minute(61, 100, 101, 97.5, 99),
       minute(62, 99, 100, 98.5, 99.5)]   # a market long at h(1) exits at its stop, 98, at h(1)+2m


def slow_run(con, finest):
    api.save_strategy(con, "slow", GOLD.replace('["1h", "4h"]', f'["{finest}"]'),
                      now=h(0))
    return api.start_run(con, "slow", mode="live", currency="USD", at=h(0), now=h(0)).id


@pytest.mark.parametrize("finest", ["1m", "15m", "1h", "90m", "4h", "1d", "1w"])
@pytest.mark.parametrize("ticker", ["ACME", "GC1!"])
def test_every_live_run_is_judged_on_1m_bars(con, fake_yahoo, finest, ticker):
    y = fake_yahoo({"1m": DIP})
    future = ({"point_value": 100.0, "instrument": None} if ticker == "GC1!"
              else {"instrument": "stock"})
    t = enter(con, slow_run(con, finest), ticker=ticker, **future, at=h(1), now=h(1))
    o = api.get_trade(con, t.id, now=LATER).outcome
    assert (o.status, o.exit, o.exited_at, o.timeframe) == (
        "exited", 98.0, h(1) + timedelta(minutes=2), "1m")
    assert {c[1] for c in y.calls} == {"1m"}


def test_a_closed_live_trade_reads_only_the_bars_up_to_its_end(con, fake_yahoo, monkeypatch):
    fake_yahoo({"1m": DIP})
    t = enter(con, "gold-r2", instrument="stock", at=h(1), now=h(1))
    api.get_trade(con, t.id, now=LATER)            # fetched and stored; the trade has ended
    ends, real = [], bars.read

    def spy(con, source, symbol, minutes, start, end=None):
        ends.append(end)
        return real(con, source, symbol, minutes, start, end)
    monkeypatch.setattr(bars, "read", spy)
    o = api.get_trade(con, t.id, now=LATER + timedelta(days=60)).outcome
    assert o.exit == 98.0
    # the first window, one day from a minute before the entry, holds the exit
    assert ends and all(e is not None and e <= h(1) + timedelta(days=1) for e in ends), ends


def test_a_gap_past_yahoos_1m_window_is_named(con, fake_yahoo):
    fake_yahoo({"1m": []})
    t = enter(con, "gold-r2", instrument="stock", at=h(1), now=h(1))
    o = api.get_trade(con, t.id, now=h(1) + timedelta(days=45)).outcome
    assert o.status == "not judged" and any("about 29 days" in n for n in o.notes), o.notes


def test_a_hand_close_the_next_day_is_priced_from_that_days_bars(con, fake_yahoo):
    # the first window holds the first day's bars only: its last bar is not the last before the
    # close, and the trade is not priced from it
    fake_yahoo({"1m": [DIP[0], minute(61, 100.5, 101, 99.5, 100.5),
                       minute(1500, 100.5, 101.5, 100, 101), minute(1501, 101, 102, 100.5, 101.5)]})
    t = enter(con, "gold-r2", instrument="stock", at=h(1), now=h(1))
    out = h(25) + timedelta(minutes=2)
    o = api.close_trade(con, t.id, reason="Out.", at=out, now=h(26)).outcome
    assert (o.exit, o.exited_at, o.exit_reason) == (101.5, out, "hand")
    assert api.get_trade(con, t.id, now=h(26)).outcome == o


@pytest.mark.parametrize(("call", "says"), [
    (lambda con, t: api.get_trade(con, t, now=datetime(2026, 3, 2)), "ISO-8601 time with its UTC"),
    (lambda con, t: api.get_trade(con, 5), "trade"),
    (lambda con, t: api.list_trades(con, open="yes"), "open"),
    (lambda con, t: api.list_trades(con, run=5), "run")])
def test_reads_refuse_a_naive_now_and_arguments_of_the_wrong_type(con, call, says):
    t = enter(con, "gold-r1", at=h(1), now=LATER)
    with pytest.raises(api.Refused, match=says):
        call(con, t.id)


@pytest.mark.parametrize(("ends", "kept"), [
    (lambda con, t: api.report(con, t, state="exited", reason="Out.", price=101.0, at=m(3.5),
                               now=m(4)), False),          # a report: the author decided no close
    (lambda con, t: api.close_trade(con, t, reason="Out.", at=m(3.5), now=m(4)), True)])
def test_the_judged_side_names_no_close_the_author_did_not_decide(con, fake_yahoo, ends, kept):
    t = enter(con, "gold-r2", at=m(1), now=m(1))
    ends(con, t.id)            # the network is off: a close the bars would refuse is accepted
    fake_yahoo(STOPPED)
    v = api.get_trade(con, t.id, now=m(4))                         # the bars: out at 98 at m(2)
    assert v.judged.exit_reason == "stop"
    assert any(n.startswith("the close at") for n in v.judged.notes) is kept


def test_a_fill_reported_while_bars_cannot_be_read_is_the_position(con, monkeypatch):
    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(bars, "open", locked)
    t = enter(con, "gold-r2", entry_type="limit", at=h(1), now=h(1))
    assert (t.outcome.fill, t.judged.fill) == (None, None)
    v = api.report(con, t.id, state="filled", reason="Filled.", price=99.5, at=h(2), now=h(4))
    o = v.outcome
    assert (o.status, o.fill, o.filled_at, o.reported) == ("not judged", 99.5, h(2), ("fill",))
    assert (v.judged.fill, v.judged.reported, v.differs) == (None, (), ())


def test_a_replay_read_that_fails_keeps_its_reports_and_carries_no_judged_outcome(con,
                                                                                  monkeypatch):
    t = enter(con, "gold-r1", at=h(1), now=LATER)
    api.report(con, t.id, state="exited", kind="target", reason="Target.", at=h(3), now=LATER)

    def broken(*a, **k):
        raise ValueError("invented failure")
    monkeypatch.setattr(judge, "replay", broken)
    v = api.get_trade(con, t.id)
    assert (v.outcome.status, v.outcome.exit, v.outcome.reported) == ("exited", 104.0, ("close",))
    assert (v.judged, v.differs) == (None, ())
    other = enter(con, "gold-r1", at=h(1), now=LATER).outcome     # nothing reported: not judged
    assert other.status == "not judged"
    assert other.notes == ("bars could not be read: ValueError: invented failure",)


BOTH = [STOPPED[0], minute(1, 100, 104.5, 97.5, 101), *STOPPED[2:]]   # m(1) reaches 98 and 104
TOUCHED = [minute(0, 100, 101, 99, 100.5), minute(1, 100, 101, 99, 100.5),
           minute(2, 100.5, 104.5, 100, 103),
           minute(3, 103, 103.5, 102.5, 103)]   # m(2) touches 104
STOP = re.escape(f"exited at {m(2).isoformat()}, at 98.0 (stop)")


def live_trade(con, fake_yahoo, rows, **kw):
    fake_yahoo(rows)
    return enter(con, "gold-r2", **{"at": m(1), "now": m(1), **kw})


def test_a_move_after_the_judged_exit_is_refused_naming_when_and_at_what_price(con, fake_yahoo):
    t = live_trade(con, fake_yahoo, STOPPED)
    with pytest.raises(api.Refused, match=STOP):
        api.move_order(con, t.id, stop=97.0, reason="Wider.", at=m(2), now=m(4))
    assert [e["kind"] for e in store.events(con, trade_id=t.id)] == ["entered"]


def test_a_move_at_the_exit_bars_open_is_in_force_in_that_bar(con, fake_yahoo):
    t = live_trade(con, fake_yahoo, STOPPED)
    assert api.move_order(con, t.id, stop=97.0, reason="Wider.", at=m(1),
                          now=m(4)).outcome.status == "open"


@pytest.mark.parametrize("rows", [STOPPED, BOTH], ids=["stop", "stop and target"])
def test_a_close_after_the_judged_exit_is_refused(con, fake_yahoo, rows):
    t = live_trade(con, fake_yahoo, rows)
    for at in (m(3), m(2)):   # the exit before the last bar; in it (m(1), finished at m(2))
        with pytest.raises(api.Refused, match=STOP):
            api.close_trade(con, t.id, reason="Out.", at=at, now=m(4))
    assert [e["kind"] for e in store.events(con, trade_id=t.id)] == ["entered"]


def test_a_close_after_a_finished_bar_that_touched_the_target_is_refused(con, fake_yahoo):
    t = live_trade(con, fake_yahoo, TOUCHED)
    target = re.escape(f"exited at {m(3).isoformat()}, at 104.0 (target)")
    with pytest.raises(api.Refused, match=target):   # the m(2) bar is judged in full
        api.close_trade(con, t.id, reason="Out.", at=m(3), now=m(4))
    assert [e["kind"] for e in store.events(con, trade_id=t.id)] == ["entered"]
    o = api.close_trade(con, t.id, reason="Out.", at=m(2), now=m(4)).outcome   # before that bar
    assert (o.status, o.exit, o.exited_at, o.exit_reason) == ("exited", 100.5, m(2), "hand")


def test_a_live_close_made_mid_bar_ignores_that_bars_target(con, fake_yahoo):
    t = live_trade(con, fake_yahoo, [minute(0, 100, 101, 99, 100.5),
                                     minute(1, 100, 104.5, 99.5, 103),
                                     minute(2, 103, 103.5, 102.5, 103)])
    # bar 1 forming at m(1.5)
    o = api.close_trade(con, t.id, reason="Out.", at=m(1.5), now=m(3)).outcome
    assert (o.status, o.exit, o.exited_at, o.exit_reason) == ("exited", 103.0, m(2), "hand")


def test_a_live_close_recorded_after_its_bar_reached_the_stop_is_refused(con, fake_yahoo):
    t = live_trade(con, fake_yahoo, STOPPED[:3])
    with pytest.raises(api.Refused, match=STOP) as err:   # bar 1 forming at m(1.5): its stop counts
        api.close_trade(con, t.id, reason="Out.", at=m(1.5), now=m(3))
    assert "cannot set that exit aside" in str(err.value) and 'state="open"' not in str(err.value)
    assert [e["kind"] for e in store.events(con, trade_id=t.id)] == ["entered"]


def test_a_reported_exit_after_the_judged_exit_is_accepted_and_a_move_before_it_stands(con,
                                                                                      fake_yahoo):
    t = live_trade(con, fake_yahoo, STOPPED)
    with pytest.raises(api.Refused, match=re.escape('state="exited"')):   # the fix it names
        api.close_trade(con, t.id, reason="Out.", at=m(3), now=m(4))
    v = api.report(con, t.id, state="exited", reason="The broker's fill.", price=99.25, at=m(3),
                   now=m(4))
    assert (v.outcome.exit, v.outcome.reported, v.judged.exit) == (99.25, ("close",), 98.0)
    assert api.move_order(con, t.id, stop=97.0, reason="Wider.", at=m(2.5),
                          now=m(4)).decisions.moves


def test_after_an_open_report_a_move_and_a_close_are_judged_from_it(con, fake_yahoo):
    t = live_trade(con, fake_yahoo, STOPPED)
    with pytest.raises(api.Refused, match=re.escape('state="open"')):     # the fix it names
        api.move_order(con, t.id, stop=97.0, reason="Wider.", at=m(3), now=m(4))
    api.report(con, t.id, state="open", reason="Still in at the broker.", at=m(2.5), now=m(4))
    assert api.move_order(con, t.id, stop=97.0, reason="Wider.", at=m(3), now=m(4)).decisions.moves
    o = api.close_trade(con, t.id, reason="Out.", at=m(4), now=m(5)).outcome
    assert (o.status, o.exit, o.exited_at, o.exit_reason) == ("exited", 99.5, m(4), "hand")


def test_an_open_report_inside_the_exit_bar_does_not_reopen_it(con, fake_yahoo):
    t = live_trade(con, fake_yahoo, STOPPED)
    o = api.report(con, t.id, state="open", reason="Still in.", at=m(1.5), now=m(4)).outcome
    assert (o.status, o.exited_at) == ("exited", m(2))         # the stop in that bar stands
    assert any("inside the bar" in n for n in o.notes)
    fix = re.escape(f"report it open at or after {m(2).isoformat()}")
    with pytest.raises(api.Refused, match=fix):
        api.move_order(con, t.id, stop=97.0, reason="Wider.", at=m(1.5), now=m(4))
    api.report(con, t.id, state="open", reason="Still in.", at=m(2), now=m(4))   # its bar's close
    v = api.move_order(con, t.id, stop=97.0, reason="Wider.", at=m(1.5), now=m(4))
    assert v.decisions.moves and v.outcome.status == "open"


def test_a_corrected_move_after_the_judged_exit_is_refused(con, fake_yahoo):
    t = live_trade(con, fake_yahoo, STOPPED)
    api.move_order(con, t.id, stop=97.0, reason="Wider.", at=m(1), now=m(4))
    with pytest.raises(api.Refused, match="already exited"):
        api.correct_trade(con, t.id, fields={"move": 0, "at": m(2)}, reason="Typo.", now=m(4))
    assert api.correct_trade(con, t.id, fields={"stop": 98.5}, reason="Typo.",
                             now=m(4)).decisions.entry.stop == 98.5   # an entry fix: never refused


@pytest.mark.parametrize("run", ["gold-r1", "gold-r2"], ids=["replay", "live"])
def test_a_move_on_a_limit_the_run_stop_cancelled_is_refused(con, fake_yahoo, run):
    fake_yahoo(STOPPED)
    t = enter(con, run, entry_type="limit", entry=90.0, stop=88.0, target=94.0, at=m(1), now=m(1))
    api.stop_run(con, run, at=m(2), now=m(4))
    with pytest.raises(api.Refused, match=re.escape(f"cancelled at {m(2).isoformat()}")):
        api.move_order(con, t.id, stop=89.0, reason="Tighter.", at=m(3), now=m(4))
    assert api.move_order(con, t.id, stop=89.0, reason="Tighter.", at=m(1.5),
                          now=m(4)).outcome.status == "cancelled"


def test_a_move_on_a_trade_not_judged_is_accepted(con):
    t = enter(con, "gold-r2", at=h(1), now=h(1))
    assert api.move_order(con, t.id, stop=99.0, reason="Tighter.", at=h(5),
                          now=h(6)).decisions.moves


def test_the_fetch_happens_before_the_write_lock(con, fake_yahoo, monkeypatch):
    fake_yahoo([minute(n, 100, 101, 99.5, 100.5) for n in range(4)])
    seen, real = [], yahoo.fetch
    monkeypatch.setattr(yahoo, "fetch",
                        lambda *a, **k: (seen.append(con.in_transaction), real(*a, **k))[1])
    t = enter(con, "gold-r2", at=m(1), now=m(1))
    api.move_order(con, t.id, stop=99.0, reason="Tighter.", at=m(4), now=m(4))
    assert seen and not any(seen)


def test_a_live_move_in_the_bar_the_trade_then_exits_in_reads_no_effect(con, fake_yahoo):
    t = live_trade(con, fake_yahoo, STOPPED[:3])
    # bar 1 still forming at m(1.5)
    api.move_order(con, t.id, stop=99.5, reason="Tighter.", at=m(1.5), now=m(1.5))
    o = api.get_trade(con, t.id, now=m(3)).outcome
    assert (o.exit, o.exited_at) == (98.0, m(2)) and any("no effect" in n for n in o.notes)


def test_a_close_at_the_exit_bars_close_names_only_the_exit_report(con, fake_yahoo):
    t = live_trade(con, fake_yahoo, STOPPED)
    with pytest.raises(api.Refused, match="cannot set that exit aside") as err:   # at m(2) itself
        api.close_trade(con, t.id, reason="Out.", at=m(2), now=m(4))
    assert 'state="open"' not in str(err.value)


def test_a_move_at_the_moment_the_run_stop_cancelled_the_limit_is_refused(con):
    t = enter(con, "gold-r1", entry_type="limit", at=h(1), now=LATER)
    api.stop_run(con, "gold-r1", at=h(2), now=LATER)
    with pytest.raises(api.Refused, match="no order to move"):
        api.move_order(con, t.id, stop=97.0, reason="Tighter.", at=h(2), now=LATER)


def test_a_failed_fetch_is_not_tried_again_under_the_write_lock(con, monkeypatch):
    seen = []

    def down(*a, **k):
        seen.append(con.in_transaction)
        raise OSError("invented outage")
    monkeypatch.setattr(yahoo, "fetch", down)
    t = enter(con, "gold-r2", at=h(1), now=h(1))
    api.move_order(con, t.id, stop=99.0, reason="Tighter.", at=h(2), now=h(4))
    api.correct_trade(con, t.id, fields={"move": 0, "at": h(3)}, reason="Typo.", now=h(4))
    api.close_trade(con, t.id, reason="Out.", at=h(4), now=h(4))
    assert seen and not any(seen)
