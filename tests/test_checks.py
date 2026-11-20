"""Tests for the number checks: one per check type, plus the carried requirements.

Every figure here is invented (tests/invented.py and the arithmetic written into each test).
A check states a fact and withholds when an input is missing, so most tests assert one of
three things: the figure, `ok is None` with the missing input named, or the absence of a grade.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import invented
import pytest

from desk import checks, model, rules, store

FIXTURE = Path(__file__).parent / "fixtures" / "rules_invented.md"

NOW = dt.datetime(2026, 5, 4, 15, 30, tzinfo=dt.UTC)
TICKER, OTHER, THIRD = invented.TICKERS


# ----------------------------------------------------------------- helpers ---
def one(results: list[checks.Result]) -> checks.Result:
    assert len(results) == 1, f"expected one result, got {[r.name for r in results]}"
    return results[0]


def named(results: list[checks.Result], name: str) -> checks.Result:
    [found] = [r for r in results if r.name == name]
    return found


def rules_with(groups=None, unchecked=None, **numbers) -> rules.Rules:
    """A Rules object for one scope, built from invented values only."""
    return rules.Rules(
        numbers={k: str(v) for k, v in numbers.items()},
        ask=(),
        groups=groups or {},
        changes=(),
        hash="0123456789ab",
        path=FIXTURE,
        sections_used=("All",),
        sections_wanted=("All",),
        sections_missing=(),
        unchecked=dict(unchecked or {}),
    )


def plan(**over) -> model.Plan:
    base = dict(
        ticker=TICKER,
        side="long",
        instrument="stock",
        point_value=1.0,
        currency="USD",
        entry=invented.PRICES[TICKER],
        stop=95.0,
        target=110.0,
        size=10,
        horizon="2w",
        setup="test-setup",
        thesis="test thesis",
        account="paper",
        run_id=None,
    )
    return model.Plan(**{**base, **over})


PLAN = plan()
SHORT_PLAN = plan(side="short", entry=100.0, stop=105.0, target=90.0)
# A future at 100 per point with its stop 1.5 points away: 2000.0 -> 1998.5.
FUTURE_PLAN = plan(
    instrument="future",
    point_value=invented.FUTURE_POINT_VALUE,
    entry=2000.0,
    stop=1998.5,
    target=2010.0,
    size=1,
)

CTX = checks.Context(now=NOW)


def enter(con, trade_id, ticker, *, size=10, side="long", exit_size=None, close=False):
    """Record an entered trade, optionally exited and closed, and return its id."""
    store.add_trade(
        con,
        trade_id=trade_id,
        account="paper",
        run_id=None,
        ticker=ticker,
        instrument="stock",
        point_value=1.0,
        currency="USD",
        side=side,
    )
    store.append(
        con,
        kind="entered",
        actor="owner",
        source="chat",
        trade_id=trade_id,
        payload={
            "entry": 100.0,
            "stop": 95.0,
            "target": 110.0,
            "size": size,
            "horizon": "2w",
            "setup": "test-setup",
            "thesis": "test thesis",
        },
    )
    if exit_size is not None:
        store.append(
            con,
            kind="exited",
            actor="owner",
            source="chat",
            trade_id=trade_id,
            payload={"price": 105.0, "size": exit_size},
        )
    if close:
        store.append(con, kind="closed", actor="owner", source="chat", trade_id=trade_id,
                     payload={})
    return trade_id


@pytest.fixture
def con(tmp_path):
    db = tmp_path / "test.db"
    store.create(db)
    return store.open(db)


# -------------------------------------------------------- reward to risk ---
def test_reward_to_risk_is_signed_for_a_short():
    """A short with entry 100, stop 105, target 90 has reward:risk 2, not -2."""
    r = one(checks.run_checks(SHORT_PLAN, rules_with(min_reward_risk="2"), CTX))
    assert r.figure == pytest.approx(2.0) and r.ok is True


def test_an_inverted_short_never_passes_on_an_absolute_value():
    """Entry 100, stop 95, target 110 is not a short at all: its stop is on the reward side.

    Dividing two absolute values would pass it at 2.0. It used to read -2.0 and breach, but a
    stop on the wrong side has no risk distance, so there is no 1R to state the reward in:
    withheld, saying why, and stop_required states the wrong side as a breach.
    """
    inverted = plan(side="short", entry=100.0, stop=95.0, target=110.0)
    r = one(checks.run_checks(inverted, rules_with(min_reward_risk="2"), CTX))
    assert r.ok is None and r.figure is None
    assert "not the risk side for a short" in r.says
    stop = one(checks.run_checks(inverted, rules_with(stop_required="yes"), CTX))
    assert stop.ok is False


def test_ok_never_contradicts_the_digits_the_fact_prints():
    """90.00 / 89.96 / 90.08 divides to 1.9999999999996447: a 2:1 trade, not a breach."""
    cents = plan(entry=90.0, stop=89.96, target=90.08)
    r = one(checks.run_checks(cents, rules_with(min_reward_risk="2"), CTX))
    assert "2.00" in r.says
    assert r.ok is True, "a breach must never be reported under a figure reading 2.00"


# ------------------------------------------------------- risk per trade ---
def test_risk_per_trade_multiplies_by_the_point_value():
    """A future at 100 per point, size 1, stop 1.5 points away, risks 150 -- not 1.5."""
    r = one(checks.run_checks(FUTURE_PLAN, rules_with(max_risk_per_trade="100 USD"), CTX))
    assert r.figure == pytest.approx(150.0) and r.ok is False
    assert "150" in r.says and "100" in r.says, "the fact states both figures"


def test_risk_per_trade_converts_to_the_limits_currency():
    """A USD plan against a CAD limit: 10 x 5 x 1 = 50 USD, converted at the given rate."""
    rate = invented.FX_USD_CAD
    def fx(src, dst):
        return rate if (src, dst) == ("USD", "CAD") else None

    ctx = checks.Context(now=NOW, fx=fx)
    r = one(checks.run_checks(PLAN, rules_with(max_risk_per_trade="100 CAD"), ctx))
    assert r.figure == pytest.approx(50.0 * rate) and r.unit == "CAD"
    assert r.ok is True

    # No rate: the figure is withheld rather than compared across two currencies at 1:1.
    blind = one(checks.run_checks(PLAN, rules_with(max_risk_per_trade="100 CAD"), CTX))
    assert blind.ok is None and blind.figure is None
    assert "USD" in blind.says and "CAD" in blind.says, "the says names the rate it wanted"


def test_a_limit_without_a_currency_is_withheld_not_assumed():
    """`100` could be either sleeve's currency, and guessing sizes a trade wrong."""
    r = one(checks.run_checks(PLAN, rules_with(max_risk_per_trade="100"), CTX))
    assert r.ok is None and "currency" in r.says


