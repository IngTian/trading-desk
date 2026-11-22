"""Tests for starting, stopping and listing runs through the api. Every figure is invented."""
from datetime import UTC, datetime, timedelta

import pytest

from desk import api, judge, store, yahoo

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=3)


def make(prose: str = "Buy dips."):
    return (f"---\nname: Dip buyer\n---\n\n# Dip buyer\n\n{prose}\n\n"
            "```rules\ntimeframes = [\"1h\"]\nmin_reward_risk = 2\n```\n")


@pytest.fixture
def con(tmp_path):
    db = tmp_path / "desk.db"
    store.create(db)
    with store.open(db) as conn:
        api.save_strategy(conn, "dip-buyer", make(), now=NOW)
        yield conn


def test_a_live_run_starts_at_now_on_the_latest_version(con):
    v1 = api.get_strategy(con, "dip-buyer")
    api.save_strategy(con, "dip-buyer", make("Buy deeper dips."), if_current=v1.current_hash,
                      now=NOW)
    r = api.start_run(con, "dip-buyer", mode="live", currency="USD",
                      tags=["paper", "GC 15m", "paper"], now=NOW)
    assert (r.id, r.version, r.mode, r.started, r.stopped) == ("dip-buyer-r1", 2, "live", NOW, None)
    assert r.tags == ("paper", "GC 15m")
    r2 = api.start_run(con, "dip-buyer", mode="live", currency="USD", version=1, now=NOW)
    assert r2.id == "dip-buyer-r2"
    assert [x.id for x in api.list_runs(con, "dip-buyer")] == ["dip-buyer-r1", "dip-buyer-r2"]
    assert api.list_runs(con, "other") == []


def test_starting_writes_the_row_and_the_event_together(con):
    api.start_run(con, "dip-buyer", mode="live", currency="USD", now=NOW)
    assert [e["kind"] for e in store.events(con, run_id="dip-buyer-r1")] == ["run_started"]


def test_a_replay_start_without_at_is_refused_and_writes_nothing(con):
    with pytest.raises(api.Refused, match="at"):
        api.start_run(con, "dip-buyer", mode="replay", currency="USD", now=NOW)
    assert store.runs(con) == []
    r = api.start_run(con, "dip-buyer", mode="replay", currency="USD",
                      at=NOW - timedelta(days=30), now=NOW)
    assert r.started == NOW - timedelta(days=30)


@pytest.mark.parametrize("kw, match", [
    ({"strategy": "nope", "mode": "live"}, "no strategy"),
    ({"strategy": "dip-buyer", "mode": "live", "version": 9}, "no version 9"),
    ({"strategy": "dip-buyer", "mode": "paper"}, "mode"),
])
def test_start_refusals(con, kw, match):
    with pytest.raises(api.Refused, match=match):
        api.start_run(con, now=NOW, **{"currency": "USD", **kw})
    assert store.runs(con) == []


def test_stop_live_defaults_to_now_and_a_second_stop_is_refused(con):
    api.start_run(con, "dip-buyer", mode="live", currency="USD", now=NOW)
    r = api.stop_run(con, "dip-buyer-r1", now=LATER)
    assert r.stopped == LATER and r.started == NOW
    with pytest.raises(api.Refused, match="already stopped"):
        api.stop_run(con, "dip-buyer-r1", now=LATER)
    assert api.list_runs(con)[0].stopped == LATER


def test_stop_before_start_is_refused(con):
    api.start_run(con, "dip-buyer", mode="live", currency="USD", now=NOW)
    with pytest.raises(api.Refused, match="before"):
        api.stop_run(con, "dip-buyer-r1", at=NOW - timedelta(minutes=1), now=LATER)
    assert api.stop_run(con, "dip-buyer-r1", at=NOW, now=LATER).stopped == NOW


def test_replay_stop_without_at_is_refused(con):
    api.start_run(con, "dip-buyer", mode="replay", currency="USD",
                  at=NOW - timedelta(days=30), now=NOW)
    with pytest.raises(api.Refused, match="at"):
        api.stop_run(con, "dip-buyer-r1", now=LATER)
    assert api.list_runs(con)[0].stopped is None


def test_unknown_run_is_refused(con):
    with pytest.raises(api.Refused, match="no run"):
        api.stop_run(con, "ghost-r1", now=NOW)


def test_a_naive_datetime_is_refused_for_start_and_stop(con):
    naive = datetime(2026, 3, 2, 12, 0)
    with pytest.raises(api.Refused, match="ISO-8601 time with its UTC offset"):
        api.start_run(con, "dip-buyer", mode="replay", currency="USD", at=naive, now=NOW)
    api.start_run(con, "dip-buyer", mode="live", currency="USD", now=NOW)
    with pytest.raises(api.Refused, match="ISO-8601 time with its UTC offset"):
        api.stop_run(con, "dip-buyer-r1", at=naive, now=LATER)


