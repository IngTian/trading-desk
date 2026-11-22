"""trade() runs the run's version's rules as of `at`, keeps the results it shows, and with
check_only writes nothing. Every figure is invented."""
import json
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest

from desk import api, config, judge, model, server, store, yahoo

NOW = datetime(2026, 3, 2, 15, 5, tzinfo=UTC)    # Monday; 5 minutes into a 15m bar
START = NOW - timedelta(days=30)
RULES = ('timeframes = ["15m", "1h"]\nmin_reward_risk = 2\nmax_risk_per_trade = { USD = 20 }\n'
         'max_open_trades = 1\n')
RSI = RULES + "rsi = { at_most = 30 }\n"
BASE = dict(ticker="ACME", side="long", entry_type="market", entry=100.0, stop=98.0,
            target=104.0, size=10, currency="USD", instrument="stock",
            reasons="Invented reasons.")
GOLD = dict(ticker="GC1!", instrument=None, point_value=100.0, size=1, at=None)
LIVE, REPLAY = "tester-r1", "tester-r2"


def strategy_text(rules=RULES, checklist="- The trend is up.\n- No news within the hour.\n"):
    return f"---\nname: Tester\n---\n\n## Checklist\n{checklist}\n```rules\n{rules}```\n"


@pytest.fixture
def con(tmp_path):
    """One strategy, run r1 live and r2 replay, both USD, started at NOW - 30 days."""
    store.create(tmp_path / "desk.db")
    with store.open(tmp_path / "desk.db") as c:
        api.save_strategy(c, "tester", strategy_text(), now=START)
        api.start_run(c, "tester", mode="live", currency="USD", at=START, now=START)
        api.start_run(c, "tester", mode="replay", currency="USD", at=START, now=START)
        yield c


def version(con, rules):
    """A new version with these rules, and a live and a replay run on it: (live, replay)."""
    current = api.get_strategy(con, "tester").current_hash
    api.save_strategy(con, "tester", strategy_text(rules), if_current=current, now=START)
    return tuple(api.start_run(con, "tester", mode=mode, currency="USD", at=START, now=START).id
                 for mode in ("live", "replay"))


def enter(con, run=REPLAY, **kw):
    return api.trade(con, run, **{**BASE, "at": NOW, "now": NOW, **kw})


def result(view, name):
    shown = view.checks if isinstance(view, api.CheckView) else view.checks_shown
    return next(r for r in shown if r.name == name)


def payload(con, trade_id):
    return json.loads(store.events(con, trade_id=trade_id)[0]["payload"])


def bars_of(minutes, end, n, base=100.0):
    """n invented bars, the last closing at `end`; a sawtooth, so RSI is neither 0 nor 100."""
    m = timedelta(minutes=minutes)
    return [judge.Bar(end - m * (n - i), base + i % 7 - (i % 3) * 0.5, base + i % 7 + 0.5,
                      base + i % 7 - 1.0, base + i % 7 - (i % 3) * 0.5 + 0.1, 1000.0)
            for i in range(n)]


def bars15(end, n, base=100.0):
    """n invented 15m bars, the last closing at `end`."""
    return bars_of(15, end, n, base)


def test_trade_returns_and_keeps_the_results_shown(con):
    t = enter(con)
    assert [(r.name, r.ok) for r in t.checks_shown] == [
        ("min_reward_risk", True), ("max_risk_per_trade", True), ("max_open_trades", True)]
    shown = [asdict(r) for r in t.checks_shown]
    assert payload(con, t.id)["checks"] == shown
    assert model.decisions_of(con, t.id).checks == tuple(shown)
    assert api.get_trade(con, t.id, now=NOW).checks_shown == t.checks_shown


def test_a_version_with_no_rules_keeps_an_empty_list(con):
    _, replay = version(con, 'timeframes = ["15m"]\n')
    t = enter(con, replay)
    assert (t.checks_shown, payload(con, t.id)["checks"]) == ((), [])


def test_a_breach_never_refuses_a_trade(con):
    t = enter(con, target=103.0, check_only=False)
    assert isinstance(t, api.TradeView)
    assert (t.checks_shown[0].name, t.checks_shown[0].ok, t.checks_shown[0].figure) == (
        "min_reward_risk", False, 1.5)
    assert [r["id"] for r in store.trades(con)] == [t.id]


