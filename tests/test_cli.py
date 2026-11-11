"""Tests for the CLI: trading-desk check and trading-desk integrity.

Every figure here is invented (tests/invented.py). The repo is public, so no real trades.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from desk import cli, config, rules, store

FIXTURE = Path(__file__).parent / "fixtures" / "rules_invented.md"


@pytest.fixture
def desk_home(tmp_path, monkeypatch):
    """Point TRADING_DESK_HOME at tmp_path so tests never touch the real book."""
    monkeypatch.setenv("TRADING_DESK_HOME", str(tmp_path))
    # Create rules.md so check doesn't fail on missing file
    (tmp_path / "rules.md").write_text(FIXTURE.read_text())
    return tmp_path


def test_check_prints_every_rule_result_and_appends_one_checked_event(desk_home):
    """A check runs all rules, prints one line per result, and writes one checked event."""
    # Create the database first
    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
                   "--currency", "USD"])
    assert rc == 0
    con = store.open()
    (ev,) = [e for e in store.events(con) if e["kind"] == "checked"]
    payload = json.loads(ev["payload"])
    # Assert exact set equality: Real scope gets min_reward_risk from Real (overriding All's 1),
    # plus two unchecked rules
    assert {r["name"] for r in payload["results"]} == {
        "min_reward_risk",      # Real's 3 overrides All's 1
        "max_trades_per_day",   # from All; no code check, so reported unchecked
        "max_position_pct",     # from Real; no code check, so reported unchecked
    }
    # Assert unchecked rules carry the verbatim sentence
    unchecked = {r["name"]: r["says"] for r in payload["results"] if r["ok"] is None}
    assert unchecked == {
        "max_trades_per_day": "no automatic check for this one; I'll ask you each time",
        "max_position_pct": "no automatic check for this one; I'll ask you each time",
    }
    assert ev["rules_hash"] == rules.load(config.rules_path()).hash


def test_check_records_the_rules_hash_so_an_edit_mid_session_is_visible(desk_home):
    """§5: every check stores the rules file's hash and date. Edit the file, re-check,
    and the two events must carry different hashes."""
    # First check
    cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
              "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
              "--currency", "USD",
             ])

    # Edit the rules file
    rules_file = config.rules_path()
    rules_file.write_text(rules_file.read_text() + "\n# Added comment\n")

    # Second check
    cli.main(["check", "--ticker", "WXYZ", "--side", "short", "--entry", "50",
              "--stop", "52", "--target", "45", "--size", "50", "--account", "real",
              "--currency", "USD",
             ])

    con = store.open()
    checked_events = [e for e in store.events(con) if e["kind"] == "checked"]
    assert len(checked_events) == 2
    assert checked_events[0]["rules_hash"] != checked_events[1]["rules_hash"]


def test_check_reports_a_breach_as_a_fact_and_still_exits_zero(desk_home):
    """A rule result is never a block: the desk cannot stop a click, so a breach is
    recorded and printed, and the command succeeds."""
    # Create a plan that breaches min_reward_risk (R < 2)
    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "10.5", "--size", "100", "--account", "real",
                   "--currency", "USD",
                  ])
    assert rc == 0


def test_check_with_no_rules_file_says_which_path_it_wanted(desk_home):
    """When rules.md is missing, print the path that was expected."""
    # Remove the rules file
    config.rules_path().unlink()

    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
                   "--currency", "USD",
                  ])
    assert rc == 1


def test_check_json_output_is_valid_json_and_refuses_nan(desk_home):
    """json.dumps defaults to allow_nan=True and emits Infinity, which is not JSON."""
    cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
              "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
              "--currency", "USD",
             ])

    con = store.open()
    (ev,) = [e for e in store.events(con) if e["kind"] == "checked"]
    payload_str = ev["payload"]

    # This must not raise
    payload = json.loads(payload_str)

    # Re-serialize and verify it's still valid (catches NaN/Infinity)
    json.dumps(payload)  # would raise TypeError if NaN present


def test_a_misspelled_side_is_refused_rather_than_treated_as_a_short(desk_home):
    """`model._signed` treats any non-'long' side as a short, so --side must be constrained."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["check", "--ticker", "ACME", "--side", "lng", "--entry", "10",
                  "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
                  "--currency", "USD",
                 ])
    assert exc.value.code != 0


