"""Tests for reading a strategy. Every figure is invented."""
import pytest

from desk import config, strategy
from desk.errors import Refused


def make(rules: str = "timeframes: 1h, 15m\nmin_reward_risk: 2", front: str = "name: Dip buyer"):
    return f"---\n{front}\n---\n\n# Dip buyer\n\nBuy dips.\n\n```rules\n{rules}\n```\n"


def test_a_strategy_parses_into_name_rules_and_timeframes_finest_first():
    s = strategy.parse(make())
    assert s.name == "Dip buyer"
    assert s.rules == {"min_reward_risk": "2"}, "timeframes is not among the rules"
    assert s.timeframes == ("15m", "1h")


@pytest.mark.parametrize("bad, says", [
    ("# no front matter\n\n```rules\ntimeframes: 1h\n```\n", "name"),
    (make(front="title: x"), "name"),
    ("---\nname: A\n---\nno block\n", "one rules block"),
    (make() + make(), "one rules block"),
    (make("min_reward_risk: 2"), "timeframes"),
    (make("timeframes: 1h, 7x"), "7x"),
    (make("timeframes: 1h\nfeels_right: strongly"), "feels_right"),
    (make("timeframes: 1h\nmax_volatility_ratio: 1.5"), "max_volatility_ratio"),
    (make("timeframes: 1h\nmin_reward_risk: 1:2"), "1:2"),
    (make("timeframes: 1h\nmin_reward_risk: 2\nmin_reward_risk: 3"), "twice"),
])
def test_a_strategy_the_checks_could_not_apply_is_refused_naming_the_line(bad, says):
    with pytest.raises(Refused) as e:
        strategy.parse(bad)
    assert says in str(e.value)


BASE = make()
SAME_TEXT = {
    "CRLF line endings": BASE.replace("\n", "\r\n"),
    "extra blank lines": BASE.replace("\n\n", "\n\n\n\n"),
    "trailing spaces": BASE.replace("\n", "   \n"),
    "tabs for spaces in a run": BASE.replace("Buy dips.", "Buy\t\tdips.").replace(
        "min_reward_risk: 2", "min_reward_risk:\t2"),
    "a wider run of spaces": BASE.replace("Buy dips.", "Buy     dips."),
}


@pytest.mark.parametrize("variant", SAME_TEXT)
def test_whitespace_alone_does_not_change_the_hash(variant):
    text = SAME_TEXT[variant]
    assert text != BASE
    assert ("\r" in text) == (variant == "CRLF line endings")
    s = strategy.parse(text)
    assert s.text_hash == strategy.parse(BASE).text_hash and s.text == text


def test_one_character_changes_the_hash():
    assert strategy.parse(make("timeframes: 1h, 15m\nmin_reward_risk: 3")).text_hash != \
        strategy.parse(BASE).text_hash


def test_a_repeated_timeframe_is_kept_once():
    assert strategy.parse(make("timeframes: 1h, 15m, 1h")).timeframes == ("15m", "1h")


@pytest.mark.parametrize(("frames", "same"), [
    ("60m, 1h", "60m and 1h"), ("1h, 15m, 60m", "1h and 60m"), ("1d, 24h", "1d and 24h"),
    ("7d, 1w", "7d and 1w")])
def test_two_names_for_one_timeframe_are_refused(frames, same):
    """Review item 10: 60m and 1h tied, so their stored order followed hash randomisation, and
    plan 3 reads the finest first."""
    with pytest.raises(Refused, match=f"{same} are the same timeframe; keep one"):
        strategy.parse(make(f"timeframes: {frames}"))


def test_a_retired_rule_is_refused_as_retired_not_as_a_typo():
    with pytest.raises(Refused, match="not a rule in this version"):
        strategy.parse(make("timeframes: 1h\nmax_session_length: 2h"))


def test_an_unknown_rule_is_refused_as_not_checkable():
    with pytest.raises(Refused, match="not a rule the desk can check"):
        strategy.parse(make("timeframes: 1h\nfeels_right: strongly"))


def test_an_unreadable_value_is_refused_with_why():
    with pytest.raises(Refused, match="could not read"):
        strategy.parse(make("timeframes: 1h\nmin_reward_risk: 1:2"))


@pytest.mark.parametrize("rule", ["daily_loss_limit: 500 USD", "banned: shorts, crypto",
                                  "max_open_trades: -1", "max_open_trades: 0",
                                  "max_open_trades: 2.5"])
def test_a_value_the_check_would_never_apply_is_refused_at_save(rule):
    """Review item 2: each of these saved, and its check then misapplied or withheld it."""
    with pytest.raises(Refused, match=f"strategy: {rule.split(':')[0]}: .*e\\.g\\."):
        strategy.parse(make(f"timeframes: 1h\n{rule}"))


def test_the_shipped_example_strategy_parses():
    s = strategy.parse(config.example_strategy().read_text())
    assert s.timeframes == ("15m", "1h") and "min_reward_risk" in s.rules
