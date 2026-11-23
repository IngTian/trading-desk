"""Tests for the 1.0 checks: a pure function of the plan, the rules and the run as of `at`.

Every figure here is invented. TZ is UTC unless a test sets it.
"""
import json
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest

from desk import checks, rules
from desk.checks import Context, Other
from desk.model import Plan

AT = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)     # a Monday
P = Plan("ACME", "long", "stock", 1.0, 100.0, 98.0, 104.0, 10)   # risk 2, reward 4


def run(values, plan=P, **ctx):
    return checks.run_checks(plan, rules.Rules(values, ("15m", "1h")),
                             Context(at=AT, **{"currency": "USD", **ctx}))


def one(values, plan=P, **ctx):
    (r,) = run(values, plan, **ctx)
    return r


def other(id="t1", *, entered=AT - timedelta(hours=2), seq=0, ended=None, exited=False,
          r=None, cash=None, ticker="ACME"):
    return Other(id=id, ticker=ticker, entered=entered, seq=seq, ended=ended, exited=exited,
                 r=r, cash=cash)


def exit_(ago, r, id="t1", **kw):
    return other(id, ended=AT - ago, exited=True, r=r, cash=None if r is None else r * 20, **kw)


def test_min_reward_risk():
    assert (one({"min_reward_risk": 2}).ok, one({"min_reward_risk": 2}).figure) == (True, 2.0)
    assert one({"min_reward_risk": 2.5}).ok is False
    assert one({"min_reward_risk": 2}, replace(P, target=None)).ok is None
    assert "risk side" in one({"min_reward_risk": 2}, replace(P, stop=101.0)).says
    grid = replace(P, entry=90.00, stop=89.96, target=90.08)
    assert one({"min_reward_risk": 2}, grid).ok is True           # float residue forgiven


def test_max_risk_per_trade_names_the_largest_size():
    r = one({"max_risk_per_trade": {"USD": 20}})
    assert (r.ok, r.figure, r.unit) == (True, 20.0, "USD") and "up to 10 shares" in r.says
    r = one({"max_risk_per_trade": {"USD": 15}})
    assert r.ok is False and "up to 7 shares" in r.says
    assert "no size passes" in one({"max_risk_per_trade": {"USD": 1}}).says
    assert one({"max_risk_per_trade": {"USD": 20}}, currency=None).ok is None
    assert one({"max_risk_per_trade": {"CAD": 20}}).ok is None    # the run is in USD
    fut = replace(P, instrument="future", point_value=100.0, size=1)   # risk 200
    assert "up to 1 contract" in one({"max_risk_per_trade": {"USD": 250}}, fut).says


def test_a_figure_too_large_to_state_reads_unknown():
    r = one({"max_risk_per_trade": {"USD": 20}}, replace(P, size=10 ** 308))   # risk is inf
    assert (r.ok, r.figure, r.limit) == (None, None, "20 USD")
    assert "too large to state" in r.says
    # a vanishing risk per contract: the size that passes overflows on the way
    tiny = replace(P, instrument="future", point_value=1e-308)
    r = one({"max_risk_per_trade": {"USD": 20}}, tiny)
    assert (r.name, r.ok, r.figure) == ("max_risk_per_trade", None, None)
    assert "too large to state" in r.says and json.loads(r.limit) == {"USD": 20}
    assert json.dumps(asdict(r), allow_nan=False)
    assert one({"max_risk_per_trade": {"USD": 20}}).figure == 20.0   # a finite one is untouched


def test_required_orders_and_allow_lists():
    assert one({"stop_required": True}, replace(P, stop=None)).ok is False
    assert one({"stop_required": False}, replace(P, stop=None)).ok is True
    assert one({"target_required": True}, replace(P, target=97.0)).ok is False   # wrong side
    assert one({"sides": ["long"]}).ok and not one({"sides": ["long"]}, replace(P, side="short")).ok
    assert one({"instruments": ["stock", "etf"]}, replace(P, instrument="future")).ok is False


def test_max_open_trades_reads_the_run_as_of_at():
    assert one({"max_open_trades": 1}, others=(other(),)).ok is False
    assert one({"max_open_trades": 1}, others=(other(ended=AT),)).ok is True        # ended at `at`
    assert one({"max_open_trades": 1}, others=(other(entered=AT + timedelta(1)),)).ok is True
    tie = other(entered=AT, seq=3)
    assert one({"max_open_trades": 1}, seq=4, others=(tie,)).ok is False   # recorded first: counted
    assert one({"max_open_trades": 1}, seq=2, others=(tie,)).ok is True
    assert one({"max_open_trades": 1}, others=(tie,)).ok is False   # check_only: after every other


