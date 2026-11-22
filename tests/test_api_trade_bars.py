"""trade_bars serves the bars around one live trade, for the author's own tracking code (design §7).
A replay trade has none in the desk. Yahoo is a stub here; every figure is invented."""
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from desk import api, bars, judge, store

B = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
LATER = B + timedelta(hours=24)
GOLD = "---\nname: Gold\n---\n\n# Gold\n\n```rules\ntimeframes = [\"1h\", \"4h\"]\n```\n"
BASE = dict(ticker="ACME", side="long", entry_type="market", entry=100.0, stop=98.0, target=104.0,
            size=10, currency="USD", instrument="stock", reasons="Invented.")


def h(n):
    return B + timedelta(hours=n)


def bar(n, o, hi, lo, c):
    return judge.Bar(h(n), o, hi, lo, c, None)


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


# A list is served at every interval, so the 1-minute bars judged open where these 1h bars do: a
# market long at h(1) exits at the target in the 1m bar opening at h(3), dated h(3) + 1m.
SIX = [bar(n, 100, 101, 99.5, 100.5) if n != 3 else bar(3, 100.5, 104.5, 100, 104)
       for n in range(6)]
EXIT = h(3) + timedelta(minutes=1)


def live_trade(con, fake_yahoo, rows=SIX, **kw):
    y = fake_yahoo(rows)
    return enter(con, "gold-r2", **{"at": h(1), "now": h(1), **kw}), y


def test_by_default_the_trades_own_bars_from_the_entry_bar_through_the_exit_bar(con, fake_yahoo):
    t, _ = live_trade(con, fake_yahoo)
    tb = api.trade_bars(con, t.id, now=h(6))
    assert tb.timeframe == "1h" and [b.open_time for b in tb.bars] == [h(1), h(2), h(3)]
    assert (tb.missing, tb.notes) == ((), ())


def test_bars_that_cannot_be_fetched_say_why(con):
    t = enter(con, "gold-r2", at=h(1), now=h(1))                    # the network is off
    tb = api.trade_bars(con, t.id, now=h(4))
    assert (tb.bars, tb.missing) == ((), ((h(1), h(4)),))
    assert len(tb.notes) == 1 and "the network is off in tests" in tb.notes[0]


def test_before_and_after_add_bars_either_side(con, fake_yahoo):
    t, _ = live_trade(con, fake_yahoo)
    assert [b.open_time for b in api.trade_bars(con, t.id, before=1, after=1, now=h(6)).bars] == [
        h(0), h(1), h(2), h(3), h(4)]


def test_before_reaches_further_back_than_the_four_days_spare(con, fake_yahoo):
    earlier = [bar(n, 100, 101, 99.5, 100.5) for n in range(-120, 0)]   # five days of hours
    t, _ = live_trade(con, fake_yahoo, earlier + SIX)
    assert api.trade_bars(con, t.id, before=100, now=h(6)).bars[0].open_time == h(-99)


def test_an_open_trade_runs_through_the_latest_bar(con, fake_yahoo):
    t, _ = live_trade(con, fake_yahoo, target=110.0)
    assert api.trade_bars(con, t.id, after=3, now=h(6)).bars[-1].open_time == h(5)


def test_a_coarser_timeframe_reads_its_own_bars(con, fake_yahoo):
    # Yahoo has no 4h (ruling R10), so the coarser timeframe here is 90m: 14:00, 15:30, 17:00, 18:30
    api.save_strategy(con, "fine", GOLD.replace('["1h", "4h"]', '["1h", "90m"]'),
                      now=h(0))
    run = api.start_run(con, "fine", mode="live", currency="USD", at=h(0), now=h(0)).id
    fake_yahoo({"1m": SIX, "60m": SIX,
                "90m": [bar(1.5 * n, 100, 101, 99.5, 100.5) for n in range(4)]})
    t = enter(con, run, at=h(1), now=h(1))
    tb = api.trade_bars(con, t.id, timeframe="90m", after=1, now=h(5))  # 18:30 is still forming
    assert [b.open_time for b in tb.bars] == [h(0), h(1.5), h(3)] and tb.missing == ()


@pytest.mark.parametrize(("kw", "says"), [
    (dict(timeframe="15m"), "1h, 4h"), (dict(timeframe=60), "timeframe"),
    (dict(timeframe="4h"), "1m, 2m, 5m, 15m, 30m"),   # in the version; Yahoo has none (R10)
    (dict(before=-1), "before"), (dict(after=True), "after"), (dict(before="2"), "before"),
    (dict(after=1.5), "after"), (dict(now=datetime(2026, 3, 3)), "ISO-8601 time with its"),
    (dict(trade=5), "trade")])
def test_bad_arguments_are_refused(con, kw, says):
    t = enter(con, "gold-r2", at=h(1), now=h(1))
    kw = dict(kw)
    with pytest.raises(api.Refused, match=says):
        api.trade_bars(con, kw.pop("trade", t.id), **kw)


def test_a_replay_trade_has_no_bars_to_serve(con):
    t = enter(con, "gold-r1", at=h(1), now=LATER)
    with pytest.raises(api.Refused, match="replay"):
        api.trade_bars(con, t.id)


def test_a_live_run_refuses_a_timeframe_yahoo_lacks(con):
    api.save_strategy(con, "odd", GOLD.replace('["1h", "4h"]', '["15m", "2h"]'),
                      now=LATER)
    run = api.start_run(con, "odd", mode="live", currency="USD", now=LATER).id
    t = enter(con, run, at=LATER, now=LATER)
    with pytest.raises(api.Refused, match="1m, 2m, 5m, 15m, 30m"):
        api.trade_bars(con, t.id, timeframe="2h", now=LATER)


def test_the_default_timeframe_is_the_runs_versions_not_the_latest(con, fake_yahoo):
    t, _ = live_trade(con, fake_yahoo)
    api.save_strategy(con, "gold", GOLD.replace('["1h", "4h"]', '["15m"]'),
                      if_current=api.get_strategy(con, "gold").current_hash, now=LATER)
    assert api.trade_bars(con, t.id, now=h(6)).timeframe == "1h"


def test_a_live_trades_bars_are_fetched_and_kept(con, fake_yahoo):
    t, _ = live_trade(con, fake_yahoo, SIX[:4])
    tb = api.trade_bars(con, t.id, before=1, now=h(4))
    assert [b.open_time for b in tb.bars] == [h(0), h(1), h(2), h(3)]
    with closing(bars.open()) as b:                                 # F5: kept in bars.db
        assert len(bars.read(b, "yahoo", "ACME", 60, h(0))) == 4


def test_a_void_trade_is_served(con, fake_yahoo):
    t, _ = live_trade(con, fake_yahoo)
    api.void_trade(con, t.id, reason="Wrong run.", now=h(6))
    assert api.trade_bars(con, t.id, now=h(6)).bars


def test_an_ended_trades_bars_are_fetched_only_through_its_window(con, fake_yahoo):
    t, y = live_trade(con, fake_yahoo)
    assert api.get_trade(con, t.id, now=h(6)).outcome.exited_at == EXIT   # judged, bars kept
    tb = api.trade_bars(con, t.id, now=h(400))                            # long after the exit
    assert [b.open_time for b in tb.bars] == [h(1), h(2), h(3)]
    assert max(c[3] for c in y.calls) <= EXIT + timedelta(hours=1, days=4)