def test_check_only_validates_as_an_entry_and_writes_nothing(con, tmp_path):
    shot = tmp_path / "chart.png"
    shot.write_bytes(b"invented image bytes")
    cv = enter(con, check_only=True, screenshots=[str(shot)])
    assert isinstance(cv, api.CheckView)
    assert (cv.run, cv.at, cv.ticker, cv.side, cv.instrument, cv.point_value, cv.currency) == (
        REPLAY, NOW, "ACME", "long", "stock", 1.0, "USD")
    assert store.trades(con) == []
    assert {e["kind"] for e in store.events(con)} == {"run_started"}
    assert not config.attachments_dir().exists() or not any(config.attachments_dir().iterdir())
    with pytest.raises(api.Refused, match="not an existing file"):
        enter(con, check_only=True, screenshots=[str(tmp_path / "missing.png")])
    with pytest.raises(api.Refused, match="needs `at`"):
        enter(con, check_only=True, at=None)
    with pytest.raises(api.Refused, match="reasons must be text"):
        enter(con, check_only=True, reasons=5)
    with pytest.raises(api.Refused, match="is in USD"):
        enter(con, check_only=True, currency="CAD")
    assert store.trades(con) == [] and len(store.events(con)) == 2

    t = enter(con, screenshots=[str(shot)])    # the results trade shows are the same
    assert t.checks_shown == cv.checks
    api.stop_run(con, REPLAY, at=NOW + timedelta(hours=1), now=NOW + timedelta(hours=2))
    with pytest.raises(api.Refused, match="stopped"):
        enter(con, check_only=True, at=NOW + timedelta(hours=2), now=NOW + timedelta(hours=2))


def test_a_size_or_price_past_a_floats_range_is_refused_or_reads_unknown(con):
    for kw, says in ((dict(size=10 ** 400), "size must be"), (dict(entry=10 ** 400), "entry must"),
                     (dict(stop=10 ** 400), "stop must"), (dict(target=10 ** 400), "target must"),
                     ({**GOLD, "at": NOW, "point_value": 10 ** 400}, "point_value must")):
        with pytest.raises(api.Refused, match=says):
            enter(con, **kw)
    assert store.trades(con) == [] and len(store.events(con)) == 2
    t = enter(con, size=10 ** 308)   # a float, but its risk is not: recorded, that rule unknown
    r = result(t, "max_risk_per_trade")
    assert (r.ok, r.figure) == (None, None) and "too large to state" in r.says
    assert payload(con, t.id)["checks"] == [asdict(x) for x in t.checks_shown]
    with pytest.raises(api.Refused, match="size must be"):
        api.correct_trade(con, t.id, fields={"size": 10 ** 400}, reason="Invented.", now=NOW)
    assert len(store.events(con, trade_id=t.id)) == 1


@pytest.mark.parametrize("stored", [
    [{"item": "The trend is up.", "verdict": "maybe", "reason": "x"}, 5,
     {"item": ["No news within the hour."], "verdict": "pass", "reason": "x"}],
    "not a list"])
def test_a_damaged_stored_checklist_reads_not_answered(con, stored):
    store.add_trade(con, "raw-1", REPLAY, "ACME", "long", "stock", 1.0, "USD")
    store.append(con, kind="entered", run_id=REPLAY, trade_id="raw-1", at=NOW.isoformat(),
                 recorded_at=NOW.isoformat(),
                 payload={"entry_type": "market", "entry": 100.0, "stop": 98.0, "target": 104.0,
                          "size": 10, "reasons": "Written by hand.", "screenshots": [],
                          "checklist": stored})
    t = api.get_trade(con, "raw-1", now=NOW)
    assert [a["verdict"] for a in t.checklist] == ["not answered", "not answered"]
    assert t.checks_shown is None


@pytest.mark.parametrize("stored", [*({"rvol": v} for v in ("2", True, None, [2], 10 ** 400)),
                                    "rvol", ["rvol"]])
