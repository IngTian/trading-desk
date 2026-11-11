"""Tests for the CLI: trading-desk check and trading-desk integrity.

Every figure here is invented (tests/invented.py). The repo is public, so no real trades.
"""
from __future__ import annotations

import json
from pathlib import Path

import invented
import pytest

from desk import cli, config, integrity, rules, store

FIXTURE = Path(__file__).parent / "fixtures" / "rules_invented.md"


@pytest.fixture
def desk_home(tmp_path, monkeypatch):
    """Point TRADING_DESK_HOME at tmp_path so tests never touch the real book."""
    monkeypatch.setenv("TRADING_DESK_HOME", str(tmp_path))
    # Create rules.md so check doesn't fail on missing file
    (tmp_path / "rules.md").write_text(FIXTURE.read_text())
    return tmp_path


@pytest.fixture
def fresh_desk_home(tmp_path, monkeypatch):
    """Point TRADING_DESK_HOME at a directory that does not exist yet."""
    fresh_dir = tmp_path / "trading-desk"
    monkeypatch.setenv("TRADING_DESK_HOME", str(fresh_dir))
    assert not fresh_dir.exists()
    return fresh_dir


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
    # Assert unchecked rules carry their name, value, and the explanation
    unchecked = {r["name"]: r["says"] for r in payload["results"] if r["ok"] is None}
    # The says field must include the rule name and value
    assert "max_trades_per_day: 5" in unchecked["max_trades_per_day"]
    assert "no automatic check for this one" in unchecked["max_trades_per_day"]
    assert "max_position_pct: 10" in unchecked["max_position_pct"]
    assert "no automatic check for this one" in unchecked["max_position_pct"]
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


def test_check_reports_a_breach_as_a_fact_and_still_exits_zero(desk_home, capsys):
    """A rule result is never a block: the desk cannot stop a click, so a breach is
    recorded and printed, and the command succeeds."""
    # Create a plan that breaches min_reward_risk (R < 3 for Real account, we have 0.5)
    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "10.5", "--size", "100", "--account", "real",
                   "--currency", "USD",
                  ])
    assert rc == 0
    # Check that breach was printed
    captured = capsys.readouterr()
    assert "reward to risk 0.50 against a minimum of 3" in captured.out


def test_check_with_no_rules_file_says_which_path_it_wanted(desk_home, capsys):
    """When rules.md is missing, print the path that was expected."""
    # Remove the rules file
    rules_path = config.rules_path()
    rules_path.unlink()

    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
                   "--currency", "USD",
                  ])
    assert rc == 1
    captured = capsys.readouterr()
    assert str(rules_path) in captured.err
    assert "not found" in captured.err.lower()


def test_check_json_output_is_valid_json_and_refuses_nan(desk_home, capsys):
    """--json outputs valid JSON with allow_nan=False enforced."""
    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
                   "--currency", "USD", "--json"])
    assert rc == 0

    # Capture and parse JSON output
    captured = capsys.readouterr()
    output = json.loads(captured.out)

    # Verify structure
    assert "plan" in output
    assert "results" in output
    assert "rules_hash" in output
    assert output["plan"]["ticker"] == "ACME"
    assert {r["name"] for r in output["results"]} == {
        "min_reward_risk", "max_trades_per_day", "max_position_pct"
    }

    # Re-serialize with allow_nan=False (would raise ValueError if NaN/Infinity present)
    json.dumps(output, allow_nan=False)


def test_stored_payload_is_valid_json(desk_home):
    """The event payload stored in the database is also valid JSON."""
    cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
              "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
              "--currency", "USD"])

    con = store.open()
    (ev,) = [e for e in store.events(con) if e["kind"] == "checked"]
    payload_str = ev["payload"]

    # This must not raise
    payload = json.loads(payload_str)

    # Re-serialize with allow_nan=False
    json.dumps(payload, allow_nan=False)