# ---------------------------------------------------------------- sizing ---
def test_size_is_suggested_rounded_down_so_the_limit_is_never_exceeded():
    """100 USD over a 3-point stop is 33.3 shares: 33 risks 99, 34 would risk 102."""
    tight = plan(stop=97.0, size=40)
    rs = rules_with(size="computed", max_risk_per_trade="100 USD")
    assert checks.suggest_size(rs, tight, CTX) == 33

    distance = tight.entry - tight.stop
    assert 33 * distance * tight.point_value <= 100
    assert 34 * distance * tight.point_value > 100, "rounding up would exceed the limit"

    r = named(checks.run_checks(tight, rs, CTX), "size")
    assert r.figure == pytest.approx(33.0) and r.ok is False, "the plan of 40 is above 33"
    assert "40" in r.says and "33" in r.says


def test_the_size_and_the_risk_checks_cannot_disagree_at_the_boundary():
    """Two formulas over one budget: the suggested size is exactly the largest one inside it."""
    rs = rules_with(size="computed", max_risk_per_trade="100 USD")
    for planned, inside in ((33, True), (34, False)):
        results = checks.run_checks(plan(stop=97.0, size=planned), rs, CTX)
        assert named(results, "size").ok is inside
        assert named(results, "max_risk_per_trade").ok is inside, "the two must agree"

    # And on a cent grid, where 100 / (90.00 - 89.96) is 2499.9999999996094: 2500 shares risk
    # exactly the limit, so neither check may call it a breach.
    cents = plan(entry=90.0, stop=89.96, size=2500)
    results = checks.run_checks(cents, rs, CTX)
    assert named(results, "max_risk_per_trade").ok is True
    assert named(results, "size").ok is True, "flooring float residue would invent a breach"
    assert checks.suggest_size(rs, cents, CTX) == 2500