def test_a_damaged_stored_market_value_reads_not_supplied(con, stored):
    _, replay = version(con, RULES + "[facts]\nrvol = { at_least = 2 }\n")
    store.add_trade(con, "raw-1", replay, "ACME", "long", "stock", 1.0, "USD")
    store.append(con, kind="entered", run_id=replay, trade_id="raw-1", at=NOW.isoformat(),
                 recorded_at=NOW.isoformat(),
                 payload={"entry_type": "market", "entry": 100.0, "stop": 98.0, "target": 104.0,
                          "size": 10, "reasons": "Written by hand.", "screenshots": [],
                          "market": stored})
    r = now_of(api.get_trade(con, "raw-1", now=NOW), "rvol")
    assert (r.ok, r.says) == (None, "supply rvol, read off the chart at the bar acted on")
    assert "rvol" in api.stats(con, run=replay, conforming_only=True, now=NOW).left_out["unknown"]


@pytest.mark.parametrize("v", ["yes", 1, [True]])
def test_check_only_must_be_true_false_or_left_out(con, v):
    with pytest.raises(api.Refused, match="check_only must be true, false or left out"):
        enter(con, check_only=v)
    assert store.trades(con) == []


def test_market_holds_only_the_keys_the_rules_read(con):
    with pytest.raises(api.Refused, match="read no market values"):
        enter(con, market={"rsi_14_15m": 28})
    _, replay = version(con, RSI)
    with pytest.raises(api.Refused, match="rsi_14_15m"):
        enter(con, replay, market={"rsi_14_1h": 28})
    for v in (True, "28", float("nan"), float("inf"), 10 ** 400):
        with pytest.raises(api.Refused, match="finite number"):
            enter(con, replay, market={"rsi_14_15m": v})
    with pytest.raises(api.Refused, match="market must be"):
        enter(con, replay, market=[28])
    assert store.trades(con) == []
    t = enter(con, replay, market={"rsi_14_15m": 28})
    assert payload(con, t.id)["market"] == {"rsi_14_15m": 28.0}
    assert t.decisions.market == {"rsi_14_15m": 28.0}
    assert isinstance(t.decisions.market["rsi_14_15m"], float)


def test_checklist_answers_are_kept_as_given(con):
    answer = {"item": "The trend is up.", "verdict": "pass", "reason": "Higher highs."}
    t = enter(con, checklist=[answer])
    assert t.checklist == (answer, {"item": "No news within the hour.",
                                    "verdict": "not answered", "reason": None})
    assert payload(con, t.id)["checklist"] == [answer]
    assert enter(con, checklist=[answer], check_only=True).checklist == t.checklist
    for bad, says in (([{**answer, "item": "The trend is down."}], "not one of"),
                      ([{**answer, "verdict": "maybe"}], "pass or fail"),
                      ([{**answer, "reason": "  "}], "reason"),
                      ([answer, answer], "second answer"),
                      ([{**answer, "extra": 1}], "exactly"),
                      (answer, "must be a list")):
        with pytest.raises(api.Refused, match=says):
            enter(con, checklist=bad)
    assert len(store.trades(con)) == 1


def test_indicators_come_from_market(con):
    live, replay = version(con, RSI)
    for run in (live, replay):
        r = result(enter(con, run), "rsi_14_15m")
        assert r.ok is None and r.says == ("supply rsi_14_15m, read off the chart at the bar "
                                           "acted on")
        r = result(enter(con, run, market={"rsi_14_15m": 28.0}), "rsi_14_15m")
        assert (r.ok, r.figure) == (True, 28.0)


def test_no_fetch_runs_under_the_write_lock(con, monkeypatch):
    rows = bars15(NOW - timedelta(minutes=5), 300)
    under: list[bool] = []

    def fetch(symbol, interval, start, end):   # yahoo.ensure swallows a raise, so record it
        under.append(con.in_transaction)
        return yahoo.Fetched(tuple(r for r in rows if start <= r.open_time < end), end)
    monkeypatch.setattr(yahoo, "fetch", fetch)
    live, _ = version(con, RSI)
    # open while the next trades are entered
    wide = dict(stop=90.0, target=120.0, at=None, market={"rsi_14_15m": 28.0})
    enter(con, live, **wide, now=NOW - timedelta(hours=1))
    enter(con, live, **wide, check_only=True)
    before = len(under)
    t = enter(con, live, **wide, now=NOW + timedelta(minutes=20))
    assert len(under) > before and not any(under)
    assert result(t, "rsi_14_15m").figure == 28.0


