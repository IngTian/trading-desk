"""Tests for reading a strategy's rules block."""
import pytest

from desk import rules
from desk.errors import Refused


def block(body: str, fence: str = "```") -> str:
    return f"# A strategy\n\n{fence}rules\n{body}\n{fence}\n"


def test_values_are_kept_verbatim_in_file_order():
    got = rules.parse_rules_block(block("min_reward_risk: 2\nmax_risk_per_trade: 100 USD"), "s")
    assert list(got.items()) == [("min_reward_risk", "2"), ("max_risk_per_trade", "100 USD")]


def test_a_rule_named_twice_is_refused_with_both_lines():
    with pytest.raises(Refused) as e:
        rules.parse_rules_block(block("min_reward_risk: 3\nmin_reward_risk: 2"), "s")
    assert "min_reward_risk" in str(e.value) and "s:5" in str(e.value) and "line 4" in str(e.value)


@pytest.mark.parametrize("fence", ["~~~", "````"])
def test_other_markdown_fences_are_read(fence):
    assert rules.parse_rules_block(block("min_reward_risk: 3", fence), "s") == {
        "min_reward_risk": "3"}


@pytest.mark.parametrize("fence", ["```rule", "```Rules", "~~~ rules", "```ruels"])
def test_a_near_miss_fence_is_refused(fence):
    text = f"{fence}\nmin_reward_risk: 3\n{fence[:3]}\n"
    with pytest.raises(Refused, match="malformed rules fence"):
        rules.parse_rules_block(text, "s")


def test_a_line_that_is_not_name_value_is_refused_with_its_line():
    with pytest.raises(Refused, match="s:4: malformed rule"):
        rules.parse_rules_block(block("min_reward_risk 2"), "s")


def test_an_unclosed_fence_is_refused():
    with pytest.raises(Refused, match="never closed"):
        rules.parse_rules_block("```rules\nmin_reward_risk: 2\n", "s")


@pytest.mark.parametrize("text", ["no block here", block("a: 1") + block("b: 2")])
def test_not_exactly_one_block_is_refused(text):
    with pytest.raises(Refused, match="exactly one rules block"):
        rules.parse_rules_block(text, "s")
