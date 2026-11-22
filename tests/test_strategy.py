"""Tests for reading a strategy. Every figure is invented."""
import pytest

from desk import config, strategy
from desk.errors import Refused


def make(rules: str = 'timeframes = ["1h", "15m"]\nmin_reward_risk = 2',
         front: str = "name: Dip buyer"):
    return f"---\n{front}\n---\n\n# Dip buyer\n\nBuy dips.\n\n```rules\n{rules}\n```\n"


DESIGN_EXAMPLE = '''---
name: Gold opening-range break
---
## Entry
reasoning
## Checklist
- The break came on rising volume.
- No tier-1 news inside the next hour.
  - a sub-bullet is not an item
```rules
timeframes = ["1h", "15m"]
min_reward_risk = 2.0
max_risk_per_trade = { USD = 150, CAD = 200 }
max_open_trades = 1
[rsi]
length = 14
long = { at_most = 30 }
[facts]
rvol = { at_least = 2.0 }
```
'''


def test_a_strategy_parses_into_name_rules_and_timeframes_finest_first():
    s = strategy.parse(make())
    assert s.name == "Dip buyer"
    assert s.rules == {"min_reward_risk": 2}, "timeframes is not among the rules"
    assert s.timeframes == ("15m", "1h") and s.checklist == ()


def test_parse_reads_name_rules_timeframes_and_checklist():
    p = strategy.parse(DESIGN_EXAMPLE)
    assert p.name == "Gold opening-range break" and p.timeframes == ("15m", "1h")
    assert p.checklist == ("The break came on rising volume.",
                           "No tier-1 news inside the next hour.")
    assert "timeframes" not in p.rules
    assert p.rules["rsi"] == {"length": 14, "long": {"at_most": 30}}


@pytest.mark.parametrize("bad, says", [
    ('# no front matter\n\n```rules\ntimeframes = ["1h"]\n```\n', "name"),
    (make(front="title: x"), "name"),
    ("---\nname: A\n---\nno block\n", "one rules block"),
    (make() + make(), "one rules block"),
    (make("min_reward_risk = 2"), "needs timeframes"),
    (make('timeframes = ["1h"]\nfeels_right = "strongly"'), "feels_right"),
    (make('timeframes = ["1h"]\nmax_volatility_ratio = 1.5'), "max_volatility_ratio"),
    (make('timeframes = ["1h"]\nmin_reward_risk = "1:2"'), '"1:2" is not a number'),
    (make('timeframes = ["1h"]\nmin_reward_risk = 2\nmin_reward_risk = 3'), "not valid TOML"),
])
def test_a_strategy_the_checks_could_not_apply_is_refused_naming_the_line(bad, says):
    with pytest.raises(Refused) as e:
        strategy.parse(bad)
    assert says in str(e.value)


@pytest.mark.parametrize("tf, match", [
    (None, "needs timeframes"), ('"15m"', "a list"), ("[]", "at least one"),
    ('["4x"]', "not a timeframe"), ("[15]", "not a timeframe"),
    ('["60m", "1h"]', "same timeframe")])
def test_timeframes_are_required_and_strict(tf, match):
    rules = "min_reward_risk = 2" if tf is None else f"timeframes = {tf}"
    with pytest.raises(Refused, match=match):
        strategy.parse(make(rules))


def test_an_indicator_may_name_only_a_declared_timeframe():
    with pytest.raises(Refused, match="4h is not one of this strategy's timeframes"):
        strategy.parse(make('timeframes = ["1h"]\n[rsi]\ntimeframe = "4h"\nat_most = 30'))


def with_checklist(body: str) -> str:
    return make().replace("```rules", f"{body}\n```rules", 1)


def test_the_checklist_ends_at_the_next_heading_and_ignores_fences():
    text = with_checklist(
        "## CHECKLIST  \n* star\n+ plus\n- dash\n```\n## Checklist\n- in a fence\n```\n"
        "## Notes\n- not an item\n")
    assert strategy.parse(text).checklist == ("star", "plus", "dash")
    assert strategy.parse(with_checklist("## Checklist\n- a\n### Sub\n- b\n")).checklist == ("a",)


@pytest.mark.parametrize("body, match", [
    ("## Checklist\n- a\n- a\n", "written twice"),
    ("## Checklist\n- a\n-\n", "empty"),
    ("## Checklist\n- a\n## Notes\n## Checklist\n- b\n", "two '## Checklist'"),
])
def test_a_duplicate_or_empty_item_or_a_second_checklist_is_refused(body, match):
    with pytest.raises(Refused, match=match):
        strategy.parse(with_checklist(body))