def test_a_misspelled_side_is_refused_rather_than_treated_as_a_short(desk_home):
    """`model._signed` treats any non-'long' side as a short, so --side must be constrained."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["check", "--ticker", "ACME", "--side", "lng", "--entry", "10",
                  "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
                  "--currency", "USD",
                 ])
    assert exc.value.code != 0


def test_a_rules_file_matching_no_section_says_so_instead_of_reporting_no_rules(desk_home, capsys):
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
    captured = capsys.readouterr()
    # Should name sections wanted
    assert "Real" in captured.err
    # Should name sections found
    assert "Paper" in captured.err


def test_the_first_run_creates_the_book_and_names_the_path_it_created(desk_home, capsys):
    """When the database doesn't exist, create it and print where it was created (to stderr)."""
    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
                   "--currency", "USD",
                  ])
    assert rc == 0
    captured = capsys.readouterr()
    # Message goes to stderr so --json stdout stays pure
    assert "created" in captured.err.lower()
    assert str(config.db_path()) in captured.err


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
    # Use run-2 which exists in the fixture
    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "12", "--size", "100", "--account", "paper",
                   "--run", "run-2",
                   "--currency", "USD",
                  ])
    assert rc == 0

    con = store.open()
    run_row = con.execute("SELECT * FROM runs WHERE id = ?", ("run-2",)).fetchone()
    assert run_row is not None
    assert run_row["kind"] == "paper"
    assert run_row["rules_section"] == "Paper — run 2"


def test_run_arg_with_real_account_is_refused(desk_home, capsys):
    """--run with --account real is a contradiction."""
    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
                   "--run", "run-1",
                   "--currency", "USD",
                  ])
    assert rc != 0
    captured = capsys.readouterr()
    assert "real" in captured.err.lower()
    assert "run" in captured.err.lower()


def test_integrity_subcommand_runs_all_checks(desk_home):
    """trading-desk integrity runs integrity.run() and returns its exit code."""
    # Create a database with some events
    cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
              "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
              "--currency", "USD",
             ])

    rc = cli.main(["integrity"])
    assert rc == 0  # Should pass with a clean log


def test_trade_id_uses_local_date_not_utc(desk_home, monkeypatch):
    """Trade ID date comes from local time, not UTC (for day-scoped rules)."""
    from datetime import datetime as dt_datetime

    # Mock datetime to return a fixed local time when UTC is a day ahead
    # Local: 2026-11-10 23:00, UTC: 2026-11-11 04:00
    class MockDatetime:
        @staticmethod
        def now(tz=None):
            from datetime import UTC
            if tz is UTC or tz is UTC:
                return dt_datetime(2026, 11, 11, 4, 0, 0, tzinfo=UTC)
            else:
                # Return mock local datetime
                class MockLocalDatetime:
                    def date(self):
                        class MockDate:
                            def strftime(self, fmt):
                                return "20261110"
                        return MockDate()
                return MockLocalDatetime()

    import desk.cli as cli_module
    monkeypatch.setattr(cli_module, "datetime", MockDatetime)

    cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
              "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
              "--currency", "USD"])

    con = store.open()
    trade_ids = store.trade_ids(con)
    assert len(trade_ids) == 1
    # Should be local date (2026-11-10), not UTC date (2026-11-11)
    assert trade_ids[0] == "acme-20261110"


def test_run_section_not_found_is_refused_by_name(desk_home, capsys):
    """When --run is given but no matching section exists, refuse and name the section wanted."""
    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "12", "--size", "100", "--account", "paper",
                   "--run", "99",
                   "--currency", "USD"])
    assert rc != 0
    captured = capsys.readouterr()
    assert "Paper — run 99" in captured.err
    assert "no section" in captured.err.lower()


def test_run_id_normalization(desk_home):
    """--run 2 and --run run-2 create the same run row."""
    # First check with "2"
    cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
              "--stop", "9", "--target", "12", "--size", "100", "--account", "paper",
              "--run", "2",
              "--currency", "USD"])

    # Second check with "run-2" (should reuse the same run)
    cli.main(["check", "--ticker", "WXYZ", "--side", "short", "--entry", "50",
              "--stop", "52", "--target", "45", "--size", "50", "--account", "paper",
              "--run", "run-2",
              "--currency", "USD"])

    con = store.open()
    runs = con.execute("SELECT id FROM runs").fetchall()
    assert len(runs) == 1
    assert runs[0]["id"] == "run-2"


