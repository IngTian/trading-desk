"""Tests for rules.md parsing."""
from pathlib import Path

import pytest

from desk import checks, config, rules

FIXTURE = Path(__file__).parent / "fixtures" / "rules_invented.md"

# Invented check names for testing — no reference to real checks
INVENTED_CHECKS = frozenset(["min_reward_risk", "max_position_pct", "max_trades_per_day"])


def test_a_narrower_section_wins_over_a_broader_one():
    """## All sets min_reward_risk 1, ## Real sets 3, a real trade gets 3."""
    r = rules.load(FIXTURE).for_scope("real", None, INVENTED_CHECKS)
    assert r.numbers["min_reward_risk"] == "3"
    assert r.sections_used == ("All", "Real")


def test_a_paper_run_section_wins_over_paper():
    r = rules.load(FIXTURE).for_scope("paper", "run-2", INVENTED_CHECKS)
    assert r.sections_used == ("All", "Paper", "Paper — run 2")


def test_a_rule_name_with_no_code_check_is_reported_not_dropped():
    r = rules.load(FIXTURE).for_scope("paper", None, INVENTED_CHECKS)
    assert "feels_right" in r.unchecked
    assert "feels_right" not in r.numbers, "an unchecked name must not reach run_checks"


def test_an_unchecked_rule_keeps_the_value_as_written():
    """The desk quotes the rule back while saying it will ask, so the name alone is not enough."""
    r = rules.load(FIXTURE).for_scope("paper", None, INVENTED_CHECKS)
    assert r.unchecked["feels_right"] == "yes"


def test_the_ask_me_list_is_kept_in_file_order_and_verbatim():
    r = rules.load(FIXTURE).for_scope("real", None, INVENTED_CHECKS)
    assert len(r.ask) >= 2
    # From All section
    assert r.ask[0] == "Is this sector overheated?"
    assert r.ask[1] == "Does the thesis still hold?"
    # From Real section
    assert r.ask[2] == "Will you check this trade tomorrow?"


def test_groups_are_parsed_so_no_open_trade_in_group_can_use_them():
    r = rules.load(FIXTURE).for_scope("real", None, INVENTED_CHECKS)
    assert "tech" in r.groups
    assert r.groups["tech"] == ("ACME", "WXYZ")
    assert "consumer" in r.groups
    assert r.groups["consumer"] == ("TEST",)


def test_the_hash_changes_when_the_file_changes_and_not_when_it_does_not(tmp_path):
    content = "## All\n\n```rules\nfoo: 1\n```\n"
    p = tmp_path / "rules.md"
    p.write_text(content)

    hash1 = rules.load(p).for_scope("real", None, frozenset()).hash
    hash2 = rules.load(p).for_scope("real", None, frozenset()).hash
    assert hash1 == hash2, "hash should be stable for unchanged file"

    p.write_text(content + "# change\n")
    hash3 = rules.load(p).for_scope("real", None, frozenset()).hash
    assert hash1 != hash3, "hash should change when file changes"


def test_a_missing_rules_file_names_the_path_it_looked_for(tmp_path):
    missing = tmp_path / "does_not_exist.md"
    with pytest.raises(FileNotFoundError) as exc_info:
        rules.load(missing)
    assert str(missing) in str(exc_info.value)


def test_a_malformed_rules_block_names_the_line(tmp_path):
    p = tmp_path / "bad.md"
    p.write_text("## All\n\n```rules\nno_colon_here\n```\n")
    with pytest.raises(ValueError) as exc_info:
        rules.load(p).for_scope("real", None, frozenset())
    err = str(exc_info.value)
    assert str(p) in err, "error should name the file"
    assert "4" in err or "line 4" in err.lower(), "error should name line 4"


def test_changes_are_parsed_with_dates_and_text_verbatim():
    r = rules.load(FIXTURE).for_scope("real", None, INVENTED_CHECKS)
    assert len(r.changes) == 3
    # Verify dates
    assert r.changes[0].date == "2026-01-15"
    assert r.changes[1].date == "2026-02-20"
    assert r.changes[2].date == "2026-03-10"
    # Verify text is verbatim after the colon
    assert r.changes[0].text == "Initial rules"
    assert r.changes[1].text == "Increased real min_reward_risk to 3"
    assert r.changes[2].text == "Added paper run 2 with higher position limit"


