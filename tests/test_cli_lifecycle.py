"""The recording loop end to end: check, entered, stop-moved, exited, closed, integrity.

Each test here reproduces a defect found by driving the CLI the way a replay session does.
Every figure is invented. A GC-style futures root is used as a generic example of a contract
with a point value; nothing here is a real trade.
"""
from __future__ import annotations

import json
import re

import invented
import pytest

from desk import cli, store

RULES = """\
# Invented rules for the recording-loop tests

## All

```rules
min_reward_risk: 2
stop_required: yes
target_required: yes
```

## Real

```rules
max_risk_per_trade: 100 USD
size: computed
banned: shorts, futures, options
```

## Paper

```rules
max_risk_per_trade: 500 USD
daily_loss_limit: -1R
cooldown_after_stop: 1 day, same ticker
```

## Replay

```rules
max_risk_per_trade: 200 USD
size: computed
max_open_trades: 3
```
"""

GC = ["--ticker", "GC", "--instrument", "future", "--point-value",
      str(invented.FUTURE_POINT_VALUE), "--currency", "USD"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_DESK_HOME", str(tmp_path))
    (tmp_path / "rules.md").write_text(RULES)
    return tmp_path


def run(capsys, *argv):
    """Run the CLI and return (exit code, stdout, stderr)."""
    rc = cli.main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def checked(capsys, *argv) -> str:
    """Run a check and return the trade id it recorded."""
    rc, out, err = run(capsys, "check", *argv)
    assert rc == 0, err
    found = re.search(r"recorded as (\S+)", err)
    assert found, err
    return found.group(1)


def kinds(trade_id):
    return [e["kind"] for e in store.events(store.open(), trade_id=trade_id)]


# --------------------------------------------------------------- the breakeven stop ---


def test_a_stop_moved_to_breakeven_leaves_every_command_working(home, capsys):
    """The 11-11 blocker, step for step: this sequence used to make desk.db unusable."""
    tid = checked(capsys, *GC, "--side", "long", "--entry", "2000", "--stop", "1998.5",
                  "--target", "2003", "--size", "1", "--account", "replay")
    assert run(capsys, "entered", tid, "--entry", "2000", "--stop", "1998.5",
               "--target", "2003", "--size", "1")[0] == 0

    rc, out, _ = run(capsys, "stop-moved", tid, "--stop", "2000")
    assert rc == 0 and "2000" in out

    rc, out, _ = run(capsys, "exited", tid, "--price", "2000", "--size", "1")
    assert rc == 0 and "flat" in out
    assert run(capsys, "closed", tid)[0] == 0
    assert run(capsys, "integrity")[0] == 0
    # A check on another ticker and another account still runs.
    checked(capsys, "--ticker", "ACME", "--currency", "USD", "--side", "long", "--entry", "10",
            "--stop", "9", "--target", "13", "--size", "10", "--account", "paper")


def test_entered_refuses_a_stop_at_the_entry_and_writes_nothing(home, capsys):
    tid = checked(capsys, *GC, "--side", "long", "--entry", "2000", "--stop", "1998.5",
                  "--target", "2003", "--size", "1", "--account", "replay")
    rc, _, err = run(capsys, "entered", tid, "--entry", "2000", "--stop", "2000",
                     "--target", "2003", "--size", "1")
    assert rc == 1
    assert "no risk" in err
    assert kinds(tid) == ["checked"]


# ------------------------------------------------------------------ the cash result ---


def test_exited_states_the_cash_result_in_the_currency_with_the_point_value(home, capsys):
    tid = checked(capsys, *GC, "--side", "long", "--entry", "2000", "--stop", "1998.5",
                  "--target", "2003", "--size", "1", "--account", "replay")
    run(capsys, "entered", tid, "--entry", "2000", "--stop", "1998.5", "--target", "2003",
        "--size", "1")
    rc, out, _ = run(capsys, "exited", tid, "--price", "2004", "--size", "1")
    assert rc == 0
    # 4 points x 100 per point x 1 contract, and 4 points over 1.5 risked.
    assert "+400.00 USD" in out
    assert "+2.67 R" in out


def test_a_partial_exit_states_what_is_still_open(home, capsys):
    tid = checked(capsys, "--ticker", "ACME", "--currency", "USD", "--side", "long",
                  "--entry", "10", "--stop", "9", "--target", "13", "--size", "10",
                  "--account", "paper")
    run(capsys, "entered", tid, "--entry", "10", "--stop", "9", "--target", "13", "--size", "10")
    rc, out, _ = run(capsys, "exited", tid, "--price", "12", "--size", "4")
    assert rc == 0
    assert "6 shares open" in out


# ------------------------------------------------------------- refused at the boundary ---


@pytest.mark.parametrize("size", ["0", "-3"])
def test_exited_refuses_a_size_that_is_not_positive(home, capsys, size):
    with pytest.raises(SystemExit):
        cli.main(["exited", "acme-20260101", "--price", "10", "--size", size])
    assert "positive" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["check", "--ticker", "ACME", "--currency", "USD", "--side", "long", "--entry", "nan",
     "--account", "paper"],
    ["check", "--ticker", "ACME", "--currency", "USD", "--side", "long", "--entry", "10",
     "--stop", "inf", "--account", "paper"],
    ["entered", "acme-20260101", "--entry", "10", "--stop", "-9", "--target", "13",
     "--size", "1"],
    ["exited", "acme-20260101", "--price", "0", "--size", "1"],
    ["stop-moved", "acme-20260101", "--stop", "nan"],
])
def test_a_price_that_is_not_a_positive_number_is_refused_before_anything_is_written(
        home, capsys, argv):
    with pytest.raises(SystemExit):
        cli.main(argv)
    assert "positive" in capsys.readouterr().err
    assert not (home / "desk.db").exists()