@pytest.mark.parametrize("tags", ["paper", [1, None], ["paper", 5], 7])
def test_tags_that_are_not_a_list_of_strings_are_refused(con, tags):
    """Review item 5: tags="paper" was stored as ('p', 'a', 'p', 'e', 'r'), for good."""
    with pytest.raises(api.Refused, match="tags must be a list of strings"):
        api.start_run(con, "dip-buyer", mode="live", currency="USD", tags=tags, now=NOW)
    assert store.runs(con) == []


def test_no_tags_may_be_given_as_none(con):
    r = api.start_run(con, "dip-buyer", mode="live", currency="USD", tags=None, now=NOW)
    assert r.tags == ()


@pytest.mark.parametrize("finest", ["3m", "45m", "90m", "4h", "1d", "1w"])
def test_a_live_run_may_have_any_finest_timeframe(con, finest):
    api.save_strategy(con, "slow", make().replace('["1h"]', f'["{finest}"]'), now=NOW)
    assert api.start_run(con, "slow", mode="live", currency="USD", now=NOW).mode == "live"


def test_a_run_has_one_currency(con):
    r = api.start_run(con, "dip-buyer", mode="live", currency="CAD", now=NOW)
    assert r.currency == "CAD" and api.list_runs(con, "dip-buyer")[-1].currency == "CAD"


@pytest.mark.parametrize("cur", [None, "GBP", ["USD"]])
def test_start_run_refuses_a_missing_or_unknown_currency(con, cur):
    before = len(store.runs(con))
    with pytest.raises(api.Refused, match="CAD, USD"):
        api.start_run(con, "dip-buyer", mode="live", currency=cur, now=NOW)
    assert len(store.runs(con)) == before


T = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)     # an invented Monday, 14:00 UTC
NEXT_DAY = T + timedelta(days=1)


def bars1(end, n, base=100.0):
    """n invented 1-minute bars around 100, the last closing at `end`; never near a limit 10%
    below."""
    m = timedelta(minutes=1)
    return [judge.Bar(end - m * (n - i), base, base + 0.5, base - 0.5, base + 0.1, 1000.0)
            for i in range(n)]


def run(con, mode, timeframes):
    """A new version of dip-buyer with these timeframes, and a run on it started a day before T."""
    rules = f"timeframes = {timeframes}\nmin_reward_risk = 2\n"
    text = f"---\nname: Dip buyer\n---\n\n# Dip buyer\n\n```rules\n{rules}```\n"
    api.save_strategy(con, "dip-buyer", text, if_current=api.get_strategy(con, "dip-buyer")
                      .current_hash, now=NOW)
    return api.start_run(con, "dip-buyer", mode=mode, currency="USD",
                         at=T - timedelta(days=1), now=NOW).id


def enter(con, run, entry_type, entry, **kw):
    return api.trade(con, run, **{**dict(ticker="ACME", side="long", entry_type=entry_type,
                                         entry=entry, stop=entry * 0.95, target=entry * 1.2,
                                         size=10, currency="USD", instrument="stock",
                                         reasons="Invented reasons.", at=T, now=T), **kw})


def limit(con, run, **kw):
    return enter(con, run, "limit", 90.0, **kw)


def market(con, run, **kw):
    return enter(con, run, "market", 100.0, **kw)


def test_stop_run_names_the_orders_it_cancelled(con):
    r = run(con, "replay", '["15m"]')
    a, b = limit(con, r), limit(con, r)
    c = market(con, r)
    stopped = api.stop_run(con, r, at=NEXT_DAY, now=NEXT_DAY)
    assert isinstance(stopped, api.RunView) and stopped.stopped == NEXT_DAY
    assert stopped.cancelled == (a.id, b.id) and c.id not in stopped.cancelled
    assert stopped.awaiting_bars == ()
    assert stopped.needs_report == {}


def test_a_live_stop_the_bars_have_not_reached_is_awaiting_them(con, fake_yahoo):
    feed = bars1(T + timedelta(minutes=60), 60)        # 14:00 to 14:59, the last closing 15:00
    fake_yahoo(feed, market_time=T + timedelta(minutes=60))
    r = run(con, "live", '["15m"]')
    t = limit(con, r)
    stop = T + timedelta(minutes=62)
    stopped = api.stop_run(con, r, at=stop, now=stop)
    assert (stopped.cancelled, stopped.awaiting_bars) == ((), (t.id,))
    assert stopped.needs_report == {}
    fake_yahoo(feed + bars1(T + timedelta(minutes=75), 15), market_time=T + timedelta(minutes=75))
    out = api.get_trade(con, t.id, now=T + timedelta(minutes=80)).outcome
    assert (out.status, out.cancelled_why) == ("cancelled", "run stopped")