def test_an_exit_without_r_withholds_only_when_its_cash_could_move_the_total():
    dll = {"daily_loss_limit": {"R": 2}}

    def no_r(cash):
        return other("n", ended=AT - timedelta(1 / 24), exited=True, r=None, cash=cash)

    assert one(dll, others=(exit_(timedelta(hours=2), -2.5), no_r(-40))).ok is False
    assert one(dll, others=(exit_(timedelta(hours=2), -2.5), no_r(40))).ok is None
    assert one(dll, others=(exit_(timedelta(hours=2), -1.0), no_r(-40))).ok is None
    r = one(dll, others=(exit_(timedelta(hours=2), -1.0, "a"), no_r(40)))
    assert (r.ok, r.figure) == (True, -1.0)
    assert "known realised R" in r.says and "n exited with no R and +40 in cash" in r.says
    assert "the day's total is at least -1.00" in r.says


def test_daily_loss_limit_counts_the_days_realised_r():
    dll = {"daily_loss_limit": {"R": 2}}
    two = (exit_(timedelta(hours=3), -1.5, "a"), exit_(timedelta(hours=2), -0.5, "b"))
    r = one(dll, others=two)
    assert (r.ok, r.figure) == (True, -2.0)                     # exactly at the limit: kept
    worse = (exit_(timedelta(hours=3), -1.5, "a"), exit_(timedelta(hours=2), -1.0, "b"))
    assert one(dll, others=worse).ok is False
    assert one(dll, others=(exit_(timedelta(days=1), -3.0),)).figure == 0.0      # yesterday
    later = other(ended=AT + timedelta(1), exited=True, r=-3.0, cash=-60)
    assert one(dll, others=(later,)).figure == 0.0


def test_max_trades_per_day_and_consecutive_losses():
    day = {"max_trades_per_day": 2}
    assert one(day, others=(other("a"), other("b", seq=1))).figure == 3.0
    yesterday = other("a", entered=AT - timedelta(hours=16))
    assert one(day, others=(yesterday,)).ok is True
    mcl = {"max_consecutive_losses": 2}

    def loss(h, id):
        return exit_(timedelta(hours=h), -1.0, id)

    def win(h, id):
        return exit_(timedelta(hours=h), 1.0, id)

    def flat(h, id):
        return exit_(timedelta(hours=h), 0.0, id)

    assert one(mcl, others=(loss(3, "a"), loss(2, "b"))).ok is False
    assert one(mcl, others=(loss(3, "a"), flat(2, "b"), loss(1, "c"))).ok is False   # skipped
    assert one(mcl, others=(loss(3, "a"), win(2, "b"), loss(1, "c"))).figure == 1.0


def test_cooldown_after_loss():
    cool = {"cooldown_after_loss": {"minutes": 30}}
    assert (one(cool, others=(exit_(timedelta(minutes=20), -1.0),)).ok,
            one(cool, others=(exit_(timedelta(minutes=20), -1.0),)).figure) == (False, 20.0)
    assert one(cool, others=(exit_(timedelta(minutes=30), -1.0),)).ok is True
    assert one(cool, others=(exit_(timedelta(minutes=5), 1.0),)).ok is True     # a win
    same = {"cooldown_after_loss": {"minutes": 30, "same_ticker": True}}
    assert one(same, others=(exit_(timedelta(minutes=5), -1.0, ticker="WXYZ"),)).ok is True
    assert one(same, others=(exit_(timedelta(minutes=5), -1.0, ticker="ACME"),)).ok is False
    assert one(cool).figure is None and one(cool).ok is True


def test_trading_hours_in_the_system_timezone(monkeypatch):
    monkeypatch.setenv("TZ", "America/Toronto")
    time.tzset()
    hours = {"trading_hours": {"start": "09:45", "end": "11:30",
                               "days": ["mon", "tue", "wed", "thu", "fri"]}}

    def at(h, m, s=0, d=2):   # EST is UTC-5 on 2026-03-02
        return datetime(2026, 3, d, h, m, s, tzinfo=UTC)

    for when, ok in ((at(15, 0), True), (at(16, 30), False), (at(14, 44, 59), False),
                     (at(15, 0, d=7), False), (at(14, 45), True)):          # at start: inside
        assert checks.run_checks(P, rules.Rules(hours, ("15m",)), Context(at=when))[0].ok is ok


def test_indicators_use_the_agents_value():
    rsi = {"rsi": {"length": 14, "long": {"at_most": 30}}}
    r = one(rsi, market={"rsi_14_15m": 31.0})
    assert (r.ok, r.figure, r.name) == (False, 31.0, "rsi_14_15m")
    missing = one(rsi)
    assert missing.ok is None
    assert missing.says == "supply rsi_14_15m, read off the chart at the bar acted on"
    short = one(rsi, replace(P, side="short"))                    # no comparison for a short
    assert (short.ok, short.figure) == (True, None) and "no comparison for a short" in short.says