def test_the_stop_of_a_flat_trade_cannot_be_moved(home, capsys):
    """A stop moved after the exit used to be able to erase the stop-out's cooldown."""
    tid = checked(capsys, "--ticker", "ACME", "--currency", "USD", "--side", "long",
                  "--entry", "10", "--stop", "9", "--target", "13", "--size", "10",
                  "--account", "paper")
    run(capsys, "entered", tid, "--entry", "10", "--stop", "9", "--target", "13", "--size", "10")
    run(capsys, "exited", tid, "--price", "9", "--size", "10")
    rc, _, err = run(capsys, "stop-moved", tid, "--stop", "8")
    assert rc == 1
    assert "flat" in err
    assert "stop_moved" not in kinds(tid)


# ------------------------------------------------- futures tickers and trade ids ---


def _trade_row(trade_id):
    return store.trade(store.open(), trade_id)


@pytest.mark.parametrize("typed", ["GC1!", "gc1!", "GC=F", "COMEX:GC1!", "GC2!"])
def test_a_futures_ticker_in_any_common_form_is_a_future_on_its_root(home, capsys, typed):
    tid = checked(capsys, "--ticker", typed, "--point-value", "100", "--currency", "USD",
                  "--side", "long", "--entry", "2000", "--stop", "1998.5", "--target", "2003",
                  "--size", "1", "--account", "replay")
    row = _trade_row(tid)
    assert (row["ticker"], row["instrument"], row["point_value"]) == ("GC", "future", 100.0)


@pytest.mark.parametrize(("typed", "root"), [
    ("6E1!", "6E"), ("MNQ1!", "MNQ"), ("ZN=F", "ZN"), ("acme", "ACME"), ("BRK.B", "BRK.B"),
])
def test_canonical_ticker(typed, root):
    ticker, implied = cli.canonical_ticker(typed)
    assert ticker == root
    assert implied == (None if root in ("ACME", "BRK.B") else "future")


def test_a_futures_ticker_without_a_point_value_is_refused(home, capsys):
    """Forgetting --instrument future used to make GC a stock worth 1 per point: 100x too
    little risk, and a pass."""
    rc, _, err = run(capsys, "check", "--ticker", "GC1!", "--currency", "USD", "--side", "long",
                     "--entry", "2000", "--stop", "1998.5", "--target", "2003", "--size", "1",
                     "--account", "replay")
    assert rc == 1
    assert "--point-value" in err
    assert not (home / "desk.db").exists()