def test_a_size_is_not_suggested_without_a_risk_rule_to_size_against():
    r = one(checks.run_checks(PLAN, rules_with(size="computed"), CTX))
    assert r.ok is None and "max_risk_per_trade" in r.says
    assert checks.suggest_size(rules_with(size="computed"), PLAN, CTX) is None


# -------------------------------------------------------- a missing stop ---
def test_a_missing_stop_makes_the_stop_rules_uncomputable_not_false():
    """ok is None and `says` explains what is missing. A missing input is not a breach."""
    stopless = plan(stop=None)
    rs = rules_with(
        min_reward_risk="2",
        max_risk_per_trade="100 USD",
        size="computed",
        stop_headroom_candles="1",
        stop_required="yes",
    )
    results = checks.run_checks(stopless, rs, CTX)

    for name in ("min_reward_risk", "max_risk_per_trade", "size", "stop_headroom_candles"):
        r = named(results, name)
        assert r.ok is None, f"{name} must withhold, not report a breach"
        assert r.figure is None
        assert "stop" in r.says, f"{name} must name the missing input"

    # stop_required is the one rule the absence ANSWERS rather than blocks.
    required = named(results, "stop_required")
    assert required.ok is False and "no stop" in required.says

    assert checks.suggest_size(rs, stopless, CTX) is None


def test_a_stop_or_target_on_the_wrong_side_of_the_entry_is_reported():
    rs = rules_with(stop_required="yes", target_required="yes")
    high_stop = named(checks.run_checks(plan(stop=105.0), rs, CTX), "stop_required")
    assert high_stop.ok is False and "above" in high_stop.says

    low_target = named(checks.run_checks(plan(target=90.0), rs, CTX), "target_required")
    assert low_target.ok is False and "below" in low_target.says

    good_side = checks.run_checks(PLAN, rs, CTX)
    assert named(good_side, "stop_required").ok is True
    assert named(good_side, "target_required").ok is True


def test_a_misspelt_stop_required_withholds_rather_than_switching_the_rule_off():
    """`ture` must not be the thing that turns the stop requirement off. `no` still means no."""
    stopless = plan(stop=None)
    for typo in ("ture", "definitely", ""):
        r = named(checks.run_checks(stopless, rules_with(stop_required=typo), CTX),
                  "stop_required")
        assert r.ok is None, f"{typo!r} is neither a yes nor a no"
        assert repr(typo) in r.says, "the value is quoted back so the owner can fix it"

    off = named(checks.run_checks(stopless, rules_with(stop_required="no"), CTX), "stop_required")
    assert off.ok is True, "a rule the owner switched off is not a breach"


# ----------------------------------------------------------- open trades ---
def test_max_open_trades_counts_the_stores_open_trades(con):
    empty = checks.Context(now=NOW, open_trades=model.open_trades(con))
    r = one(checks.run_checks(PLAN, rules_with(max_open_trades="1"), empty))
    assert r.figure == pytest.approx(0.0) and r.ok is True

    enter(con, "trade-a", OTHER)
    ctx = checks.Context(now=NOW, open_trades=model.open_trades(con))
    r = one(checks.run_checks(PLAN, rules_with(max_open_trades="1"), ctx))
    assert r.figure == pytest.approx(1.0) and r.ok is False
    # The sentence must support the breach: "1 open against a limit of 1" would not.
    assert "this would make 2" in r.says and "limit of 1" in r.says


def test_a_fully_exited_trade_with_no_close_event_is_not_counted_as_open(con):
    """Counting by status would let a finished trade block the next one."""
    enter(con, "trade-a", OTHER, size=5, exit_size=5)
    open_now = model.open_trades(con)
    assert [t.status for t in open_now] == ["open"], "the fold keeps it open until it is closed"
    assert open_now[0].size_open == 0, "but it holds nothing, so it occupies no slot"

    ctx = checks.Context(now=NOW, open_trades=open_now)
    r = one(checks.run_checks(PLAN, rules_with(max_open_trades="1"), ctx))
    assert r.figure == pytest.approx(0.0) and r.ok is True