def test_the_run_as_of_at_reads_the_effective_outcomes(con):
    t1, t2, t25, t3, t4 = (NOW - timedelta(hours=h) for h in (4, 3, 2.5, 2, 1))
    a = enter(con, at=t1)
    api.report(con, a.id, state="exited", kind="target", at=t3, reason="Invented.", now=NOW)
    b = enter(con, at=t2, check_only=True)
    assert result(b, "max_open_trades").ok is False and len(store.trades(con)) == 1
    assert result(enter(con, at=t4), "max_open_trades").ok is True    # A ended at t3
    # entered after C, dated before it: A is open at t2.5, and C is not yet entered
    assert result(enter(con, at=t25), "max_open_trades").ok is False
    api.void_trade(con, a.id, reason="Invented.", now=NOW)   # a void trade is out
    assert result(enter(con, at=t2, check_only=True), "max_open_trades").ok is True


def test_another_trades_unknown_state_withholds_and_names_the_report_that_settles_it(
        con, fake_yahoo):
    # a future not judged (the network is off); reported open, it is known open up to that report
    live, _ = version(con, 'timeframes = ["1d"]\nmax_open_trades = 1\n')
    a = enter(con, live, **{**GOLD, "at": NOW - timedelta(days=2)})
    api.report(con, a.id, state="open", at=NOW - timedelta(days=1), reason="Invented.", now=NOW)
    held = enter(con, live, **{**GOLD, "at": NOW - timedelta(hours=36)}, check_only=True)
    assert result(held, "max_open_trades").ok is False
    r = result(enter(con, live, **GOLD, check_only=True), "max_open_trades")
    assert r.ok is None and a.id in r.says and "report its exit, or that it is still open" in r.says

    # a hand close whose last bar is still forming reads closing until the bar is complete
    fake_yahoo(bars_of(1, NOW - timedelta(minutes=5), 300))
    live, _ = version(con, 'timeframes = ["15m"]\ndaily_loss_limit = { R = 1 }\n')
    wide = dict(stop=90.0, target=120.0)
    a = enter(con, live, **wide, at=NOW - timedelta(hours=1))
    assert api.close_trade(con, a.id, reason="Invented.", at=NOW, now=NOW).outcome.status == (
        "closing")
    later = NOW + timedelta(minutes=5)
    r = result(enter(con, live, **wide, at=later, now=later, check_only=True), "daily_loss_limit")
    assert r.ok is None and a.id in r.says
    assert "give the broker's price with correct_trade, {\"report\": \"close\"" in r.says


LAGGED = ('timeframes = ["15m"]\nmax_open_trades = 1\ndaily_loss_limit = { R = 0.5 }\n'
          'cooldown_after_loss = { minutes = 30 }\n')
RECORD = ("max_open_trades", "daily_loss_limit", "cooldown_after_loss")
T14 = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
T15 = T14 + timedelta(hours=1)
QUIET = [judge.Bar(T14 + timedelta(minutes=i), 100.0, 100.5, 99.5, 100.0, 1000.0)
         for i in range(30)]                                    # 14:00 to 14:29
STOPPED = judge.Bar(T14 + timedelta(minutes=30), 99.0, 99.5, 97.5, 98.0, 1000.0)   # the 14:30


def lagged(con, fake_yahoo, *, current: bool, **a):
    """Trade A at 14:00, read at 15:00, stopped out in the 14:30 bar (dated 14:31) when the feed
    is current; with Yahoo's delay, the feed ends at the 14:29 bar."""
    fake_yahoo(QUIET + [STOPPED] if current else QUIET,
               market_time=None if current else T14 + timedelta(minutes=30))
    live, _ = version(con, LAGGED)
    return live, enter(con, live, **{**a, "at": T14, "now": T15})


def record(con, live, minutes, now=T15):
    cv = enter(con, live, at=T14 + timedelta(minutes=minutes), now=now, check_only=True)
    return [result(cv, n) for n in RECORD]


