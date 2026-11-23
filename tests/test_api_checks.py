"""trade() runs the run's version's rules as of `at`, keeps the results it shows, and with
check_only writes nothing. Every figure is invented."""
import json
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest

from desk import api, config, model, server, store, views

NOW = datetime(2026, 3, 2, 15, 5, tzinfo=UTC)    # a Monday
START = NOW - timedelta(days=30)
RULES = ('timeframes = ["15m", "1h"]\nmin_reward_risk = 2\nmax_risk_per_trade = { USD = 20 }\n'
         'max_open_trades = 1\n')
RSI = RULES + "rsi = { at_most = 30 }\n"
BASE = dict(ticker="ACME", side="long", entry_type="market", entry=100.0, stop=98.0,
            target=104.0, size=10, instrument="stock",
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
    store.add_trade(con, "raw-1", REPLAY, "ACME", "long", "stock", 1.0)
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
    store.add_trade(con, "raw-1", replay, "ACME", "long", "stock", 1.0)
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


def test_max_open_trades_counts_a_live_trade_as_open_until_its_exit_is_reported(con):
    live, _ = version(con, 'timeframes = ["15m"]\nmax_open_trades = 1\n')
    a = enter(con, live, at=NOW - timedelta(hours=2))
    assert a.outcome.status == "open"                     # a live trade reads from its record
    opened = enter(con, live, at=NOW - timedelta(hours=1), check_only=True)
    assert result(opened, "max_open_trades").ok is False
    out = api.report(con, a.id, state="exited", kind="hand", price=101.0,
                     at=NOW - timedelta(minutes=30), reason="Invented.", now=NOW)
    assert (out.outcome.status, out.outcome.exited_at) == ("exited", NOW - timedelta(minutes=30))
    assert result(enter(con, live, at=NOW, check_only=True), "max_open_trades").ok is True


def test_an_entry_on_a_version_with_no_record_rule_reads_no_other_trade(con, monkeypatch):
    _, replay = version(con, 'timeframes = ["15m"]\nmin_reward_risk = 2\n')
    enter(con, replay)

    def no(*a, **k):
        raise AssertionError("views.others was called")
    monkeypatch.setattr(views, "others", no)
    b = enter(con, replay)
    assert [r.name for r in api.get_trade(con, b.id, now=NOW).checks_now] == ["min_reward_risk"]


def test_the_day_is_the_system_timezones_midnight_to_midnight(con, monkeypatch):
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

    a = enter(con, live, at=utc(3, 4, 58) + timedelta(seconds=30), now=later)
    out = api.report(con, a.id, state="exited", kind="stop", at=utc(3, 5), reason="Invented.",
                     now=later)                             # at its stop, 98: -1R
    assert (out.outcome.exited_at, out.outcome.r) == (utc(3, 5), -1.0)   # 00:00 EST, Mar 3
    assert day_r(live, utc(3, 5, 30)) == (True, -1.0)       # 00:30 EST sees it, on its date's day
    assert day_r(live, utc(3, 4, 59)) == (True, 0.0)        # 23:59 EST, before the exit: does not

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


def test_get_trade_shows_both_sets_and_names_the_difference(con):
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
    store.add_trade(con, "raw-1", REPLAY, "ACME", "long", "stock", 1.0)
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