def test_a_negative_open_size_is_withheld_rather_than_counted(con):
    """An over-exit is recorded, not clamped, so no figure may be derived from it."""
    enter(con, "trade-a", OTHER, size=5, exit_size=7)
    broken = model.open_trades(con)
    assert broken[0].size_open == -2, "the fold leaves the corruption visible"

    ctx = checks.Context(now=NOW, open_trades=broken)
    rs = rules_with(groups={"tech": (TICKER, OTHER)}, max_open_trades="1",
                    no_open_trade_in_group="tech")
    for r in checks.run_checks(PLAN, rs, ctx):
        assert r.ok is None, f"{r.name} must not count a negative open size"
        assert "trade-a" in r.says and "negative" in r.says

    # Nor may a plan carrying such a size be risked or sized off.
    over_exited = plan(size=-2)
    risk_rules = rules_with(max_risk_per_trade="100 USD", size="computed")
    for r in checks.run_checks(over_exited, risk_rules, CTX):
        assert r.ok is None, f"{r.name} must not compute a figure from a negative size"
        assert "-2" in r.says


def test_no_open_trade_in_group_uses_the_groups_from_the_rules_file(con):
    parsed = rules.load(FIXTURE).for_scope("real", None, checks.CHECK_NAMES)
    assert parsed.groups["tech"] == (TICKER, OTHER), "the fixture's group, read from the file"
    rs = rules_with(groups=parsed.groups, no_open_trade_in_group="tech")

    enter(con, "trade-a", OTHER)
    ctx = checks.Context(now=NOW, open_trades=model.open_trades(con))

    same_group = one(checks.run_checks(plan(ticker=TICKER), rs, ctx))
    assert same_group.ok is False and same_group.figure == pytest.approx(1.0)
    assert "tech" in same_group.says and OTHER in same_group.says

    other_group = one(checks.run_checks(plan(ticker=THIRD), rs, ctx))
    assert other_group.ok is True, f"{THIRD} is in consumer, not tech"


def test_a_group_the_rules_file_never_defines_is_withheld():
    rs = rules_with(groups={"tech": (TICKER,)}, no_open_trade_in_group="tech, semis")
    r = one(checks.run_checks(PLAN, rs, CTX))
    assert r.ok is None and "semis" in r.says


# -------------------------------------------------------------- the clock ---
def test_cooldown_after_stop_measures_from_the_last_stopped_close():
    rs = rules_with(cooldown_after_stop="1 day, same ticker")

    inside = checks.Context(now=NOW, stopped_closes={TICKER: NOW - dt.timedelta(hours=5)})
    r = one(checks.run_checks(PLAN, rs, inside))
    assert r.figure == pytest.approx(5.0) and r.ok is False

    outside = checks.Context(now=NOW, stopped_closes={TICKER: NOW - dt.timedelta(hours=30)})
    assert one(checks.run_checks(PLAN, rs, outside)).ok is True

    # "same ticker": a stop on another ticker does not gate this one.
    elsewhere = checks.Context(now=NOW, stopped_closes={OTHER: NOW - dt.timedelta(hours=5)})
    assert one(checks.run_checks(PLAN, rs, elsewhere)).ok is True
    # ...but it does when the rule is not scoped to the ticker.
    any_ticker = one(checks.run_checks(PLAN, rules_with(cooldown_after_stop="1 day"), elsewhere))
    assert any_ticker.ok is False and any_ticker.figure == pytest.approx(5.0)

    # Not supplied at all is not the same as none on record.
    assert one(checks.run_checks(PLAN, rs, CTX)).ok is None
    assert one(checks.run_checks(PLAN, rs, checks.Context(now=NOW, stopped_closes={}))).ok is True

    # The scope does not depend on which side of the comma it was written.
    reversed_rule = rules_with(cooldown_after_stop="same ticker, 1 day")
    assert one(checks.run_checks(PLAN, reversed_rule, elsewhere)).ok is True


def test_max_session_length_measures_from_the_session_start():
    rs = rules_with(max_session_length="2h")
    over = checks.Context(now=NOW, session_started=NOW - dt.timedelta(hours=2, minutes=30))
    r = one(checks.run_checks(PLAN, rs, over))
    assert r.figure == pytest.approx(2.5) and r.ok is False and "2h" in r.says

    under = checks.Context(now=NOW, session_started=NOW - dt.timedelta(minutes=30))
    assert one(checks.run_checks(PLAN, rs, under)).ok is True
    assert one(checks.run_checks(PLAN, rs, CTX)).ok is None, "no session is not a pass"


