"""Tests for rules.md parsing."""
from pathlib import Path

import pytest

from desk import rules

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
