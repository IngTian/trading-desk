"""Tests for saving and reading strategies through the api. Every figure is invented."""
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from desk import api, store

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)


def make(prose: str = "Buy dips.", rules: str = "timeframes: 1h, 15m\nmin_reward_risk: 2"):
    return f"---\nname: Dip buyer\n---\n\n# Dip buyer\n\n{prose}\n\n```rules\n{rules}\n```\n"


@pytest.fixture
def con(tmp_path):
    db = tmp_path / "desk.db"
    store.create(db)
    with store.open(db) as conn:
        yield conn


def test_a_new_strategy_is_version_one(con):
    v = api.save_strategy(con, "dip-buyer", make(), now=NOW)
    assert (v.version, v.versions, v.saved, v.name) == (1, 1, "version 1 created", "Dip buyer")
    assert v.timeframes == ("15m", "1h") and v.rules == {"min_reward_risk": "2"}
    assert api.get_strategy(con, "dip-buyer") == replace(v, saved="unchanged")
    assert [s.id for s in api.list_strategies(con)] == ["dip-buyer"]


def test_a_whitespace_resave_is_unchanged_and_writes_nothing(con):
    v = api.save_strategy(con, "dip-buyer", make(), now=NOW)
    again = api.save_strategy(con, "dip-buyer", make("Buy   dips.  "),
                              if_current=v.current_hash, now=NOW)
    assert again.saved == "unchanged" and again.versions == 1
    assert again.current_hash == v.current_hash
    assert len(store.versions(con, "dip-buyer")) == 1


def test_a_prose_edit_makes_version_two_and_keeps_version_one_readable(con):
    v1 = api.save_strategy(con, "dip-buyer", make(), now=NOW)
    v2 = api.save_strategy(con, "dip-buyer", make("Buy dips!"), if_current=v1.current_hash, now=NOW)
    assert (v2.version, v2.versions, v2.saved) == (2, 2, "version 2 created")
    old = api.get_strategy(con, "dip-buyer", 1)
    assert old.version == 1 and "Buy dips." in old.text and old.versions == 2


def test_a_tracker_only_edit_keeps_the_version_and_changes_the_hash(con):
    v1 = api.save_strategy(con, "dip-buyer", make(), now=NOW)
    v2 = api.save_strategy(con, "dip-buyer", make(), tracker_script="print(1)",
                           tracker_limit="40 bars after exit", if_current=v1.current_hash, now=NOW)
    assert v2.version == 1 and v2.saved == "tracker updated"
    assert v2.current_hash != v1.current_hash
    assert (v2.tracker_script, v2.tracker_limit) == ("print(1)", "40 bars after exit")
    same = api.save_strategy(con, "dip-buyer", make(), tracker_script="print(1)",
                             tracker_limit="40 bars after exit", if_current=v2.current_hash,
                             now=NOW)
    assert same.saved == "unchanged"


def test_a_second_save_from_the_same_hash_is_refused_after_a_tracker_only_save(con):
    v1 = api.save_strategy(con, "dip-buyer", make(), now=NOW)
    api.save_strategy(con, "dip-buyer", make(), tracker_script="print(1)",
                      tracker_limit="2h after exit", if_current=v1.current_hash, now=NOW)
    with pytest.raises(api.Refused, match="changed since you read it"):
        api.save_strategy(con, "dip-buyer", make("Buy dips!"), if_current=v1.current_hash, now=NOW)
    assert len(store.versions(con, "dip-buyer")) == 1


def test_an_existing_id_without_if_current_is_refused(con):
    api.save_strategy(con, "dip-buyer", make(), now=NOW)
    with pytest.raises(api.Refused, match="if_current"):
        api.save_strategy(con, "dip-buyer", make("Buy dips!"), now=NOW)


def test_a_new_id_with_if_current_is_refused_and_a_bad_slug_too(con):
    with pytest.raises(api.Refused, match="new"):
        api.save_strategy(con, "dip-buyer", make(), if_current="abc", now=NOW)
    with pytest.raises(api.Refused, match="Dip_Buyer"):
        api.save_strategy(con, "Dip_Buyer", make(), now=NOW)


def test_an_unknown_id_is_refused_naming_it(con):
    with pytest.raises(api.Refused, match="no-such"):
        api.get_strategy(con, "no-such")
    api.save_strategy(con, "dip-buyer", make(), now=NOW)
    with pytest.raises(api.Refused, match="version 9"):
        api.get_strategy(con, "dip-buyer", 9)


@pytest.mark.parametrize("limit", ["soon", "0 bars after exit", "1.5 bars after exit",
                                   "40 bars", "2 parsecs after exit"])
def test_a_bad_tracker_limit_is_refused_and_nothing_is_written(con, limit):
    with pytest.raises(api.Refused, match="tracker_limit"):
        api.save_strategy(con, "dip-buyer", make(), tracker_script="x", tracker_limit=limit,
                          now=NOW)
    assert api.list_strategies(con) == []


def test_a_limit_without_a_script_is_refused(con):
    with pytest.raises(api.Refused, match="script"):
        api.save_strategy(con, "dip-buyer", make(), tracker_limit="40 bars after exit", now=NOW)


def test_a_failed_save_of_a_new_strategy_leaves_nothing_behind(con):
    with pytest.raises(api.Refused):
        api.save_strategy(con, "dip-buyer", "not a strategy", tracker_script="x", now=NOW)
    assert con.execute("SELECT count(*) FROM strategies").fetchone()[0] == 0


def test_the_stale_check_reads_under_the_write_lock(con, monkeypatch):
    v = api.save_strategy(con, "dip-buyer", make(), now=NOW)
    seen = []
    real = store.versions
    monkeypatch.setattr(store, "versions",
                        lambda c, i: (seen.append(c.in_transaction), real(c, i))[1])
    api.save_strategy(con, "dip-buyer", make("Buy dips!"), if_current=v.current_hash, now=NOW)
    assert seen[0] is True, "reads and the if_current comparison must sit inside the transaction"


def test_a_writer_holding_the_lock_blocks_a_save(con, tmp_path):
    import sqlite3
    other = sqlite3.connect(tmp_path / "desk.db", isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    con.execute("PRAGMA busy_timeout = 50")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        api.save_strategy(con, "dip-buyer", make(), now=NOW)
    other.close()


def test_a_naive_now_is_refused_on_save(con):
    with pytest.raises(api.Refused, match="timezone"):
        api.save_strategy(con, "dip-buyer", make(), now=datetime(2026, 3, 2))
    assert api.list_strategies(con) == []
