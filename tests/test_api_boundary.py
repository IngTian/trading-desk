"""The API's boundary, as an MCP client reaches it: every value is JSON-shaped. Invented figures."""
from datetime import UTC, date, datetime, time, timedelta

import pytest

from desk import api, rules, store
from desk.errors import Refused, shown

NOW = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
START = NOW - timedelta(days=1)
TEXT = '---\nname: Tester\n---\n\n```rules\ntimeframes = ["15m"]\n```\n'
ENTRY = dict(ticker="ACME", side="long", entry_type="market", entry=100.0, size=1,
             currency="USD", instrument="stock", reasons="Invented.")


@pytest.fixture
def con(tmp_path):
    store.create(tmp_path / "desk.db")
    with store.open(tmp_path / "desk.db") as c:
        api.save_strategy(c, "tester", TEXT, now=START)
        yield c


def replay(con):
    return api.start_run(con, "tester", mode="replay", currency="USD", at=START, now=NOW).id


def test_shown_writes_json_and_cuts_long_values():
    assert [shown(v) for v in (True, None, "x", {"a": 1}, [1.5])] == [
        "true", "null", '"x"', '{"a": 1}', "[1.5]"]
    assert shown(datetime(2026, 3, 2, 14, 30)) == "2026-03-02T14:30:00"
    assert shown(date(1979, 5, 27)) == "1979-05-27" and shown(time(7, 32)) == "07:32:00"
    assert len(shown(10**400)) == 80 and shown(10**400).endswith("…")


def test_at_is_read_from_an_iso_string_with_its_offset(con):
    a = api.start_run(con, "tester", mode="replay", currency="USD", at="2026-03-02T14:30:00Z",
                      now=NOW)
    b = api.start_run(con, "tester", mode="replay", currency="USD",
                      at="2026-03-02T09:30:00-05:00", now=NOW)
    assert a.started == b.started == datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
    t = api.trade(con, a.id, **ENTRY, at="2026-03-02T14:45:00+00:00", now=NOW)
    fixed = api.correct_trade(con, t.id, fields={"at": "2026-03-02T14:40:00Z"},
                              reason="Invented.", now=NOW)
    assert fixed.decisions.entry.at == datetime(2026, 3, 2, 14, 40, tzinfo=UTC)
    out = api.report(con, t.id, state="exited", price=101.0, at="2026-03-02T14:55:00Z",
                     reason="Invented.", now=NOW).outcome
    assert out.exited_at == datetime(2026, 3, 2, 14, 55, tzinfo=UTC)


@pytest.mark.parametrize("at", ["2026-03-02T14:30:00", "2026-03-02", "yesterday", 5, True,
                                ["2026-03-02T14:30:00Z"], "0001-01-01T00:00:00+01:00",
                                "9999-12-31T23:59:59-01:00"])
def test_an_at_without_an_offset_or_not_a_time_is_refused(con, at):
    with pytest.raises(Refused, match=r"ISO-8601 time with its UTC offset, e\.g\. "
                                      r"2026-03-02T14:30:00Z; got "):
        api.start_run(con, "tester", mode="replay", currency="USD", at=at, now=NOW)


def test_a_strategy_or_run_id_must_be_text(con):
    calls = [
        lambda: api.get_strategy(con, ["tester"]),
        lambda: api.start_run(con, {"a": 1}, mode="replay", currency="USD", at=NOW, now=NOW),
        lambda: api.stop_run(con, ["x"], at=NOW, now=NOW),
        lambda: api.list_runs(con, True),
        lambda: api.trade(con, 5, **ENTRY, at=NOW, now=NOW),
    ]
    for call in calls:
        with pytest.raises(Refused, match="must be text, a string; got "):
            call()


@pytest.mark.parametrize("v", [True, "1", 1.0, 0, -1, [1]])
def test_a_version_is_a_whole_number_from_1(con, v):
    calls = [
        lambda: api.get_strategy(con, "tester", v),
        lambda: api.start_run(con, "tester", mode="replay", currency="USD", version=v,
                              at=NOW, now=NOW),
        lambda: api.stats(con, strategy="tester", version=v, now=NOW),
    ]
    for call in calls:
        with pytest.raises(Refused, match="version must be a whole number from 1"):
            call()


def test_if_current_must_be_text(con):
    with pytest.raises(Refused, match="if_current must be text"):
        api.save_strategy(con, "tester", TEXT, if_current=5, now=NOW)


def test_an_empty_id_lists_nothing(con):
    api.trade(con, replay(con), **ENTRY, at=START, now=NOW)
    assert api.list_runs(con, "") == [] and len(api.list_runs(con)) == 1
    assert api.list_trades(con, run="", now=NOW) == []
    assert len(api.list_trades(con, now=NOW)) == 1


def test_trade_bars_before_and_after_are_at_most_10000(con):
    run = api.start_run(con, "tester", mode="live", currency="USD", at=START, now=START).id
    t = api.trade(con, run, **ENTRY, at=START, now=START)
    for name in ("before", "after"):
        with pytest.raises(Refused, match="from 0 to 10000"):
            api.trade_bars(con, t.id, **{name: 10_001}, now=NOW)
    assert api.trade_bars(con, t.id, before=10_000, after=10_000, now=NOW).trade == t.id


@pytest.mark.parametrize("move", [True, "0", [0], -1, 1.5])
def test_a_correction_whose_move_is_not_a_place_is_refused_as_such(con, move):
    t = api.trade(con, replay(con), **ENTRY, stop=99.0, at=START, now=NOW)
    with pytest.raises(Refused, match="move must be a whole number from 0"):
        api.correct_trade(con, t.id, fields={"move": move, "stop": 98.0}, reason="Invented.",
                          now=NOW)


def test_a_relative_screenshot_path_is_refused_even_if_it_exists(con, tmp_path, monkeypatch):
    (tmp_path / "shot.png").write_bytes(b"\x89PNG invented")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Refused, match="is not a full path"):
        api.trade(con, replay(con), **ENTRY, screenshots=["shot.png"], at=START, now=NOW)
    t = api.trade(con, replay(con), **ENTRY, screenshots=[str(tmp_path / "shot.png")],
                  at=START, now=NOW)
    assert t.decisions.screenshots[0]["name"] == "shot.png"


def test_a_screenshot_under_an_unknown_users_home_is_refused_as_not_a_full_path(con):
    with pytest.raises(Refused, match="is not a full path"):
        api.trade(con, replay(con), **ENTRY, screenshots=["~nosuchuser9/shot.png"], at=START,
                  now=NOW)


def test_an_indicator_setting_of_1000_saves():
    rules.validate({"rsi": {"length": 1000, "at_most": 30}}, ("15m",))
