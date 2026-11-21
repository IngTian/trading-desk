"""Tests for reading and validating a strategy's rules block. Every figure is invented."""
import datetime as dt

import pytest

from desk import rules
from desk.errors import Refused


def block(body, before="---\nname: X\n---\n\n## Rules\n", fence="```"):
    return f"{before}{fence}rules\n{body}{fence}\n"


def test_the_block_is_read_as_toml():
    got = rules.read_block(block(
        'timeframes = ["1h", "15m"]\nmin_reward_risk = 2\n'
        'max_risk_per_trade = { USD = 150, CAD = 200 }\n'
        '[rsi]\nlength = 14\nlong = { at_most = 30 }\n'), "strategy")
    assert got == {"timeframes": ["1h", "15m"], "min_reward_risk": 2,
                   "max_risk_per_trade": {"USD": 150, "CAD": 200},
                   "rsi": {"length": 14, "long": {"at_most": 30}}}


def test_a_toml_error_names_the_file_line():
    # the fence is line 6, so block line 2 is file line 8
    with pytest.raises(Refused,
                       match=r"strategy:8: the rules block is not valid TOML: Invalid value"):
        rules.read_block(block('timeframes = ["1h"]\nmin_reward_risk = \n'), "strategy")


def test_an_error_at_the_end_of_the_document_names_the_closing_fence():
    with pytest.raises(Refused, match=r"strategy:8: the rules block is not valid TOML"):
        rules.read_block(block('timeframes = ["1h"\n'), "strategy")


def test_the_old_line_format_is_refused_with_the_toml_form():
    with pytest.raises(Refused,
                       match=r'from 0\.7 the rules block is TOML: write timeframes = \["1h"'):
        rules.read_block(block("timeframes: 1h\nmin_reward_risk: 2\n"), "strategy")


def test_a_key_or_table_set_twice_is_refused():
    for body in ("max_open_trades = 1\nmax_open_trades = 2\n", "[rsi]\nat_most = 30\n[rsi]\n"):
        with pytest.raises(Refused, match="not valid TOML"):
            rules.read_block(block(body), "strategy")


@pytest.mark.parametrize("fence", ["~~~", "````"])
def test_other_markdown_fences_are_read(fence):
    assert rules.read_block(block("min_reward_risk = 3\n", fence=fence), "s") == {
        "min_reward_risk": 3}


@pytest.mark.parametrize("fence", ["```rule", "```Rules", "~~~ rules", "```ruels"])
def test_a_near_miss_fence_is_refused(fence):
    text = f"{fence}\nmin_reward_risk = 3\n{fence[:3]}\n"
    with pytest.raises(Refused, match="malformed rules fence"):
        rules.read_block(text, "s")


def test_an_unclosed_fence_is_refused():
    with pytest.raises(Refused, match="never closed"):
        rules.read_block("```rules\nmin_reward_risk = 2\n", "s")


@pytest.mark.parametrize("text", ["no block here", block("a = 1\n") + block("b = 2\n")])
def test_not_exactly_one_block_is_refused(text):
    with pytest.raises(Refused, match="exactly one rules block"):
        rules.read_block(text, "s")


TF = ("15m", "1h")