def test_replay_run_stores_correct_kind_and_section(desk_home):
    """A replay run stores kind='replay' and looks for Replay — ... section."""
    # Add Replay — run 1 section to rules (fixture already has ## Replay)
    rules_file = config.rules_path()
    rules_file.write_text(rules_file.read_text() + """
## Replay — run 1

```rules
min_reward_risk: 3
```
""")

    cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
              "--stop", "9", "--target", "12", "--size", "100", "--account", "replay",
              "--run", "1",
              "--currency", "USD"])

    con = store.open()
    run_row = con.execute("SELECT * FROM runs WHERE id = ?", ("run-1",)).fetchone()
    assert run_row is not None
    assert run_row["kind"] == "replay"
    assert run_row["rules_section"] == "Replay — run 1"


def test_instrument_and_point_value_defaults_reach_trade_row(desk_home):
    """Default instrument=stock and point_value=1.0 are written to the trade row."""
    cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
              "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
              "--currency", "USD"])

    con = store.open()
    trade_ids = store.trade_ids(con)
    trade_row = store.trade(con, trade_ids[0])
    assert trade_row["instrument"] == "stock"
    assert trade_row["point_value"] == 1.0


def test_instrument_future_without_point_value_is_refused(desk_home, capsys):
    """--instrument future without --point-value is refused with explanation."""
    rc = cli.main(["check", "--ticker", "ES", "--side", "long", "--entry", "4000",
                   "--stop", "3980", "--target", "4050", "--size", "1", "--account", "real",
                   "--instrument", "future",
                   "--currency", "USD"])
    assert rc != 0
    captured = capsys.readouterr()
    assert "point value" in captured.err.lower()
    assert "100×" in captured.err


def test_point_value_zero_is_refused(desk_home, capsys):
    """--point-value 0 is refused before attempting to store it (the schema CHECK rejects it)."""
    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
                   "--point-value", "0",
                   "--currency", "USD"])
    assert rc != 0
    captured = capsys.readouterr()
    # Must not be a traceback (no sqlite3.IntegrityError in output)
    assert "IntegrityError" not in captured.err
    assert "Traceback" not in captured.err
    # Must give a clear error about point_value
    assert "point" in captured.err.lower() or "point_value" in captured.err.lower()


def test_malformed_rule_line_exits_cleanly_not_with_traceback(desk_home, capsys):
    """A rule line without a colon raises ValueError, caught not printed as traceback."""
    # Create a rules file with a malformed line
    (config.rules_path()).write_text("""## All

```rules
min_reward_risk 2
```
""")

    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--account", "real", "--currency", "USD"])
    assert rc != 0
    captured = capsys.readouterr()
    # Must not be a traceback
    assert "Traceback" not in captured.err
    # Must give a clear error with the line content
    assert "min_reward_risk 2" in captured.err
    assert "malformed" in captured.err or "expected" in captured.err


def test_ticker_and_currency_are_normalized_to_uppercase(desk_home):
    """--ticker and --currency are upper-cased so case variation doesn't turn a rule off."""
    # Lower-case ticker and currency
    rc = cli.main(["check", "--ticker", "acme", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
                   "--currency", "usd"])
    assert rc == 0

    # The stored trade must have upper-case ticker
    con = store.open()
    trade = store.trade(con, "acme-" + cli.datetime.now().date().strftime("%Y%m%d"))
    assert trade is not None
    assert trade["ticker"] == "ACME"
    assert trade["currency"] == "USD"


def test_unsupported_currency_is_refused_by_name(desk_home, capsys):
    """An unsupported --currency is refused by argparse, not silently withheld."""
    # Argparse exits with SystemExit(2) for invalid arguments
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                  "--stop", "9", "--account", "real", "--currency", "EUR"])
    assert exc_info.value.code == 2

    captured = capsys.readouterr()
    # Must be an argparse error (invalid choice), not a withheld figure
    assert "invalid choice" in captured.err
    # Must name the supported currencies
    assert "CAD" in captured.err and "USD" in captured.err