def test_a_live_futures_exit_the_feed_has_not_delivered_reads_unknown(con, fake_yahoo):
    live, a = lagged(con, fake_yahoo, current=False, **GOLD)
    assert (a.outcome.status, a.outcome.judged_through) == ("open", T14 + timedelta(minutes=30))
    # at 14:30:30 the 14:30 bar is forming, and an exit in it is dated 14:31: A is known open
    assert [r.ok for r in record(con, live, 30.5)] == [False, True, True]
    for minutes in (31, 60):   # a bar past what A was judged through could hold its exit
        for r in record(con, live, minutes):
            assert r.ok is None and a.id in r.says, r
            assert "report its exit, or that it is still open" in r.says
            assert "judged only through 2026-03-02T14:30:00+00:00" in r.says    # R11
            assert ".999999" not in r.says and "not judged" not in r.says
    b = enter(con, live, at=T15, now=T15)
    left = api.stats(con, run=live, conforming_only=True, now=T15).left_out
    assert left == {"breached": (), "unknown": {n: (b.id,) for n in RECORD}}


def test_a_futures_open_report_inside_an_undelivered_bar_is_unknown_from_that_bars_close(
        con, fake_yahoo):
    live, a = lagged(con, fake_yahoo, current=False, **GOLD)
    h = T15 + timedelta(minutes=7)
    held = api.report(con, a.id, state="open", at=h, reason="Invented.", now=h)
    assert held.outcome.judged_through == h   # the 15:07 bar, holding h, is not in the feed yet
    at = T15 + timedelta(minutes=7.5)
    assert [r.ok for r in record(con, live, 67.5, now=at)] == [False, True, True]
    for r in record(con, live, 68, now=T15 + timedelta(minutes=8)):
        assert r.ok is None and a.id in r.says, r
    # the feed catches up: the 15:07 bar stops A out, dated its close, 15:08
    fake_yahoo(QUIET + [replace(QUIET[0], open_time=T14 + timedelta(minutes=i))
                        for i in range(30, 67)] + [replace(STOPPED, open_time=h)])
    out = api.get_trade(con, a.id, now=T15 + timedelta(minutes=50)).outcome
    assert (out.exited_at, out.r) == (T15 + timedelta(minutes=8), -1.0)


def test_a_futures_open_report_is_known_to_the_end_of_its_minute(con, fake_yahoo):
    fake_yahoo([])
    live, _ = version(con, LAGGED)
    a = enter(con, live, **{**GOLD, "at": T14, "now": T14})
    h = T15 + timedelta(minutes=7, seconds=20)
    api.report(con, a.id, state="open", at=h, reason="Invented.", now=h)
    same = enter(con, live, at=h, now=h, check_only=True)
    assert all(result(same, n).ok is not None for n in RECORD)       # the same `at`: known
    inside = h + timedelta(seconds=30)                                # 15:07:50, same minute
    assert all(result(enter(con, live, at=inside, now=inside, check_only=True), n).ok
               is not None for n in RECORD)
    nxt = T15 + timedelta(minutes=8)                                  # the minute it may end in
    for r in (result(enter(con, live, at=nxt, now=nxt, check_only=True), n) for n in RECORD):
        assert r.ok is None and a.id in r.says and "2026-03-02T15:08:00+00:00" in r.says, r


def test_a_mid_bar_futures_entry_is_known_until_the_first_bar_it_can_end_in(con, fake_yahoo):
    fake_yahoo(QUIET, market_time=T14 + timedelta(minutes=30))   # 14:00 to 14:29 delivered
    live, _ = version(con, LAGGED)
    at = T14 + timedelta(minutes=30.5)      # inside the 14:30 bar; its first bar is 14:31's
    a = enter(con, live, **{**GOLD, "at": at, "now": at})
    assert a.outcome.judged_through == at
    assert [r.ok for r in record(con, live, 31, now=T14 + timedelta(minutes=31))] == [
        False, True, True]                  # it read unknown from 14:31 before (over-withheld)
    for r in record(con, live, 32, now=T14 + timedelta(minutes=32)):
        assert r.ok is None and a.id in r.says, r


def test_an_entry_on_a_version_with_no_record_rule_reads_no_other_trade(con, monkeypatch):
    _, replay = version(con, 'timeframes = ["15m"]\nmin_reward_risk = 2\n')
    enter(con, replay)

    def no(*a, **k):
        raise AssertionError("_others was called")
    monkeypatch.setattr(api, "_others", no)
    b = enter(con, replay)
    assert [r.name for r in api.get_trade(con, b.id, now=NOW).checks_now] == ["min_reward_risk"]