@pytest.mark.parametrize("values, match", [
    ({"foo": 1}, "'foo' is not a rule the desk knows"),
    ({"size": "computed"}, "size.*largest size"),
    ({"banned": "shorts"}, "sides = "),
    ({"stop_headroom_candles": 1.5}, r"\[stop_atr\]"),
    ({"cooldown_after_stop": "1h"}, "cooldown_after_loss"),
    ({"max_session_length": "2h"}, "delete it"),
    ({"min_reward_risk": "2"}, "min_reward_risk: '2' is not a number"),
    ({"min_reward_risk": True}, "is not a number"),
    ({"min_reward_risk": 0}, "above zero"),
    ({"min_reward_risk": float("inf")}, "finite"),
    ({"min_reward_risk": 10**400}, "min_reward_risk: .*too large"),
    ({"rsi": {"between": [1, 10**400]}}, "rsi.between: .*not two numbers"),
    ({"rsi": []}, "rsi: an empty list holds no check"),
    ({"facts": {}}, "facts: an empty table holds no fact"),
    ({"max_risk_per_trade": 150}, "max_risk_per_trade: .*a table"),
    ({"max_risk_per_trade": {}}, "at least one currency"),
    ({"max_risk_per_trade": {"GBP": 100}}, "only CAD or USD"),
    ({"stop_required": "yes"}, "true or false"),
    ({"sides": "long"}, "sides: .*a list"),
    ({"sides": []}, "at least one"),
    ({"sides": ["up"]}, "'up'.*long or short"),
    ({"instruments": ["option"]}, "stock, etf or future"),
    ({"max_open_trades": 1.5}, "whole number"),
    ({"max_open_trades": True}, "whole number"),
    ({"max_trades_per_day": 0}, "whole number"),
    ({"daily_loss_limit": 2}, r"daily_loss_limit: .*\{ R = 2 \}"),
    ({"daily_loss_limit": {"USD": 500}}, "only R"),
    ({"daily_loss_limit": {"R": -2}}, "above zero"),
    ({"cooldown_after_loss": {}}, "minutes"),
    ({"cooldown_after_loss": {"minutes": 30, "same": True}}, "'same' is not a setting"),
    ({"trading_hours": {"start": "9:45", "end": "11:30"}}, "HH:MM"),
    ({"trading_hours": {"start": dt.time(9, 45), "end": "11:30"}}, "a string"),
    ({"trading_hours": {"start": "18:00", "end": "02:00"}}, "crosses midnight"),
    ({"trading_hours": {"start": "09:30", "end": "16:00", "days": ["monday"]}}, "mon, tue"),
    ({"rsi": {"length": 14}}, "exactly one of at_least, at_most or between"),
    ({"rsi": {"at_most": 30, "at_least": 10}}, "exactly one"),
    ({"rsi": {"at_most": 30, "long": {"at_most": 30}}}, "not both"),
    ({"rsi": {"between": [70, 30]}}, "between"),
    ({"rsi": {"between": [30]}}, "two numbers"),
    ({"rsi": {"timeframe": "4h", "at_most": 30}}, "4h is not one of this strategy's timeframes"),
    ({"rsi": {"depth": 3, "at_most": 30}}, "'depth' is not a setting of rsi"),
    ({"rsi": [{"at_most": 30}, {"at_least": 70}]}, "two rsi checks read rsi_14_15m"),
    ({"macd": {"output": "hist", "at_least": 0}}, "histogram, line or signal"),
    ({"ema": {"long": "above"}}, "ema.length"),
    ({"ema": {"length": 200, "long": "over"}}, "above or below"),
    ({"ema": {"length": 200}}, "long or short"),
    ({"ema": {"length": 200, "long": "above", "at_least": 1}},
     "'at_least' is not a setting of ema"),
    ({"facts": {"rvol": 2}}, "facts.rvol: .*comparison"),
    ({"facts": {"rvol": {"at_least": True}}}, "checklist"),
    ({"facts": {"rvol": {"at_least": "2"}}}, "checklist"),
    ({"facts": {"Rvol": {"at_least": 2}}}, "lowercase"),
    ({"facts": {"rsi_14_15m": {"at_least": 2}}}, "built-in"),
    ({"facts": {"sides": {"at_least": 2}}}, "a rule's name"),
])
def test_validate_refuses_naming_the_key_and_the_fix(values, match):
    with pytest.raises(Refused, match=match):
        rules.validate(values, TF)


FULL = {  # proposal §2.1, without timeframes
    "min_reward_risk": 2.0, "max_risk_per_trade": {"USD": 150, "CAD": 200},
    "stop_required": True, "target_required": True, "sides": ["long", "short"],
    "instruments": ["stock", "etf"], "max_open_trades": 1, "max_trades_per_day": 3,
    "max_consecutive_losses": 3, "daily_loss_limit": {"R": 2},
    "cooldown_after_loss": {"minutes": 30, "same_ticker": False},
    "trading_hours": {"start": "09:45", "end": "11:30",
                      "days": ["mon", "tue", "wed", "thu", "fri"]},
    "stop_atr": {"length": 14, "between": [1.0, 3.0]},
    "rsi": {"length": 14, "long": {"at_most": 30}, "short": {"at_least": 70}},
    "macd": {"fast": 12, "slow": 26, "signal": 9, "output": "histogram",
             "long": {"at_least": 0}, "short": {"at_most": 0}},
    "ema": {"length": 200, "timeframe": "1h", "long": "above", "short": "below"},
    "facts": {"rvol": {"at_least": 2.0}, "minutes_from_news": {"at_least": 5}},
}


def test_the_full_library_validates_and_names_its_keys():
    rules.validate(FULL, TF)
    assert [i.label for i in rules.indicators(FULL, TF)] == [
        "stop_atr_14_15m", "rsi_14_15m", "macd_hist_12_26_9_15m", "ema_200_1h"]
    assert rules.market_keys(FULL, TF) == (
        "atr_14_15m", "rsi_14_15m", "macd_hist_12_26_9_15m", "ema_200_1h", "close_1h",
        "rvol", "minutes_from_news")


def test_two_ema_checks_are_written_as_an_array():
    values = {"ema": [{"length": 50, "long": "above"},
                      {"length": 200, "timeframe": "1h", "long": "above"}]}
    rules.validate(values, TF)
    assert [i.keys for i in rules.indicators(values, TF)] == [
        ("ema_50_15m", "close_15m"), ("ema_200_1h", "close_1h")]
    same = {"ema": [{"length": 20, "long": "above"}, {"length": 50, "long": "above"}]}
    rules.validate(same, TF)                     # one timeframe, two labels: allowed
    assert rules.market_keys(same, TF) == ("ema_20_15m", "close_15m", "ema_50_15m")


def test_defaults_are_filled_in_the_indicator_settings():
    (m,) = rules.indicators({"macd": {"at_least": 0}}, TF)
    assert (m.settings, m.timeframe) == (
        {"fast": 12, "slow": 26, "signal": 9, "output": "histogram"}, "15m")
    assert m.compare == {"at_least": 0}


def test_a_huge_whole_number_is_not_a_raw_error():
    rules.validate({"max_open_trades": 10**400}, TF)   # an int compares exactly: kept as written