def test_stop_atr_ema_macd_and_facts():
    atr = {"stop_atr": {"between": [1.0, 3.0]}}
    assert one(atr, market={"atr_14_15m": 1.0}).figure == 2.0
    assert one(atr, market={"atr_14_15m": 0.0}).ok is None
    ema = {"ema": {"length": 200, "timeframe": "1h", "long": "above"}}
    assert one(ema, market={"ema_200_1h": 101.0, "close_1h": 102.0}).figure == 1.0
    assert one(ema, market={"ema_200_1h": 101.0, "close_1h": 101.0}).ok is False      # strict
    assert one({"macd": {"at_least": 0}}, market={"macd_hist_12_26_9_15m": -0.2}).ok is False
    facts = {"facts": {"rvol": {"at_least": 2}}}
    assert one(facts, market={"rvol": 2.5}).ok is True and one(facts).ok is None


def test_a_stored_rule_the_library_does_not_know_reads_unknown():
    rs = run({"sides": ["long"], "feels_right": "strongly"})
    assert [(r.name, r.ok) for r in rs] == [("sides", True), ("feels_right", None)]
    assert "no rule named feels_right" in rs[1].says


def test_each_rule_applies_at_its_edges():
    atr = {"stop_atr": {"between": [1.0, 3.0]}}
    assert one(atr, market={"atr_14_15m": 1.0}).ok is True                 # 2.0 inside
    assert one(atr, market={"atr_14_15m": 0.5}).ok is False                # 4.0 above
    assert one(atr, market={"atr_14_15m": 2.1}).ok is False                # under 1 below
    assert one(atr, market={"atr_14_15m": -1.0}).ok is None
    facts = {"facts": {"rvol": {"at_least": 2}}}
    assert one(facts, market={"rvol": float("nan")}).ok is None
    ema = {"ema": {"length": 200, "timeframe": "1h", "short": "below"}}
    flat = {"ema_200_1h": 101.0, "close_1h": 101.0}
    assert one(ema, replace(P, side="short"), market=flat).ok is False        # strict
    assert one({"max_trades_per_day": 2}, others=(other("a"),)).ok is True    # at the limit


def test_record_rules_read_order_scope_and_kind():
    mcl = {"max_consecutive_losses": 2}
    old_win = exit_(timedelta(hours=3), 1.0, "w")
    losses = (exit_(timedelta(hours=2), -1.0, "a"), exit_(timedelta(hours=1), -1.0, "b"))
    assert one(mcl, others=(old_win, *losses)).ok is False          # newest first: win is older
    cancelled = other("c", ended=AT - timedelta(minutes=30))
    assert (one(mcl, others=(cancelled,)).ok, one(mcl, others=(cancelled,)).figure) == (True, 0.0)
    assert one({"daily_loss_limit": {"R": 2}}, others=(cancelled,)).figure == 0.0
    cool = {"cooldown_after_loss": {"minutes": 30}}
    two = (exit_(timedelta(minutes=50), -1.0, "a"), exit_(timedelta(minutes=10), -1.0, "b"))
    assert (one(cool, others=two).ok, one(cool, others=two).figure) == (False, 10.0)
    later = other("z", ended=AT + timedelta(minutes=10), exited=True, r=-1.0, cash=-20.0)
    assert (one(cool, others=(later,)).ok, one(cool, others=(later,)).figure) == (True, None)


def test_results_come_in_library_order_and_round_trip_through_json():
    rs = run({"facts": {"rvol": {"at_least": 2}}, "sides": ["long"], "min_reward_risk": 2},
             market={"rvol": 3.0})
    assert [r.name for r in rs] == ["min_reward_risk", "sides", "rvol"]
    assert all(checks.Result(**json.loads(json.dumps(asdict(r)))) == r for r in rs)
    assert run({}) == []


class _ReadsOthers:
    def __iter__(self):
        raise AssertionError("reads ctx.others")


def test_record_names_exactly_the_rules_that_read_the_other_trades():
    settings = {
        "min_reward_risk": 2, "max_risk_per_trade": {"USD": 20}, "stop_required": True,
        "target_required": True, "sides": ["long"], "instruments": ["stock"],
        "max_open_trades": 1, "max_trades_per_day": 1, "max_consecutive_losses": 1,
        "daily_loss_limit": {"R": 1}, "cooldown_after_loss": {"minutes": 30},
        "trading_hours": {"start": "09:30", "end": "16:00"},
    }
    assert set(settings) == set(checks._RULES)
    ctx = Context(at=AT, currency="USD", others=_ReadsOthers())
    for name, fn in checks._RULES.items():
        if name in checks.RECORD:
            with pytest.raises(AssertionError, match="reads ctx.others"):
                fn(P, settings[name], ctx)
        else:
            fn(P, settings[name], ctx)   # a rule missing from RECORD would read others unseen