def test_misspelled_check_name_prints_name_and_value_not_anonymously(desk_home, capsys):
    """A misspelled check name prints with its name and value, not anonymously."""
    # Create a rules file with a misspelled check name
    (config.rules_path()).write_text("""## All

```rules
min_reward_risk: 2
max_risk_per_trde: 40 USD
```
""")

    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
                   "--currency", "USD"])
    assert rc == 0

    captured = capsys.readouterr()
    # The misspelled rule must print with its name and value visible
    assert "max_risk_per_trde: 40 USD" in captured.out
    assert "no automatic check for this one" in captured.out


def test_typo_in_account_heading_is_reported_as_unmatched(desk_home, capsys):
    """A typo'd account heading (e.g., ## Rael) is reported as unrecognised, not silent."""
    (config.rules_path()).write_text("""## All

```rules
min_reward_risk: 2
```

## Rael

```rules
min_reward_risk: 5
max_risk_per_trade: 40 USD
```
""")

    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
                   "--currency", "USD"])
    assert rc == 0

    captured = capsys.readouterr()
    # Must report "Rael" as unrecognised
    assert "Rael" in captured.out
    assert "unrecognised" in captured.out

    # Must apply the looser limit from All (2), not the tighter one from the typo'd Rael (5)
    assert "against a minimum of 2" in captured.out
    assert "against a minimum of 5" not in captured.out


def test_checked_line_names_rules_and_sections_used(desk_home, capsys):
    """The output names which checks applied and from which sections."""
    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
                   "--currency", "USD"])
    assert rc == 0

    captured = capsys.readouterr()
    # Must have a "checked" line naming the rules and sections
    assert "checked" in captured.out.lower() or "from" in captured.out.lower()
    # Must name the rules that applied
    assert "min_reward_risk" in captured.out
    # Must name the sections
    assert "All" in captured.out
    assert "Real" in captured.out


def test_sections_used_is_stored_in_payload_and_json(desk_home, capsys):
    """sections_used is stored in the checked payload and included in --json output."""
    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
                   "--currency", "USD", "--json"])
    assert rc == 0

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert "sections_used" in output
    assert "All" in output["sections_used"]
    assert "Real" in output["sections_used"]

    # Also check the stored payload
    con = store.open()
    (ev,) = [e for e in store.events(con) if e["kind"] == "checked"]
    payload = json.loads(ev["payload"])
    assert "sections_used" in payload
    assert "All" in payload["sections_used"]
    assert "Real" in payload["sections_used"]


def test_prose_output_prints_one_line_per_result_and_nothing_else(desk_home, capsys):
    """Prose output is one line per result, no counts, no summary."""
    cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
              "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
              "--currency", "USD"])

    captured = capsys.readouterr()
    lines = [line for line in captured.out.strip().split("\n") if line]
    # Filter out Ask me lines
    result_lines = [line for line in lines if not line.startswith("Ask me:")]

    # Must have the checked rule (min_reward_risk) with its figure
    assert any("reward to risk" in line and "against a minimum" in line for line in result_lines)

    # Must have the two unchecked rules with their names and values visible
    assert any("max_trades_per_day: 5" in line for line in result_lines)
    assert any("max_position_pct: 10" in line for line in result_lines)

    # No summary words
    assert "passed" not in captured.out.lower()
    assert "failed" not in captured.out.lower()
    assert "total" not in captured.out.lower()


def test_ask_me_questions_are_printed_and_stored(desk_home, capsys):
    """### Ask me questions are printed after results and stored in payload."""
    cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
              "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
              "--currency", "USD"])

    # Check prose output
    captured = capsys.readouterr()
    assert "Ask me:" in captured.out
    # Fixture has these questions in Real section
    assert "Will you check this trade tomorrow?" in captured.out

    # Check stored payload
    con = store.open()
    (ev,) = [e for e in store.events(con) if e["kind"] == "checked"]
    payload = json.loads(ev["payload"])
    assert "ask" in payload
    assert len(payload["ask"]) > 0
    assert "Will you check this trade tomorrow?" in payload["ask"]


def test_json_output_includes_ask_questions(desk_home, capsys):
    """--json output includes ask questions."""
    cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
              "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
              "--currency", "USD", "--json"])

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert "ask" in output
    assert len(output["ask"]) > 0
    assert "Will you check this trade tomorrow?" in output["ask"]