def test_a_clock_that_disagrees_with_itself_is_withheld_not_passed():
    """Negative elapsed time is nonsense in either direction: neither a pass nor a breach."""
    ahead = checks.Context(
        now=NOW,
        session_started=NOW + dt.timedelta(hours=1),
        stopped_closes={TICKER: NOW + dt.timedelta(hours=1)},
    )
    rs = rules_with(max_session_length="2h", cooldown_after_stop="1 day, same ticker")
    for r in checks.run_checks(PLAN, rs, ahead):
        assert r.ok is None, f"{r.name} must not read a figure off a skewed clock"
        assert "in the future" in r.says and "clocks disagree" in r.says


def test_daily_loss_limit_sums_todays_closed_r():
    rs = rules_with(daily_loss_limit="-2R")
    over = checks.Context(now=NOW, closed_today_r=-2.5)
    r = one(checks.run_checks(PLAN, rs, over))
    assert r.figure == pytest.approx(-2.5) and r.ok is False
    assert "-2.50" in r.says and "-2R" in r.says

    under = checks.Context(now=NOW, closed_today_r=-1.0)
    assert one(checks.run_checks(PLAN, rs, under)).ok is True
    flat = checks.Context(now=NOW, closed_today_r=0.0)
    assert one(checks.run_checks(PLAN, rs, flat)).ok is True
    assert one(checks.run_checks(PLAN, rs, CTX)).ok is None, "an unknown total is not zero"


# -------------------------------------------------------------- the lists ---
def test_banned_catches_the_side_and_the_instrument():
    rs = rules_with(banned="short, leverage, options, futures")
    short_future = plan(
        side="short",
        instrument="future",
        point_value=invented.FUTURE_POINT_VALUE,
        entry=2000.0,
        stop=2001.5,
        target=1990.0,
        size=1,
    )
    r = one(checks.run_checks(short_future, rs, CTX))
    assert r.ok is False
    assert "short" in r.says and "future" in r.says, "both halves are named"

    assert one(checks.run_checks(PLAN, rs, CTX)).ok is True, "a long stock is on neither list"


def test_stop_headroom_and_volatility_are_uncomputable_without_bars():
    """Bars arrive in plan 3; until then these report ok=None, never a pass."""
    rs = rules_with(stop_headroom_candles="1", max_volatility_ratio="1.5")
    for r in checks.run_checks(PLAN, rs, CTX):
        assert r.ok is None and r.figure is None
        assert "bars" in r.says, f"{r.name} must say why it could not compute"

    # The arithmetic is here already, so plan 3 only has to supply the bars.
    with_bars = checks.Context(now=NOW, median_candle_range=2.0, volatility_ratio=1.2)
    results = checks.run_checks(PLAN, rs, with_bars)
    headroom = named(results, "stop_headroom_candles")
    assert headroom.figure == pytest.approx(2.5), "a 5-point stop over a 2-point candle"
    assert headroom.ok is True
    assert named(results, "max_volatility_ratio").figure == pytest.approx(1.2)
    assert named(results, "max_volatility_ratio").ok is True


# ------------------------------------------------------- the whole output ---
ALL_RULES = rules_with(
    groups={"tech": (TICKER, OTHER)},
    unchecked={"feels_right": "strongly"},
    min_reward_risk="2",
    max_risk_per_trade="100 USD",
    size="computed",
    stop_required="yes",
    target_required="yes",
    stop_headroom_candles="1",
    max_open_trades="1",
    no_open_trade_in_group="tech",
    cooldown_after_stop="1 day, same ticker",
    max_session_length="2h",
    daily_loss_limit="-2R",
    banned="short, leverage, options, futures",
    max_volatility_ratio="1.5",
)

FULL_CTX = checks.Context(
    now=NOW,
    open_trades=[],
    session_started=NOW - dt.timedelta(hours=1),
    closed_today_r=-0.5,
    median_candle_range=2.0,
    volatility_ratio=1.2,
    stopped_closes={},
)