def test_prose_bullets_outside_ask_or_changes_are_ignored(tmp_path):
    """Stray list items should not become ask-me items or changes."""
    content = """## All

```rules
foo: 1
```

- This is a prose bullet, not an ask-me item
- Another stray bullet

## Real

- Prose under Real section

### Ask me

- This IS an ask-me item

## Changes

- 2026-01-01: This IS a change
"""
    p = tmp_path / "rules.md"
    p.write_text(content)
    r = rules.load(p).for_scope("real", None, frozenset())

    # Only the Ask me item should appear in ask
    assert r.ask == ("This IS an ask-me item",)
    # Only the Changes item should appear in changes
    assert len(r.changes) == 1
    assert r.changes[0].text == "This IS a change"


def test_ask_me_ordering_is_scope_composition_order(tmp_path):
    """Ask-me items from All come before Real, regardless of file position."""
    content = """## Real

### Ask me

- Real question

## All

### Ask me

- All question
"""
    p = tmp_path / "rules.md"
    p.write_text(content)
    r = rules.load(p).for_scope("real", None, frozenset())

    # All's ask-me comes first despite Real appearing first in file
    assert len(r.ask) == 2
    assert r.ask[0] == "All question"
    assert r.ask[1] == "Real question"


def test_the_shipped_example_rules_file_parses_and_uses_no_real_figures():
    example = config.example_rules()
    r = rules.load(example).for_scope("real", None, checks.CHECK_NAMES)
    assert r.numbers and not r.unchecked, "the example must only use implemented checks"


def test_mistyped_rules_fence_is_refused_not_silently_accepted(tmp_path):
    """A fence with a space (``` rules instead of ```rules) is a typo and must be refused."""
    content = """## Real

``` rules
min_reward_risk: 5
```
"""
    p = tmp_path / "rules.md"
    p.write_text(content)

    with pytest.raises(ValueError) as exc_info:
        rules.load(p)

    # Must name the line number and show the malformed fence
    err = str(exc_info.value)
    assert "rules.md" in err or str(p) in err
    assert "``` rules" in err or "rules" in err.lower()
    assert "malformed" in err.lower() or "expected" in err.lower()
    # Must include line number
    assert any(char.isdigit() for char in err)


def test_duplicate_section_heading_is_refused(tmp_path):
    """Two ## Real headings: the second would overwrite the first, so refuse."""
    content = """## Real

```rules
max_risk_per_trade: 40 USD
```

## Real

```rules
min_reward_risk: 5
```
"""
    p = tmp_path / "rules.md"
    p.write_text(content)

    with pytest.raises(ValueError) as exc_info:
        rules.load(p)

    # Must name both line numbers and the section name
    err = str(exc_info.value)
    assert "Real" in err
    assert "duplicate" in err.lower()
    # Must include line numbers for both occurrences
    assert any(char.isdigit() for char in err)


# ---------------------------------------------------------------------------------------------
# Shapes of rules.md that used to drop or mis-scope a rule without a word. Each one either
# parses the way it reads or is refused with the line and the fix; none is silently tolerated.


def _parsed(tmp_path, content):
    p = tmp_path / "rules.md"
    p.write_text(content)
    return rules.load(p)


def _refused(tmp_path, content) -> str:
    with pytest.raises(ValueError) as exc_info:
        _parsed(tmp_path, content)
    return str(exc_info.value)


def test_a_wrapped_ask_me_question_keeps_its_second_line_and_every_later_question(tmp_path):
    """One wrapped bullet used to end the list: the question was cut and the rest dropped."""
    r = _parsed(tmp_path, """## Paper

### Ask me

- Is the first question long enough that it wraps
  onto a second line?
- Is there a second question?
- And a third,
which wraps without an indent?

## Changes
""").for_scope("paper", None, frozenset())
    assert r.ask == (
        "Is the first question long enough that it wraps onto a second line?",
        "Is there a second question?",
        "And a third, which wraps without an indent?",
    )


def test_ask_me_accepts_star_bullets_and_any_heading_case(tmp_path):
    r = _parsed(tmp_path, """## Paper

### Ask Me

* First?
* Second?
""").for_scope("paper", None, frozenset())
    assert r.ask == ("First?", "Second?")