def test_payload_includes_rules_hash(desk_home):
    """The stored payload includes rules_hash."""
    cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
              "--stop", "9", "--target", "12", "--size", "100", "--account", "real",
              "--currency", "USD"])

    con = store.open()
    (ev,) = [e for e in store.events(con) if e["kind"] == "checked"]
    payload = json.loads(ev["payload"])
    assert "rules_hash" in payload
    assert payload["rules_hash"] == rules.load(config.rules_path()).hash


def test_critical_run_2_applies_correct_rules_not_fallback(desk_home, capsys):
    """CRITICAL: --run 2 must apply the run's rules or refuse, not silently fall back to Paper.

    This is the finding-1 reproduction from fix round 3. With All→2, Paper→3, Paper—run 2→9,
    checking with --run 2 must either apply 9 or refuse by name. It must not report 3.
    """
    # Create rules file with All→2, Paper→3, Paper—run 2→9
    rules_file = config.rules_path()
    rules_file.write_text("""## All

```rules
min_reward_risk: 2
```

## Paper

```rules
min_reward_risk: 3
```

## Paper — run 2

```rules
min_reward_risk: 9
```
""")

    # Check with --run 2 (which normalizes to run-2)
    cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
              "--stop", "9", "--target", "12", "--size", "100", "--account", "paper",
              "--run", "2",
              "--currency", "USD"])

    captured = capsys.readouterr()
    # Must report "against a minimum of 9", not 3 or 2
    assert "against a minimum of 9" in captured.out
    # Must NOT report the wrong limits
    assert "against a minimum of 3" not in captured.out
    assert "against a minimum of 2" not in captured.out


def test_cmd_check_creates_home_directory_so_suggested_cp_works(fresh_desk_home, capsys):
    """cmd_check creates the home directory if it doesn't exist, so the suggested cp works.

    The error message for a missing rules.md suggests 'cp <example> <path>', but if the home
    directory doesn't exist, that cp fails with 'No such file or directory'. The fix is to
    create the home directory early in cmd_check.
    """
    import subprocess

    # Attempt check with no rules.md and no home directory
    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--account", "real", "--currency", "USD"])

    # Must fail because rules.md doesn't exist
    assert rc != 0
    captured = capsys.readouterr()
    assert "Rules file not found" in captured.err

    # The suggested cp command must work (the target directory must exist)
    assert "cp" in captured.err

    # Extract the target path from the error message
    rules_path_str = str(config.rules_path())
    assert rules_path_str in captured.err

    # The home directory must now exist (created by cmd_check)
    assert fresh_desk_home.exists(), "cmd_check must create the home directory"

    # The suggested cp should now work
    example_path = config.example_rules()
    result = subprocess.run(
        ["cp", str(example_path), str(config.rules_path())],
        capture_output=True,
    )
    assert result.returncode == 0, f"cp command failed: {result.stderr.decode()}"

    # Now check should work
    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--account", "real", "--currency", "USD"])
    assert rc == 0


def test_check_with_clean_rules_prints_no_unrecognised_line(desk_home, capsys):
    """A rules file with only legitimate sections prints no unrecognised-section line."""
    # desk_home fixture already loads the example rules file, which has only legitimate sections
    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--account", "real", "--currency", "USD"])
    assert rc == 0
    captured = capsys.readouterr()
    # Should not mention unrecognised or unmatched
    assert "unrecognised" not in captured.out
    assert "unmatched" not in captured.out


def test_check_with_typo_section_reports_it_as_unrecognised(desk_home, capsys):
    """A section with a typo (Rael instead of Real) is reported as unrecognised."""
    # Append a typo section to the rules file
    rules_path = config.rules_path()
    with open(rules_path, "a") as f:
        f.write("\n## Rael\nmax_risk_per_trade: 100 USD\n")

    rc = cli.main(["check", "--ticker", "ACME", "--side", "long", "--entry", "10",
                   "--stop", "9", "--account", "real", "--currency", "USD"])
    assert rc == 0
    captured = capsys.readouterr()
    # Should report Rael as unrecognised
    assert "unrecognised section 'Rael'" in captured.out
    assert "matches no account or run" in captured.out
    assert "none of its rules will ever apply" in captured.out