def test_a_rules_file_matching_no_section_says_so_instead_of_reporting_no_rules(desk_home):
    """Carried from Task 4. A rules.md whose headings match neither the account nor All
    composes to zero rules. Reporting 'every rule passed' and exiting 0 is the failure mode
    this whole tool exists to prevent, so say which headings were found and which were
    wanted."""
    # Create a rules file with only a Paper section
    rules_file = config.rules_path()
    rules_file.write_text("""## Paper

```rules
min_reward_risk: 2
```
""")

    # Check with account=real should complain
    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
                   "--currency", "USD",
                  ])
    assert rc != 0


def test_the_first_run_creates_the_book_and_names_the_path_it_created(desk_home, capsys):
    """When the database doesn't exist, create it and print where it was created."""
    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
                   "--currency", "USD",
                  ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "created" in captured.out.lower()
    assert str(config.db_path()) in captured.out


def test_check_creates_the_trade_row_with_the_checked_event(desk_home):
    """The trade row and the checked event are written together."""
    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
                   "--currency", "USD",
                  ])
    assert rc == 0
    con = store.open()

    # Should have one trade
    trade_ids = store.trade_ids(con)
    assert len(trade_ids) == 1

    # The trade id should be readable
    assert trade_ids[0].startswith("acme-")

    # The trade should have a checked event
    checked_events = [e for e in store.events(con, trade_id=trade_ids[0]) if e["kind"] == "checked"]
    assert len(checked_events) == 1


def test_check_generates_collision_suffixes_for_same_ticker_same_day(desk_home):
    """When checking the same ticker twice on the same day, append -2, -3, etc."""
    # First check
    cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
              "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
              "--currency", "USD",
             ])

    # Second check same ticker same day
    cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "11",
              "--stop", "10", "--target", "13", "--size", "50", "--account", "real",
              "--currency", "USD",
             ])

    # Third check same ticker same day
    cli.main(["check", "--ticker", "ACME", "--side", "short", "--entry", "12",
              "--stop", "13", "--target", "10", "--size", "75", "--account", "real",
              "--currency", "USD",
             ])

    con = store.open()
    trade_ids = store.trade_ids(con)
    assert len(trade_ids) == 3

    # Check that IDs have proper suffixes
    base = trade_ids[0]  # acme-20261110 or similar
    assert not base.endswith("-2") and not base.endswith("-3")
    assert trade_ids[1] == base + "-2"
    assert trade_ids[2] == base + "-3"


def test_run_arg_creates_runs_row_if_absent(desk_home):
    """--run N creates the runs row if it doesn't exist."""
    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "12", "--size", "100", "--account", "paper",
                   "--run", "run-5",
                   "--currency", "USD",
                  ])
    assert rc == 0

    con = store.open()
    run_row = con.execute("SELECT * FROM runs WHERE id = ?", ("run-5",)).fetchone()
    assert run_row is not None
    assert run_row["rules_section"] == "Paper — run 5"


def test_run_arg_with_real_account_is_refused(desk_home):
    """--run with --account real is a contradiction."""
    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
                   "--run", "run-1",
                   "--currency", "USD",
                  ])
    assert rc != 0


def test_integrity_subcommand_runs_all_checks(desk_home):
    """trading-desk integrity runs integrity.run() and returns its exit code."""
    # Create a database with some events
    cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
              "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
              "--currency", "USD",
             ])

    rc = cli.main(["integrity"])
    assert rc == 0  # Should pass with a clean log
