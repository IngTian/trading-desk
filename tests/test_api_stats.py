"""api.stats: one scope, type-checked filters, narrowing before any trade is read, and
conforming_only from the results now. Every figure is invented."""
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from desk import api, bars, store

NOW = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
START = NOW - timedelta(days=30)
BASE = dict(ticker="ACME", side="long", entry_type="market", entry=100.0, stop=98.0,
            target=104.0, size=10, currency="USD", reasons="Invented reasons.")
NO_SCOPE = ("stats needs one scope: a run, a strategy, or a strategy with a version; every "
            "strategy pooled would be account-level P&L, which the desk does not report")


def h(n):
    return NOW - timedelta(hours=n)


def strategy_text(rules='timeframes = ["15m"]\n'):
    return f"---\nname: Stats tester\n---\n\n```rules\n{rules}```\n"


@pytest.fixture
def con(tmp_path):
    """Strategy s, version 1 without rules, and replay run s-r1 in USD."""
    store.create(tmp_path / "desk.db")
    with store.open(tmp_path / "desk.db") as c:
        api.save_strategy(c, "s", strategy_text(), now=START)
        api.start_run(c, "s", mode="replay", currency="USD", at=START, now=START)
        yield c


def version(con, rules):
    current = api.get_strategy(con, "s").current_hash
    return api.save_strategy(con, "s", strategy_text(rules), if_current=current,
                             now=START).version


def run(con, mode="replay", currency="USD", tags=(), version=None):
    return api.start_run(con, "s", mode=mode, currency=currency, tags=list(tags), version=version,
                         at=START, now=START).id


def enter(con, run="s-r1", **kw):
    return api.trade(con, run, **{**BASE, "at": h(10), "now": NOW, **kw})


def out(con, t, price, at):
    api.report(con, t.id, state="exited", kind="hand", price=price, at=at, reason="Invented.",
               now=NOW)


def test_stats_needs_exactly_one_scope(con):
    with pytest.raises(api.Refused) as e:
        api.stats(con, now=NOW)
    assert str(e.value) == NO_SCOPE
    with pytest.raises(api.Refused, match="named with its strategy"):
        api.stats(con, version=1, now=NOW)
    for kw in ({"strategy": "s"}, {"version": 1}, {"strategy": "s", "version": 1}):
        with pytest.raises(api.Refused, match="a run alone"):
            api.stats(con, run="s-r1", **kw, now=NOW)
    with pytest.raises(api.Refused, match="There is no strategy 'nope'; save it first"):
        api.stats(con, strategy="nope", now=NOW)
    with pytest.raises(api.Refused, match="Strategy 's' has no version 9; it has versions 1 to 1"):
        api.stats(con, strategy="s", version=9, now=NOW)
    with pytest.raises(api.Refused, match="There is no run 's-r9'; list_runs"):
        api.stats(con, run="s-r9", now=NOW)


@pytest.mark.parametrize("kw, match", [
    ({"strategy": "s", "version": True}, "whole number"),
    ({"strategy": "s", "version": "1"}, "whole number"),
    ({"run": "s-r1", "mode": "paper"}, "live"),
    ({"run": "s-r1", "tags": "paper"}, "list of strings"),
    ({"run": "s-r1", "conforming_only": "yes"}, "true, false"),
    ({"run": 1}, "must be text"),
    ({"strategy": ["s"]}, "must be text")])
def test_stats_parameters_are_type_checked(con, kw, match):
    with pytest.raises(api.Refused, match=match):
        api.stats(con, **kw, now=NOW)


def test_stats_per_run_per_version_and_per_strategy(con):
    a, b = enter(con, at=h(10)), enter(con, at=h(9))
    out(con, a, 104.0, h(8))     # +2 R, +40
    out(con, b, 99.0, h(7))      # -0.5 R, -10
    v2 = version(con, 'timeframes = ["15m", "1h"]\n')
    cad, usd = run(con, currency="CAD"), run(con)
    c = enter(con, cad, at=h(6), currency="CAD")
    out(con, c, 102.0, h(5))     # +1 R, +20 CAD
    d = enter(con, usd, at=h(4))

    s = api.stats(con, run="s-r1", now=NOW)
    assert (s.strategy, s.version, s.run) == ("s", 1, "s-r1")
    assert (s.trades, s.wins, s.losses, s.open) == ((a.id, b.id), (a.id,), (b.id,), ())
    assert (s.win_rate, s.total_r, s.average_r, s.pnl) == (0.5, 1.5, 0.75, {"USD": 30.0})
    assert s.average_planned_rr == 2.0

    s = api.stats(con, strategy="s", version=v2, now=NOW)
    assert (s.strategy, s.version, s.run) == ("s", 2, None)
    assert (s.trades, s.closed, s.open, s.pnl) == ((c.id, d.id), (c.id,), (d.id,), {"CAD": 20.0})
    assert s.rests_on["version"] == {2: 2}

    s = api.stats(con, strategy="s", now=NOW)
    assert (s.version, s.run, s.trades) == (None, None, (a.id, b.id, c.id, d.id))
    assert s.pnl == {"USD": 30.0, "CAD": 20.0} and s.rests_on["version"] == {1: 2, 2: 2}
    assert s.rests_on["mode"] == {"replay": 4} and s.left_out == {}