def test_entered_refuses_unknown_trade_id(fresh_desk_home, capsys):
    """entered on an unknown trade id should refuse and exit non-zero."""
    # Create the database first
    store.create()
    rc = cli.main(["entered", "unknown-trade", "--entry", "100", "--stop", "95",
                   "--target", "110", "--size", "10"])
    assert rc != 0
    captured = capsys.readouterr()
    assert "Unknown trade id: unknown-trade" in captured.err


def test_entered_refuses_duplicate_entry(fresh_desk_home, capsys):
    """entered on a trade that already has an entered event should refuse."""
    # Create the database first
    store.create()
    # Create a trade with an entered event
    con = store.open()
    store.add_trade_with_event(
        con,
        trade_id="test-trade",
        account="paper",
        run_id=None,
        ticker=invented.TICKERS[0],
        instrument="stock",
        point_value=1.0,
        currency="USD",
        side="long",
        kind="checked",
        actor="ing",
        source="cli",
        payload={"plan": {}},
        rules_hash="test-hash",
        at="2026-01-01T00:00:00Z",
    )
    store.append(
        con,
        kind="entered",
        actor="ing",
        source="cli",
        trade_id="test-trade",
        payload={
            "entry": 100.0,
            "stop": 95.0,
            "target": 110.0,
            "size": 10,
            "horizon": "1w",
            "setup": "test",
            "thesis": "test",
        },
        at="2026-01-01T01:00:00Z",
    )

    # Try to enter again
    rc = cli.main(["entered", "test-trade", "--entry", "101", "--stop", "96",
                   "--target", "111", "--size", "10"])
    assert rc != 0
    captured = capsys.readouterr()
    assert "already entered on 2026-01-01" in captured.err
    assert "correction" in captured.err


def test_exited_refuses_unknown_trade_id(fresh_desk_home, capsys):
    """exited on an unknown trade id should refuse and exit non-zero."""
    store.create()
    rc = cli.main(["exited", "unknown-trade", "--price", "105", "--size", "10"])
    assert rc != 0
    captured = capsys.readouterr()
    assert "Unknown trade id: unknown-trade" in captured.err


def test_exited_refuses_exit_without_entry(fresh_desk_home, capsys):
    """exited on a trade with no entered event should refuse."""
    store.create()
    # Create a trade without an entered event
    con = store.open()
    store.add_trade_with_event(
        con,
        trade_id="test-trade",
        account="paper",
        run_id=None,
        ticker=invented.TICKERS[0],
        instrument="stock",
        point_value=1.0,
        currency="USD",
        side="long",
        kind="checked",
        actor="ing",
        source="cli",
        payload={"plan": {}},
        rules_hash="test-hash",
        at="2026-01-01T00:00:00Z",
    )

    # Try to exit
    rc = cli.main(["exited", "test-trade", "--price", "105", "--size", "10"])
    assert rc != 0
    captured = capsys.readouterr()
    assert "has no entered event" in captured.err


def test_exited_refuses_over_exit(fresh_desk_home, capsys):
    """exited with size larger than open size should refuse."""
    store.create()
    # Create a trade with 10 shares open
    con = store.open()
    store.add_trade_with_event(
        con,
        trade_id="test-trade",
        account="paper",
        run_id=None,
        ticker=invented.TICKERS[0],
        instrument="stock",
        point_value=1.0,
        currency="USD",
        side="long",
        kind="checked",
        actor="ing",
        source="cli",
        payload={"plan": {}},
        rules_hash="test-hash",
        at="2026-01-01T00:00:00Z",
    )
    store.append(
        con,
        kind="entered",
        actor="ing",
        source="cli",
        trade_id="test-trade",
        payload={
            "entry": 100.0,
            "stop": 95.0,
            "target": 110.0,
            "size": 10,
            "horizon": "1w",
            "setup": "test",
            "thesis": "test",
        },
        at="2026-01-01T01:00:00Z",
    )

    # Try to exit 20 shares (exceeds open size of 10)
    rc = cli.main(["exited", "test-trade", "--price", "105", "--size", "20"])
    assert rc != 0
    captured = capsys.readouterr()
    assert "cannot exit 20 shares" in captured.err
    assert "10" in captured.err