def test_an_unchecked_rule_is_asked_never_estimated():
    r = one(checks.run_checks(PLAN, rules_with(unchecked={"feels_right": "strongly"}), CTX))
    assert r.name == "feels_right" and r.ok is None and r.figure is None
    # The says field includes the rule name and value so a misspelling is visible
    expected = "feels_right: strongly — no automatic check for this one; I'll ask you each time"
    assert r.says == expected
    assert r.limit == "strongly", "the value is carried so the desk can quote the rule back"


def test_every_rule_in_the_file_gets_a_result_in_the_tables_order():
    results = checks.run_checks(PLAN, ALL_RULES, FULL_CTX)
    assert [r.name for r in results] == [
        "min_reward_risk",
        "max_risk_per_trade",
        "size",
        "stop_required",
        "target_required",
        "stop_headroom_candles",
        "max_open_trades",
        "no_open_trade_in_group",
        "cooldown_after_stop",
        "max_session_length",
        "daily_loss_limit",
        "banned",
        "max_volatility_ratio",
        "feels_right",
    ]
    assert len(checks.CHECK_NAMES) == 13


def test_a_result_never_contains_a_grade_or_a_score():
    for ctx in (FULL_CTX, CTX):  # computed and withheld both
        for r in checks.run_checks(PLAN, ALL_RULES, ctx):
            assert not re.search(r"\b(good|bad|score|grade|well done|poor)\b", r.says, re.I)
            assert r.says and not r.says.endswith(("!", "?")), "a fact, stated plainly"


# ---------------------------------------------------------------------------------------------
# From the 11-11 adversarial hunt: rules that passed when they should not have.


def test_a_near_miss_is_a_breach_and_prints_the_digits_that_show_it():
    """1.996 rounded to 2.00 and passed a floor of 2; only float residue may pass."""
    near = plan(entry=100.0, stop=95.0, target=109.98)  # 9.98 / 5 = 1.996
    r = one(checks.run_checks(near, rules_with(min_reward_risk="2"), CTX))
    assert r.ok is False
    assert "1.996" in r.says, "a breach must not print as 2.00"


def test_a_session_three_minutes_over_its_limit_is_over_it():
    ctx = checks.Context(now=NOW, session_started=NOW - dt.timedelta(hours=2, minutes=3))
    r = one(checks.run_checks(PLAN, rules_with(max_session_length="2h"), ctx))
    assert r.ok is False
    assert "2.05" in r.says


@pytest.mark.parametrize("value", ["1:2", "2,5", "2 or 3", "2-3", "about 2"])
def test_a_rule_value_that_is_not_one_number_is_withheld_not_read_loosely(value):
    """'1:2' read as a floor of 1 and '2,5' as 2: both looser than the owner meant."""
    r = one(checks.run_checks(PLAN, rules_with(min_reward_risk=value), CTX))
    assert r.ok is None
    assert value in r.says


@pytest.mark.parametrize(("value", "limit"), [("1,000 USD", 1000.0), ("100 usd", 100.0)])
def test_an_amount_with_a_thousands_separator_or_lower_case_currency_is_read(value, limit):
    r = one(checks.run_checks(PLAN, rules_with(max_risk_per_trade=value), CTX))
    assert r.ok is True and r.unit == "USD"


def test_a_limit_naming_two_currencies_is_withheld():
    r = one(checks.run_checks(PLAN, rules_with(max_risk_per_trade="100 USD or CAD"), CTX))
    assert r.ok is None


@pytest.mark.parametrize(("value", "hours"), [("2h30m", 2.5), ("1 day 12 hours", 36.0)])
def test_a_duration_in_two_parts_is_read_whole(value, hours):
    """'2h30m' read as 2h: a cooldown 30 minutes shorter than written."""
    ctx = checks.Context(now=NOW, stopped_closes={TICKER: NOW - dt.timedelta(hours=hours - 0.1)})
    r = one(checks.run_checks(PLAN, rules_with(cooldown_after_stop=value), ctx))
    assert r.ok is False, f"{hours - 0.1} h is inside a cooldown of {value}"


@pytest.mark.parametrize("stop", [105.0, 100.0])
def test_a_stop_that_is_not_on_the_risk_side_computes_no_risk_and_no_reward_to_risk(stop):
    """A long with its stop ABOVE the entry passed min_reward_risk and max_risk_per_trade:
    the distance was taken as a risk although no loss stops there."""
    wrong = plan(entry=100.0, stop=stop, target=110.0)
    rs = rules_with(min_reward_risk="2", max_risk_per_trade="1000 USD", size="computed")
    for r in checks.run_checks(wrong, rs, CTX):
        assert r.ok is None, f"{r.name} must withhold, said: {r.says}"
    assert checks.suggest_size(rs, wrong, CTX) is None