@pytest.mark.parametrize("a, current, ok", [
    (GOLD, True, [True, False, False]),   # the feed current: out at 14:31 at -1R
    ({}, False, [False, True, True])])    # a stock's empty stretch reads as the market shut
def test_a_futures_exit_the_feed_has_delivered_or_a_stocks_lag_reads_known(con, fake_yahoo, a,
                                                                            current, ok):
    live, t = lagged(con, fake_yahoo, current=current, **a)
    assert t.outcome.status == ("exited" if current else "open")
    assert [r.ok for r in record(con, live, 60)] == ok


def test_the_day_is_the_system_timezones_midnight_to_midnight(con, fake_yahoo, monkeypatch):
    monkeypatch.setenv("TZ", "America/Toronto")
    time.tzset()
    later = datetime(2026, 3, 4, tzinfo=UTC)

    def utc(day, hour, minute=0):
        return datetime(2026, 3, day, hour, minute, tzinfo=UTC)

    def day_r(run, at):
        r = result(enter(con, run, at=at, now=later), "daily_loss_limit")
        return r.ok, r.figure

    live, replay = version(con, 'timeframes = ["1h"]\ndaily_loss_limit = { R = 1 }\n')
    a = enter(con, replay, at=utc(3, 3), now=later)
    api.report(con, a.id, state="exited", kind="stop", at=utc(3, 4, 30), reason="Invented.",
               now=later)                                   # 23:30 EST, -1R
    assert day_r(replay, utc(3, 5, 10)) == (True, 0.0)      # 00:10 EST: the next day
    assert day_r(replay, utc(3, 4, 45)) == (True, -1.0)     # 23:45 EST: the same day, at -1R

    fake_yahoo([judge.Bar(utc(3, 4, 58), 100.0, 100.5, 99.5, 100.0, 1000.0),
                judge.Bar(utc(3, 4, 59), 99.0, 99.5, 97.5, 98.5, 1000.0),    # 23:59 EST: the stop
                judge.Bar(utc(3, 5), 98.5, 99.0, 98.2, 98.5, 1000.0)])
    a = enter(con, live, at=utc(3, 4, 58) + timedelta(seconds=30), now=later)
    assert (a.outcome.exited_at, a.outcome.r) == (utc(3, 5), -1.0)   # dated 00:00 EST, Mar 3
    assert day_r(live, utc(3, 5, 30)) == (True, -1.0)       # 00:30 EST sees it, on its date's day
    assert day_r(live, utc(3, 4, 59)) == (True, 0.0)        # 23:59 EST, inside its bar: does not

    _, replay = version(con, 'timeframes = ["1h"]\ntrading_hours = { start = "09:30", '
                             'end = "16:00" }\n')
    for at, ok in ((datetime(2026, 10, 30, 13, 45, tzinfo=UTC), True),    # 09:45 EDT
                   (datetime(2026, 11, 2, 13, 45, tzinfo=UTC), False),    # 08:45 EST
                   (datetime(2026, 11, 2, 14, 45, tzinfo=UTC), True)):    # 09:45 EST
        cv = enter(con, replay, at=at, now=datetime(2026, 11, 3, tzinfo=UTC), check_only=True)
        assert result(cv, "trading_hours").ok is ok


# --- the results now, on get_trade ---------------------------------------------------------

def now_of(view, name):
    return next(r for r in view.checks_now if r.name == name)


def test_get_trade_shows_both_sets_and_names_what_differs(con):
    _, replay = version(con, 'timeframes = ["15m"]\nmax_open_trades = 1\n')
    a = enter(con, replay, at=NOW - timedelta(hours=2))
    b = enter(con, replay, at=NOW - timedelta(hours=1))
    assert result(b, "max_open_trades").ok is False
    api.void_trade(con, a.id, reason="Invented.", now=NOW)
    v = api.get_trade(con, b.id, now=NOW)
    assert now_of(v, "max_open_trades").ok is True
    assert result(v, "max_open_trades").ok is False
    assert v.checks_differ == ("max_open_trades",)


