"""Tests for starting, stopping and listing runs through the api. Every figure is invented."""
from datetime import UTC, datetime, timedelta

import pytest

from desk import api, store

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=3)


def make(prose: str = "Buy dips."):
    return (f"---\nname: Dip buyer\n---\n\n# Dip buyer\n\n{prose}\n\n"
            "```rules\ntimeframes: 1h\nmin_reward_risk: 2\n```\n")


@pytest.fixture
def con(tmp_path):
    db = tmp_path / "desk.db"
    store.create(db)
    with store.open(db) as conn:
        api.save_strategy(conn, "dip-buyer", make(), now=NOW)
        yield conn


def test_a_live_run_starts_at_now_on_the_latest_version(con):
    v1 = api.get_strategy(con, "dip-buyer")
    api.save_strategy(con, "dip-buyer", make("Buy deeper dips."), if_current=v1.current_hash,
                      now=NOW)
    r = api.start_run(con, "dip-buyer", mode="live", tags=["paper", "GC 15m", "paper"], now=NOW)
    assert (r.id, r.version, r.mode, r.started, r.stopped) == ("dip-buyer-r1", 2, "live", NOW, None)
    assert r.tags == ("paper", "GC 15m")
    assert api.start_run(con, "dip-buyer", mode="live", version=1, now=NOW).id == "dip-buyer-r2"
    assert [x.id for x in api.list_runs(con, "dip-buyer")] == ["dip-buyer-r1", "dip-buyer-r2"]
    assert api.list_runs(con, "other") == []


def test_starting_writes_the_row_and_the_event_together(con):
    api.start_run(con, "dip-buyer", mode="live", now=NOW)
    assert [e["kind"] for e in store.events(con, run_id="dip-buyer-r1")] == ["run_started"]


def test_a_replay_start_without_at_is_refused_and_writes_nothing(con):
    with pytest.raises(api.Refused, match="at"):
        api.start_run(con, "dip-buyer", mode="replay", now=NOW)
    assert store.runs(con) == []
    r = api.start_run(con, "dip-buyer", mode="replay", at=NOW - timedelta(days=30), now=NOW)
    assert r.started == NOW - timedelta(days=30)


@pytest.mark.parametrize("kw, match", [
    ({"strategy": "nope", "mode": "live"}, "no strategy"),
    ({"strategy": "dip-buyer", "mode": "live", "version": 9}, "no version 9"),
    ({"strategy": "dip-buyer", "mode": "paper"}, "mode"),
])
def test_start_refusals(con, kw, match):
    with pytest.raises(api.Refused, match=match):
        api.start_run(con, now=NOW, **kw)
    assert store.runs(con) == []


def test_stop_live_defaults_to_now_and_a_second_stop_is_refused(con):
    api.start_run(con, "dip-buyer", mode="live", now=NOW)
    r = api.stop_run(con, "dip-buyer-r1", now=LATER)
    assert r.stopped == LATER and r.started == NOW
    with pytest.raises(api.Refused, match="already stopped"):
        api.stop_run(con, "dip-buyer-r1", now=LATER)
    assert api.list_runs(con)[0].stopped == LATER


def test_stop_before_start_is_refused(con):
    api.start_run(con, "dip-buyer", mode="live", now=NOW)
    with pytest.raises(api.Refused, match="before"):
        api.stop_run(con, "dip-buyer-r1", at=NOW - timedelta(minutes=1), now=LATER)
    assert api.stop_run(con, "dip-buyer-r1", at=NOW, now=LATER).stopped == NOW


def test_replay_stop_without_at_is_refused(con):
    api.start_run(con, "dip-buyer", mode="replay", at=NOW - timedelta(days=30), now=NOW)
    with pytest.raises(api.Refused, match="at"):
        api.stop_run(con, "dip-buyer-r1", now=LATER)
    assert api.list_runs(con)[0].stopped is None


def test_unknown_run_is_refused(con):
    with pytest.raises(api.Refused, match="no run"):
        api.stop_run(con, "ghost-r1", now=NOW)


def test_a_naive_datetime_is_refused_for_start_and_stop(con):
    naive = datetime(2026, 3, 2, 12, 0)
    with pytest.raises(api.Refused, match="timezone"):
        api.start_run(con, "dip-buyer", mode="replay", at=naive, now=NOW)
    api.start_run(con, "dip-buyer", mode="live", now=NOW)
    with pytest.raises(api.Refused, match="timezone"):
        api.stop_run(con, "dip-buyer-r1", at=naive, now=LATER)


@pytest.mark.parametrize("tags", ["paper", [1, None], ["paper", 5], 7])
def test_tags_that_are_not_a_list_of_strings_are_refused(con, tags):
    """Review item 5: tags="paper" was stored as ('p', 'a', 'p', 'e', 'r'), for good."""
    with pytest.raises(api.Refused, match="tags must be a list of strings"):
        api.start_run(con, "dip-buyer", mode="live", tags=tags, now=NOW)
    assert store.runs(con) == []


def test_no_tags_may_be_given_as_none(con):
    assert api.start_run(con, "dip-buyer", mode="live", tags=None, now=NOW).tags == ()