def test_stop_moved_refuses_without_entry(fresh_desk_home, capsys):
    """stop-moved on a trade with no entered event should refuse."""
    store.create()
    # Create a trade without an entered event
    con = store.open()
    store.add_trade_with_event(
        con,
        trade_id="test-trade",
        account="paper",
        run_id=None,
        ticker=invented.TICKERS[0],
        instrument="stock",
        point_value=1.0,
        currency="USD",
        side="long",
        kind="checked",
        actor="ing",
        source="cli",
        payload={"plan": {}},
        rules_hash="test-hash",
        at="2026-01-01T00:00:00Z",
    )

    # Try to move the stop
    rc = cli.main(["stop-moved", "test-trade", "--stop", "96"])
    assert rc != 0
    captured = capsys.readouterr()
    assert "has no entered event" in captured.err


def test_closed_refuses_when_still_holding_size(fresh_desk_home, capsys):
    """closed on a trade still holding size should refuse."""
    store.create()
    # Create a trade with 10 shares open
    con = store.open()
    store.add_trade_with_event(
        con,
        trade_id="test-trade",
        account="paper",
        run_id=None,
        ticker=invented.TICKERS[0],
        instrument="stock",
        point_value=1.0,
        currency="USD",
        side="long",
        kind="checked",
        actor="ing",
        source="cli",
        payload={"plan": {}},
        rules_hash="test-hash",
        at="2026-01-01T00:00:00Z",
    )
    store.append(
        con,
        kind="entered",
        actor="ing",
        source="cli",
        trade_id="test-trade",
        payload={
            "entry": 100.0,
            "stop": 95.0,
            "target": 110.0,
            "size": 10,
            "horizon": "1w",
            "setup": "test",
            "thesis": "test",
        },
        at="2026-01-01T01:00:00Z",
    )

    # Try to close while still holding 10 shares
    rc = cli.main(["closed", "test-trade"])
    assert rc != 0
    captured = capsys.readouterr()
    assert "still holding" in captured.err
    assert "10" in captured.err


def test_full_recording_loop_passes_integrity(desk_home, capsys):
    """A realistic sequence: check, entered, partial exit, full exit, closed passes integrity."""
    # 1. Check a plan
    rc = cli.main([
        "check",
        "--ticker", invented.TICKERS[0],
        "--side", "long",
        "--entry", "100",
        "--stop", "95",
        "--target", "110",
        "--size", "100",
        "--account", "paper",
        "--currency", "USD",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    # Extract trade_id from "recorded as <trade-id>"
    import re
    match = re.search(r"recorded as ([\w-]+)", captured.err)
    assert match, f"Could not find trade id in: {captured.err}"
    trade_id = match.group(1)

    # 2. Enter the trade
    rc = cli.main([
        "entered", trade_id,
        "--entry", "100",
        "--stop", "95",
        "--target", "110",
        "--size", "100",
    ])
    assert rc == 0

    # 3. Check a second plan on the same account to verify max_open_trades counts the first
    rc = cli.main([
        "check",
        "--ticker", invented.TICKERS[1],
        "--side", "long",
        "--entry", "50",
        "--stop", "45",
        "--target", "60",
        "--size", "50",
        "--account", "paper",
        "--currency", "USD",
    ])
    assert rc == 0
    # If max_open_trades is configured, it should now see 1 trade already open
    # (We're not testing the rule itself here, just that the fold is scoped)

    # 4. Partial exit
    rc = cli.main(["exited", trade_id, "--price", "105", "--size", "60"])
    assert rc == 0

    # 5. Exit the rest
    rc = cli.main(["exited", trade_id, "--price", "108", "--size", "40"])
    assert rc == 0

    # 6. Close the trade
    rc = cli.main(["closed", trade_id])
    assert rc == 0

    # 7. Verify integrity passes
    con = store.open()
    violations = integrity.run(con)
    assert violations == 0, "Integrity check should pass after a full recording loop"
