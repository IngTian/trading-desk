"""Tests for saving and reading strategies through the api. Every figure is invented."""
import hashlib
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from desk import api, store, strategy

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


def test_the_current_hash_is_the_latest_versions_text_hash(con):
    v = api.save_strategy(con, "dip-buyer", make(), now=NOW)
    assert v.current_hash == store.versions(con, "dip-buyer")[-1]["text_hash"]
    assert not hasattr(v, "tracker_script") and not hasattr(v, "tracker_limit")


def test_a_second_save_from_the_same_hash_is_refused_after_a_text_edit(con):
    v1 = api.save_strategy(con, "dip-buyer", make(), now=NOW)
    api.save_strategy(con, "dip-buyer", make("Buy dips!"), if_current=v1.current_hash, now=NOW)
    with pytest.raises(api.Refused, match="changed since you read it"):
        api.save_strategy(con, "dip-buyer", make("Buy deep dips."), if_current=v1.current_hash,
                          now=NOW)
    assert len(store.versions(con, "dip-buyer")) == 2


def test_save_strategy_takes_no_tracker_arguments(con):
    with pytest.raises(TypeError):
        api.save_strategy(con, "dip-buyer", make(), tracker_script="print(1)", now=NOW)


def test_an_existing_id_without_if_current_is_refused(con):
    api.save_strategy(con, "dip-buyer", make(), now=NOW)
    with pytest.raises(api.Refused, match="if_current"):
        api.save_strategy(con, "dip-buyer", make("Buy dips!"), now=NOW)


def test_a_new_id_with_if_current_is_refused_and_a_bad_slug_too(con):
    with pytest.raises(api.Refused, match="new"):
        api.save_strategy(con, "dip-buyer", make(), if_current="abc", now=NOW)
    with pytest.raises(api.Refused, match="Dip_Buyer"):
        api.save_strategy(con, "Dip_Buyer", make(), now=NOW)


def test_an_id_over_64_characters_is_refused_as_an_invalid_id(con):
    """Review item 6: a 65-character id passed the slug check, failed the schema's length CHECK
    and came back as "changed by another session"."""
    with pytest.raises(api.Refused, match="1 to 64 lowercase letters"):
        api.save_strategy(con, "a" * 65, make(), now=NOW)
    assert api.list_strategies(con) == []
    assert api.save_strategy(con, "a" * 64, make(), now=NOW).id == "a" * 64


def test_a_stored_version_the_parser_would_now_refuse_is_still_readable(con):
    """Review item 8: reading a strategy re-parsed its stored text, so tightening the parser
    would make old versions, and list_strategies for every strategy, unreadable."""
    api.save_strategy(con, "dip-buyer", make(), now=NOW)
    old = make(rules="timeframes: 1h\nfeels_right: strongly")
    con.execute("INSERT INTO versions (strategy_id, version, text, text_hash, rules, timeframes,"
                " saved_at) VALUES ('dip-buyer', 2, ?, ?, '{\"feels_right\": \"strongly\"}',"
                " '[\"1h\"]', ?)",
                (old, hashlib.sha256(strategy.normalise(old).encode()).hexdigest(),
                 NOW.isoformat()))
    con.commit()
    v = api.get_strategy(con, "dip-buyer")
    assert (v.name, v.version, v.rules) == ("Dip buyer", 2, {"feels_right": "strongly"})
    assert [s.version for s in api.list_strategies(con)] == [2]


def test_an_unknown_id_is_refused_naming_it(con):
    with pytest.raises(api.Refused, match="no-such"):
        api.get_strategy(con, "no-such")
    api.save_strategy(con, "dip-buyer", make(), now=NOW)
    with pytest.raises(api.Refused, match="version 9"):
        api.get_strategy(con, "dip-buyer", 9)


def test_a_failed_save_of_a_new_strategy_leaves_nothing_behind(con):
    with pytest.raises(api.Refused):
        api.save_strategy(con, "dip-buyer", "not a strategy", now=NOW)
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


@pytest.mark.parametrize(("kw", "name"), [
    (dict(markdown=5), "markdown"), (dict(markdown=None), "markdown"), (dict(id=5), "id")])
def test_text_arguments_that_are_not_text_are_refused(con, kw, name):
    """Review item 5: a non-string reached .strip() or a regex and raised a TypeError or an
    AttributeError instead of a refusal."""
    with pytest.raises(api.Refused, match=f"{name} must be text"):
        api.save_strategy(con, **{"id": "dip-buyer", "markdown": make(), "now": NOW, **kw})
    assert api.list_strategies(con) == []