def test_mode_and_tags_narrow_exactly(con, monkeypatch):
    swing, cap, live = run(con, tags=["paper", "swing"]), run(con, tags=["Paper"]), run(
        con, "live", tags=["paper"])
    ids = {r: enter(con, r).id for r in ("s-r1", swing, cap)}
    ids[live] = enter(con, live).id      # the network is off: not judged

    def trades(**kw):
        return api.stats(con, strategy="s", now=NOW, **kw).trades
    assert trades(tags=["paper"]) == (ids[swing], ids[live])
    assert trades(tags=["Paper"]) == (ids[cap],)
    assert trades(tags=["paper", "swing"]) == (ids[swing],)
    assert trades(tags=["swing", "Paper"]) == ()
    assert trades(mode="live") == (ids[live],)
    assert api.stats(con, strategy="s", mode="live", now=NOW).not_judged.keys() == {ids[live]}
    assert trades(mode="replay", tags=["paper"]) == (ids[swing],)
    s = api.stats(con, strategy="s", tags=["paper"], now=NOW)
    assert (s.tags, s.mode) == (("paper",), None)
    assert s.rests_on["tags"] == [{"tags": ["paper", "swing"], "trades": 1},
                                  {"tags": ["paper"], "trades": 1}]

    read = []    # narrowing comes before any trade is read
    real = api._trade_view
    monkeypatch.setattr(api, "_trade_view", lambda c, row, now: read.append(row["id"]) or real(
        c, row, now))
    api.stats(con, strategy="s", mode="replay", tags=["Paper"], now=NOW)
    assert read == [ids[cap]]


def test_void_trades_leave_every_figure_and_the_fill_rate(con):
    a = enter(con, at=h(10))
    out(con, a, 104.0, h(9))
    b = enter(con, at=h(8))
    out(con, b, 99.0, h(7))
    lim = enter(con, at=h(6), entry_type="limit", entry=99.0)
    for t in (b, lim):
        api.void_trade(con, t.id, reason="Invented.", now=NOW)
    s = api.stats(con, run="s-r1", now=NOW)
    assert (s.trades, s.closed, s.losses, s.pnl) == ((a.id,), (a.id,), (), {"USD": 40.0})
    assert (s.limits["placed"], s.limits["rate"], s.pending) == ((), None, ())


def test_conforming_only_reads_the_results_now_and_names_what_it_left_out(con, monkeypatch):
    version(con, 'timeframes = ["15m"]\nmin_reward_risk = 2\nmax_open_trades = 1\n')
    r = run(con)
    k = enter(con, r, at=h(10))                                       # kept, exits at +2 R
    out(con, k, 104.0, h(9))
    b = enter(con, r, at=h(8), target=103.0)                          # reward to risk 1.5
    out(con, b, 101.0, h(7))
    u = enter(con, r, at=h(6), entry_type="limit", entry=99.0, target=None)   # no target
    api.report(con, u.id, state="cancelled", why="not filled", at=h(5), reason="Invented.",
               now=NOW)
    x = enter(con, r, at=h(4))                                        # kept, still open
    y = enter(con, r, at=h(3), target=None)       # unknown reward to risk, and a second open

    built = []
    real = api._other
    monkeypatch.setattr(api, "_other",
                        lambda *a, **kw: built.append(a[0]["id"]) or real(*a, **kw))
    s = api.stats(con, run=r, conforming_only=True, now=NOW)
    assert len(built) == 5     # each run's Others once, never once per pair of trades
    assert (s.conforming_only, s.trades, s.closed, s.open) == (True, (k.id, x.id), (k.id,),
                                                               (x.id,))
    assert s.left_out == {"breached": (b.id, y.id), "unknown": {"min_reward_risk": (u.id,)}}
    assert (s.limits["placed"], s.limits["cancelled"], s.limits["rate"]) == ((), {}, None)
    assert s.pnl == {"USD": 40.0}

    s = api.stats(con, run=r, now=NOW)
    assert s.trades == (k.id, b.id, u.id, x.id, y.id) and s.left_out == {}
    assert s.conforming_only is False
    assert (s.limits["placed"], s.limits["cancelled"], s.limits["rate"]) == (
        (u.id,), {"not filled": (u.id,)}, 0.0)
    assert s.pnl == {"USD": 50.0}


def test_stats_never_raises_when_a_live_trades_bars_cannot_be_read(con, monkeypatch):
    version(con, 'timeframes = ["15m"]\nrsi = { at_most = 30 }\n')
    live = run(con, "live")
    t = enter(con, live, at=h(2))                 # the network is off: no bars since the entry
    assert t.outcome.status == "not judged"
    s = api.stats(con, run=live, now=NOW)
    assert s.not_judged == {t.id: {"missing": t.outcome.missing, "settles": api._settles(
        t.outcome)}}
    assert t.outcome.missing[0] == h(2) and s.not_judged[t.id]["settles"].startswith(
        "report its exit")
    assert s.rests_on["live_exits"] == {"reported": 0, "judged": 0}
    s = api.stats(con, run=live, conforming_only=True, now=NOW)
    assert s.trades == () and s.left_out == {"breached": (), "unknown": {"rsi_14_15m": (t.id,)}}

    def broken(path=None):
        raise sqlite3.OperationalError("an invented locked database")
    monkeypatch.setattr(bars, "open", broken)
    for conforming in (None, True):
        s = api.stats(con, strategy="s", mode="live", conforming_only=conforming, now=NOW)
        assert s.not_judged.keys() == ({t.id} if conforming is None else set())