@pytest.mark.parametrize("heading", ["### Checklist", "## Checklist:", "# checklist",
                                     "###### CHECKLIST :", " ## Checklist", "## Checklist ##"])
def test_a_checklist_heading_written_another_way_is_refused(heading):
    with pytest.raises(Refused, match="write it as '## Checklist'"):
        strategy.parse(with_checklist(f"{heading}\n- a\n"))


@pytest.mark.parametrize("body", ["1. a\n2. b\n", "The trend is up.\n", "a\n```\n- b\n```\n"])
def test_a_checklist_with_text_but_no_bullet_items_is_refused(body):
    with pytest.raises(Refused, match="no bullet items; write each item on a line starting '- '"):
        strategy.parse(with_checklist(f"## Checklist\n{body}"))


def test_a_task_box_is_not_part_of_the_item_and_prose_beside_items_is_kept_out():
    text = with_checklist("## Checklist\nAnswer each.\n- [ ] a\n- [x] b\n* [X] c\n- [ ]\n")
    with pytest.raises(Refused, match="empty"):
        strategy.parse(text)
    assert strategy.parse(text.replace("- [ ]\n", "")).checklist == ("a", "b", "c")


def test_no_checklist_section_is_an_empty_checklist():
    assert strategy.parse(make()).checklist == ()
    assert strategy.parse(with_checklist("## Checklist\n")).checklist == ()


BASE = make()
SAME_TEXT = {
    "CRLF line endings": BASE.replace("\n", "\r\n"),
    "extra blank lines": BASE.replace("\n\n", "\n\n\n\n"),
    "trailing spaces": BASE.replace("\n", "   \n"),
    "tabs for spaces in a run": BASE.replace("Buy dips.", "Buy\t\tdips.").replace(
        "min_reward_risk = 2", "min_reward_risk\t=\t2"),
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
    assert strategy.parse(make('timeframes = ["1h", "15m"]\nmin_reward_risk = 3')).text_hash != \
        strategy.parse(BASE).text_hash


def test_a_repeated_timeframe_is_kept_once():
    assert strategy.parse(make('timeframes = ["1h", "15m", "1h"]')).timeframes == ("15m", "1h")


@pytest.mark.parametrize(("frames", "same"), [
    ('"60m", "1h"', "60m and 1h"), ('"1h", "15m", "60m"', "1h and 60m"),
    ('"1d", "24h"', "1d and 24h"), ('"7d", "1w"', "7d and 1w")])
def test_two_names_for_one_timeframe_are_refused(frames, same):
    """Two names for one timeframe tied, so which stored first followed hash randomisation; the
    plan needs the finest first, so the tie is refused rather than left to chance."""
    with pytest.raises(Refused, match=f"{same} are the same timeframe; keep one"):
        strategy.parse(make(f"timeframes = [{frames}]"))


def test_an_unknown_rule_is_refused_as_not_checkable():
    with pytest.raises(Refused, match="not a rule the desk knows"):
        strategy.parse(make('timeframes = ["1h"]\nfeels_right = "strongly"'))


@pytest.mark.parametrize("rule", ['daily_loss_limit = "500 USD"', "max_open_trades = -1",
                                  "max_open_trades = 0", "max_open_trades = 2.5"])
def test_a_value_the_check_would_never_apply_is_refused_at_save(rule):
    """Review item 2: each of these saved, and its check then misapplied or withheld it."""
    with pytest.raises(Refused, match=f"strategy: {rule.split(' =')[0]}: .*write it as"):
        strategy.parse(make(f'timeframes = ["1h"]\n{rule}'))


def test_the_shipped_example_strategy_parses():
    s = strategy.parse(config.example_strategy().read_text())
    assert s.timeframes == ("15m", "1h") and "min_reward_risk" in s.rules
    assert len(s.checklist) == 2 and s.rules["ema"]["length"] == 50


def test_a_timeframe_in_minutes():
    assert [strategy.minutes(t) for t in ("15m", "1h", "1d", "1w")] == [15, 60, 1440, 10080]


def test_a_checklist_heading_with_a_tab_is_read():
    text = make().replace("# Dip buyer\n", "##\tChecklist\n- One.\n\n# Dip buyer\n")
    assert strategy.parse(text).checklist == ("One.",)


def test_a_bullet_indented_one_space_is_an_item_and_two_is_a_sub_point():
    text = make().replace(
        "# Dip buyer\n", "## Checklist\n- One.\n - Two.\n  - a sub-point\n\n# Dip buyer\n")
    assert strategy.parse(text).checklist == ("One.", "Two.")