def test_a_futures_ticker_cannot_be_checked_as_a_stock(home, capsys):
    rc, _, err = run(capsys, "check", "--ticker", "GC1!", "--instrument", "stock",
                     "--currency", "USD", "--side", "long", "--entry", "2000", "--stop", "1998.5",
                     "--target", "2003", "--size", "1", "--account", "replay")
    assert rc == 1
    assert "future" in err


def test_a_futures_ban_applies_without_the_instrument_flag(home, capsys):
    rc, out, _ = run(capsys, "check", "--json", "--ticker", "GC1!", "--point-value", "100",
                     "--currency", "USD", "--side", "long", "--entry", "2000",
                     "--stop", "1998.5", "--target", "2003", "--size", "1", "--account", "real")
    assert rc == 0
    (ban,) = [r for r in json.loads(out)["results"] if r["name"] == "banned"]
    assert ban["ok"] is False


def test_a_trade_id_is_safe_to_type_unquoted(home, capsys):
    """'gc1!-20261111' needed quoting in zsh, where ! is history expansion."""
    tid = checked(capsys, "--ticker", "GC1!", "--point-value", "100", "--currency", "USD",
                  "--side", "long", "--entry", "2000", "--stop", "1998.5", "--target", "2003",
                  "--size", "1", "--account", "replay")
    assert re.fullmatch(r"[a-z0-9]+-\d{8}", tid), tid


def test_a_stop_out_on_the_root_cools_down_the_continuous_ticker(home, capsys):
    """Typing GC once and GC1! the next time split the cooldown in two."""
    tid = checked(capsys, *GC, "--side", "long", "--entry", "2000", "--stop", "1998.5",
                  "--target", "2003", "--size", "1", "--account", "paper")
    run(capsys, "entered", tid, "--entry", "2000", "--stop", "1998.5", "--target", "2003",
        "--size", "1")
    run(capsys, "exited", tid, "--price", "1998.5", "--size", "1")

    rc, out, _ = run(capsys, "check", "--json", "--ticker", "GC1!", "--point-value", "100",
                     "--currency", "USD", "--side", "long", "--entry", "2000",
                     "--stop", "1998.5", "--target", "2003", "--size", "1", "--account", "paper")
    (cooldown,) = [r for r in json.loads(out)["results"] if r["name"] == "cooldown_after_stop"]
    assert cooldown["ok"] is False


def test_a_trade_id_is_matched_whatever_its_case(home, capsys):
    tid = checked(capsys, "--ticker", "ACME", "--currency", "USD", "--side", "long",
                  "--entry", "10", "--stop", "9", "--target", "13", "--size", "10",
                  "--account", "paper")
    rc, _, err = run(capsys, "entered", tid.upper(), "--entry", "10", "--stop", "9",
                     "--target", "13", "--size", "10")
    assert rc == 0, err
    assert kinds(tid) == ["checked", "entered"]


# ---------------------------------------------------------------- the check's output ---


def test_every_result_line_is_marked_with_its_state(home, capsys):
    """A breach printed exactly like a pass, so mid-session every line had to be read
    against its limit by eye. The exit code stays 0: a result is a fact, never a block."""
    rc, out, _ = run(capsys, "check", *GC, "--side", "long", "--entry", "2000",
                     "--stop", "1997", "--target", "2004", "--size", "1", "--account", "replay")
    assert rc == 0
    lines = out.splitlines()
    assert any(ln.startswith("BREACH") and "reward to risk 1.33" in ln for ln in lines)
    assert any(ln.startswith("BREACH") and "risk 300.00 USD" in ln for ln in lines)
    assert any(ln.startswith("pass") and "the stop 1997" in ln for ln in lines)


def test_a_withheld_figure_is_marked_unknown_and_an_unchecked_rule_ask(home, capsys):
    (home / "rules.md").write_text(
        RULES + "\n## Paper — run 1\n\n```rules\nfeels_right: yes\n```\n")
    rc, out, _ = run(capsys, "check", "--ticker", "ACME", "--currency", "USD", "--side", "long",
                     "--entry", "10", "--target", "13", "--account", "paper", "--run", "1")
    assert rc == 0
    lines = out.splitlines()
    assert any(ln.startswith("unknown") and "no stop" in ln for ln in lines)
    assert any(ln.startswith("ask") and "feels_right: yes" in ln for ln in lines)