def test_a_correction_changes_the_results_now_never_the_shown(con):
    t = enter(con, target=103.0)
    assert result(t, "min_reward_risk").ok is False
    before = payload(con, t.id)["checks"]
    api.correct_trade(con, t.id, fields={"target": 104.0}, reason="Invented.", now=NOW)
    v = api.get_trade(con, t.id, now=NOW)
    assert now_of(v, "min_reward_risk").ok is True
    assert result(v, "min_reward_risk").ok is False
    assert v.checks_differ == ("min_reward_risk",)
    assert payload(con, t.id)["checks"] == before


def test_an_earlier_entry_recorded_later_changes_the_results_now(con):
    b = enter(con, at=NOW - timedelta(hours=1))
    assert result(b, "max_open_trades").ok is True
    enter(con, at=NOW - timedelta(hours=2))   # recorded after B, dated before it, still open
    v = api.get_trade(con, b.id, now=NOW)
    assert now_of(v, "max_open_trades").ok is False
    assert v.checks_differ == ("max_open_trades",)


def test_correcting_an_entrys_at_moves_the_recomputes_at(con):
    t1, t2, t3, t4 = (NOW - timedelta(hours=h) for h in (4, 3, 2, 1))
    a = enter(con, at=t1)
    b = enter(con, at=t2)
    api.report(con, a.id, state="exited", kind="target", at=t3, reason="Invented.", now=NOW)
    assert now_of(api.get_trade(con, b.id, now=NOW), "max_open_trades").ok is False  # A open at t2
    api.correct_trade(con, b.id, fields={"at": t4}, reason="Invented.", now=NOW)
    v = api.get_trade(con, b.id, now=NOW)      # after A's exit now
    assert v.decisions.entry.at == t4
    assert (now_of(v, "max_open_trades").ok, result(v, "max_open_trades").ok) == (True, False)
    assert v.checks_differ == ("max_open_trades",)


def test_an_entered_event_without_results_reads_none_shown(con):
    store.add_trade(con, "raw-1", REPLAY, "ACME", "long", "stock", 1.0, "USD")
    store.append(con, kind="entered", run_id=REPLAY, trade_id="raw-1", at=NOW.isoformat(),
                 recorded_at=NOW.isoformat(),
                 payload={"entry_type": "market", "entry": 100.0, "stop": 98.0, "target": 104.0,
                          "size": 10, "reasons": "Written by hand.", "screenshots": []})
    v = api.get_trade(con, "raw-1", now=NOW)
    assert v.checks_shown is None and v.checks_differ is None
    assert [r.name for r in v.checks_now] == [
        "min_reward_risk", "max_risk_per_trade", "max_open_trades"]


def test_reads_other_than_get_trade_do_not_recompute(con):
    t = enter(con)
    assert t.checks_now is None
    u = enter(con, at=NOW - timedelta(minutes=1))
    for v in (*api.list_trades(con, now=NOW),
              api.report(con, t.id, state="open", at=NOW, reason="Invented.", now=NOW),
              api.move_order(con, t.id, reason="Invented.", stop=99.0, at=NOW, now=NOW),
              api.correct_trade(con, t.id, fields={"size": 11}, reason="Invented.", now=NOW),
              api.report(con, u.id, state="exited", kind="hand", price=101.0, at=NOW,
                         reason="Invented.", now=NOW),
              api.void_trade(con, t.id, reason="Invented.", now=NOW)):
        assert v.checks_now is None and v.checks_differ is None


def test_a_void_trade_has_no_results_now(con):
    t = enter(con)
    api.void_trade(con, t.id, reason="Invented.", now=NOW)
    v = api.get_trade(con, t.id, now=NOW)
    assert v.checks_now is None and v.checks_differ is None


def test_checks_differ_is_null_unless_get_trade_compared(con):
    t = enter(con)
    assert t.checks_differ is None                                 # the entry's own reply
    assert api.get_trade(con, t.id, now=NOW).checks_differ == ()   # compared: nothing changed
    listed = api.list_trades(con, now=NOW)
    assert {v.checks_differ for v in listed} == {None}
    assert all(v["checks_differ"] is None for v in server.plain(listed))   # null over MCP, not []