def test_stop_run_cancels_a_daily_futures_limit_judged_on_1m_bars(con, fake_yahoo):
    fake_yahoo(bars1(T + timedelta(minutes=40), 45), market_time=T + timedelta(minutes=40))
    r = run(con, "live", '["1d"]')
    t = limit(con, r, ticker="GC1!", instrument=None, point_value=100.0)
    stopped = api.stop_run(con, r, at=T + timedelta(minutes=30),
                           now=T + timedelta(minutes=41))
    assert (stopped.cancelled, stopped.awaiting_bars) == ((t.id,), ())
    assert stopped.needs_report == {}
    assert not hasattr(stopped, "not_judged")


@pytest.mark.parametrize("fails, says", [
    (OSError("the network is off in tests"), "the network is off in tests"),
    (ValueError("NOSUCH: possibly delisted; no price data found"), "possibly delisted"),
])
def test_a_live_stop_whose_bars_cannot_be_read_says_why(con, monkeypatch, fails, says):
    def fetch(*a):
        raise fails
    monkeypatch.setattr(yahoo, "fetch", fetch)
    r = run(con, "live", '["15m"]')
    t = limit(con, r)
    stopped = api.stop_run(con, r, at=NEXT_DAY, now=NEXT_DAY)
    assert (stopped.cancelled, stopped.awaiting_bars) == ((), ())
    assert list(stopped.needs_report) == [t.id]
    assert any(says in n for n in stopped.needs_report[t.id]), stopped.needs_report
    o = api.report(con, t.id, state="cancelled", why="run stopped", at=NEXT_DAY,
                   reason="Invented.", now=NEXT_DAY).outcome
    assert (o.status, o.cancelled_why) == ("cancelled", "run stopped")


def test_a_limit_pulled_before_the_stop_is_not_the_stops_to_report(con, monkeypatch):
    def fetch(*a):
        raise OSError("the network is off in tests")
    monkeypatch.setattr(yahoo, "fetch", fetch)
    r = run(con, "live", '["15m"]')
    t = limit(con, r)
    api.close_trade(con, t.id, reason="Changed my mind.", at=T + timedelta(minutes=5),
                    now=T + timedelta(minutes=5))
    stopped = api.stop_run(con, r, at=NEXT_DAY, now=NEXT_DAY)
    assert (stopped.cancelled, stopped.awaiting_bars, stopped.needs_report) == ((), (), {})


def _closed_after_a_backdated_stop(con):
    r = run(con, "live", '["15m"]')
    t = limit(con, r)
    api.close_trade(con, t.id, reason="Changed my mind.", at=T + timedelta(minutes=30),
                    now=T + timedelta(minutes=31))
    return r, t


def test_a_limit_closed_after_a_backdated_stop_is_the_stops_to_cancel(con, fake_yahoo):
    fake_yahoo(bars1(T + timedelta(minutes=40), 45), market_time=T + timedelta(minutes=40))
    r, t = _closed_after_a_backdated_stop(con)
    stopped = api.stop_run(con, r, at=T + timedelta(minutes=20), now=T + timedelta(minutes=41))
    assert (stopped.cancelled, stopped.awaiting_bars, stopped.needs_report) == ((t.id,), (), {})


def test_an_unreadable_limit_closed_after_a_backdated_stop_is_in_no_group(con, monkeypatch):
    def fetch(*a):
        raise OSError("the network is off in tests")
    monkeypatch.setattr(yahoo, "fetch", fetch)
    r, _ = _closed_after_a_backdated_stop(con)
    stopped = api.stop_run(con, r, at=T + timedelta(minutes=20), now=T + timedelta(minutes=41))
    assert (stopped.cancelled, stopped.awaiting_bars, stopped.needs_report) == ((), (), {})


def test_a_start_or_stop_dated_after_now_is_refused(con):
    with pytest.raises(api.Refused, match="is after now"):
        api.start_run(con, "dip-buyer", mode="live", currency="USD", at=LATER, now=NOW)
    assert store.runs(con) == []
    api.start_run(con, "dip-buyer", mode="live", currency="USD", now=NOW)
    with pytest.raises(api.Refused, match="is after now"):
        api.stop_run(con, "dip-buyer-r1", at=LATER, now=NOW)
    assert api.list_runs(con)[0].stopped is None