def test_a_missing_account_section_is_said_in_prose_json_and_the_record(home, capsys):
    """With no '## Replay', a replay trade got All's rules alone and nothing said so."""
    (home / "rules.md").write_text(RULES.split("## Replay")[0])
    rc, out, _ = run(capsys, "check", *GC, "--side", "long", "--entry", "2000",
                     "--stop", "1998.5", "--target", "2003", "--size", "1", "--account", "replay")
    assert rc == 0
    assert "no '## Replay' section" in out

    rc, out, err = run(capsys, "check", "--json", *GC, "--side", "long", "--entry", "2000",
                       "--stop", "1998.5", "--target", "2003", "--size", "1",
                       "--account", "replay")
    (warning,) = json.loads(out)["warnings"]
    assert "no '## Replay' section" in warning
    tid = re.search(r"recorded as (\S+)", err).group(1)
    (checked_event,) = store.events(store.open(), trade_id=tid)
    assert json.loads(checked_event["payload"])["warnings"] == [warning]


def test_an_unrecognised_section_is_in_the_json_too(home, capsys):
    (home / "rules.md").write_text(
        RULES + "\n## Real — run 9\n\n```rules\nmin_reward_risk: 1\n```\n")
    rc, out, _ = run(capsys, "check", "--json", "--ticker", "ACME", "--currency", "USD",
                     "--side", "long", "--entry", "10", "--stop", "9", "--target", "13",
                     "--size", "10", "--account", "paper")
    assert rc == 0
    assert any("Real — run 9" in w for w in json.loads(out)["warnings"])


def test_a_run_that_names_no_single_run_is_refused_cleanly(home, capsys):
    rc, _, err = run(capsys, "check", "--ticker", "ACME", "--currency", "USD", "--side", "long",
                     "--entry", "10", "--stop", "9", "--target", "13", "--account", "paper",
                     "--run", "3b")
    assert rc == 1
    assert "does not name a run" in err
    assert "Traceback" not in err


# ------------------------------------------------------------------- daily loss ---


def test_daily_loss_limit_reads_the_r_realised_today(home, capsys):
    tid = checked(capsys, "--ticker", "ACME", "--currency", "USD", "--side", "long",
                  "--entry", "10", "--stop", "9", "--target", "13", "--size", "10",
                  "--account", "paper")
    run(capsys, "entered", tid, "--entry", "10", "--stop", "9", "--target", "13", "--size", "10")
    run(capsys, "exited", tid, "--price", "8.5", "--size", "10")  # -1.5R

    rc, out, _ = run(capsys, "check", "--json", "--ticker", "WXYZ", "--currency", "USD",
                     "--side", "long", "--entry", "50", "--stop", "45", "--target", "60",
                     "--size", "10", "--account", "paper")
    assert rc == 0
    (daily,) = [r for r in json.loads(out)["results"] if r["name"] == "daily_loss_limit"]
    assert daily["figure"] == pytest.approx(-1.5)
    assert daily["ok"] is False


def test_the_cooldown_starts_at_the_stop_out_without_a_closed_event(home, capsys):
    tid = checked(capsys, "--ticker", "ACME", "--currency", "USD", "--side", "long",
                  "--entry", "10", "--stop", "9", "--target", "13", "--size", "10",
                  "--account", "paper")
    run(capsys, "entered", tid, "--entry", "10", "--stop", "9", "--target", "13", "--size", "10")
    run(capsys, "exited", tid, "--price", "9", "--size", "10")

    rc, out, _ = run(capsys, "check", "--json", "--ticker", "ACME", "--currency", "USD",
                     "--side", "long", "--entry", "10", "--stop", "9", "--target", "13",
                     "--size", "10", "--account", "paper")
    (cooldown,) = [r for r in json.loads(out)["results"] if r["name"] == "cooldown_after_stop"]
    assert cooldown["ok"] is False