def test_the_ask_me_list_ends_at_prose_after_a_blank_line_and_at_a_heading(tmp_path):
    r = _parsed(tmp_path, """## Paper

### Ask me

- Kept?

- Also kept, a loose list is still a list?

This paragraph is not a question.

- Nor is this bullet, which belongs to the paragraph.

### Notes

- Not a question either.
""").for_scope("paper", None, frozenset())
    assert r.ask == ("Kept?", "Also kept, a loose list is still a list?")


def test_a_rule_named_twice_in_a_section_is_refused(tmp_path):
    """Last-one-wins could LOOSEN a rule: 3 then 2 silently became 2."""
    err = _refused(tmp_path, """## Real

```rules
min_reward_risk: 3
min_reward_risk: 2
```
""")
    assert "min_reward_risk" in err and "4" in err and "5" in err


def test_a_rule_named_again_in_a_second_block_of_the_same_section_is_refused(tmp_path):
    err = _refused(tmp_path, """## Real

```rules
max_open_trades: 1
```

```rules
max_open_trades: 4
```
""")
    assert "max_open_trades" in err


@pytest.mark.parametrize("fence", ["~~~rules", "````rules"])
def test_other_markdown_fences_for_a_rules_block_are_read(tmp_path, fence):
    close = fence[:-len("rules")]
    r = _parsed(tmp_path, f"""## Real

{fence}
min_reward_risk: 3
{close}
""").for_scope("real", None, frozenset({"min_reward_risk"}))
    assert r.numbers == {"min_reward_risk": "3"}


@pytest.mark.parametrize("fence", ["```rule", "```Rules", "~~~ rules", "```ruels"])
def test_a_near_miss_rules_fence_is_refused(tmp_path, fence):
    err = _refused(tmp_path, f"""## Real

{fence}
min_reward_risk: 3
```
""")
    assert "rules" in err and ":3" in err


@pytest.mark.parametrize("heading", ["##Replay", "### Replay", "# Replay", "### replay"])
def test_a_misformed_section_heading_is_refused_not_merged_into_the_section_above(
        tmp_path, heading):
    """`##Replay` used to fold Replay's looser floor into All, which real money reads."""
    err = _refused(tmp_path, f"""## All

```rules
min_reward_risk: 2
```

{heading}

```rules
min_reward_risk: 1
```
""")
    assert "## Replay" in err and ":7" in err


def test_a_rules_block_before_any_section_heading_is_refused(tmp_path):
    err = _refused(tmp_path, """# My rules

```rules
min_reward_risk: 2
```

## All
""")
    assert ":3" in err and "section" in err


@pytest.mark.parametrize(("typed", "normal"), [
    ("3", "run-3"), ("run-3", "run-3"), ("run 3", "run-3"), ("Run3", "run-3"), (" RUN_3 ", "run-3"),
])
def test_a_run_id_is_read_when_it_names_one_run(typed, normal):
    assert rules.normalize_run_id(typed) == normal


@pytest.mark.parametrize("typed", ["3b", "run-3-retry", "r3v2", "run", ""])
def test_a_run_id_that_does_not_name_one_run_is_refused(typed):
    """'3b', 'run-3-retry' and 'r3v2' all collapsed into run 3."""
    with pytest.raises(ValueError):
        rules.normalize_run_id(typed)


def test_sections_no_scope_can_reach_are_reported_as_unrecognised(tmp_path):
    rf = _parsed(tmp_path, """## All
## Real
## Real — run 9
## Paper — run 2
## Replay - run 4
## replay
## Groups
## Changes
""")
    # Runs exist only for paper and replay, so a Real run's rules can never apply; a hyphen
    # is not the separator; and section names are case-sensitive.
    assert rf.unrecognised_sections() == ("Real — run 9", "Replay - run 4", "replay")


def test_a_scope_says_which_account_section_it_wanted_and_did_not_find(tmp_path):
    rf = _parsed(tmp_path, """## All

```rules
min_reward_risk: 2
```
""")
    r = rf.for_scope("replay", None, frozenset({"min_reward_risk"}))
    assert r.sections_missing == ("Replay",)
    assert rf.for_scope("replay", None, frozenset()).numbers == {}
