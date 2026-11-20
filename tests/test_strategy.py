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


def test_whitespace_alone_does_not_change_the_hash_but_one_character_does():
    base = strategy.parse(make())
    spaced = strategy.parse(make().replace("\n", "  \n").replace("\n\n", "\r\n\r\n\r\n"))
    assert spaced.text_hash == base.text_hash and spaced.normalised == base.normalised
    changed = strategy.parse(make("timeframes: 1h, 15m\nmin_reward_risk: 3"))
    assert changed.text_hash != base.text_hash


def test_the_shipped_example_strategy_parses():
    s = strategy.parse(config.example_strategy().read_text())
    assert s.timeframes == ("15m", "1h") and "min_reward_risk" in s.rules