def test_a_price_that_is_not_a_number_never_passes_a_rule_that_reads_it():
    """NaN compares false to everything, so `ratio >= limit` and `risk <= limit` could each
    be decided by which way round the comparison happened to be written."""
    nan = float("nan")
    rs = rules_with(min_reward_risk="2", max_risk_per_trade="1000 USD", size="computed")
    for bad in (plan(entry=nan), plan(stop=nan)):
        for r in checks.run_checks(bad, rs, CTX):
            assert r.ok is None, f"{r.name} on a NaN entry or stop said: {r.says}"
    # The target is read by reward-to-risk alone; the risk does not depend on it.
    r = named(checks.run_checks(plan(target=nan), rs, CTX), "min_reward_risk")
    assert r.ok is None


@pytest.mark.parametrize("entry", [10.0, 37.13, 90.0, 251.7, 2000.0])
@pytest.mark.parametrize("distance", [0.01, 0.04, 0.37, 1.5, 3.0])
@pytest.mark.parametrize("budget", ["99 USD", "100 USD", "137.5 USD", "200 CAD"])
@pytest.mark.parametrize("point_value", [1.0, 2.0, 50.0])
def test_the_suggested_size_is_always_the_largest_size_max_risk_passes(
        entry, distance, budget, point_value):
    """size floored a quotient rounded to 2 decimals, so it could allow 5 shares that
    max_risk_per_trade then called a breach. Now one risk function serves both."""
    def fx(src, dst):
        return invented.FX_USD_CAD if (src, dst) == ("USD", "CAD") else None
    ctx = checks.Context(now=NOW, fx=fx)
    rs = rules_with(size="computed", max_risk_per_trade=budget)
    base = plan(entry=entry, stop=round(entry - distance, 2), point_value=point_value)
    n = checks.suggest_size(rs, base, ctx)
    assert n is not None
    risk = rules_with(max_risk_per_trade=budget)
    if n > 0:
        at_n = one(checks.run_checks(plan(**{**base.__dict__, "size": n}), risk, ctx))
        assert at_n.ok is True, f"size allows {n} but max_risk says {at_n.says}"
    above = one(checks.run_checks(plan(**{**base.__dict__, "size": n + 1}), risk, ctx))
    assert above.ok is False, f"size allows only {n} but max_risk passes {n + 1}"


def test_group_tickers_are_matched_whatever_their_case(con):
    rs = rules_with(groups={"tech": ("acme", "wxyz")}, no_open_trade_in_group="Tech")
    enter(con, "trade-a", OTHER)
    ctx = checks.Context(now=NOW, open_trades=model.open_trades(con))
    r = one(checks.run_checks(plan(ticker=TICKER), rs, ctx))
    assert r.ok is False, "a lower-case group used to let its own tickers through"


def test_a_group_listing_a_continuous_future_matches_the_futures_root():
    rs = rules_with(groups={"metals": ("GC1!", "SI=F")}, no_open_trade_in_group="metals")
    ctx = checks.Context(now=NOW, open_trades=[])
    r = one(checks.run_checks(plan(ticker="GC", instrument="future", point_value=100.0), rs, ctx))
    assert r.says == "no open trade in metals, which GC is in", r.says


@pytest.mark.parametrize("value", [
    "shorts", "no shorts", "short selling", "shorts and futures", "Shorting / options",
])
def test_banned_reads_the_ways_a_short_ban_is_written(value):
    r = one(checks.run_checks(SHORT_PLAN, rules_with(banned=value), CTX))
    assert r.ok is False, f"{value!r} let a short through"


def test_a_banned_list_with_a_word_it_cannot_read_never_passes_silently():
    r = one(checks.run_checks(PLAN, rules_with(banned="shorts, crypto"), CTX))
    assert r.ok is None
    assert "crypto" in r.says
    # ...but a ban it CAN see is still a breach.
    hit = one(checks.run_checks(SHORT_PLAN, rules_with(banned="shorts, crypto"), CTX))
    assert hit.ok is False
