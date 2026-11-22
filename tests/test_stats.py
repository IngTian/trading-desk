"""stats.summarise: the buckets, figures and fill rate over rows the api built. Counts and
averages only. Every figure is invented."""
import pytest

from desk import judge
from desk.stats import Row, summarise


def o(status, r=None, cash=None, fill=100.0, why=None):
    return judge.Outcome(**{**judge._BLANK, "status": status, "r": r, "cash": cash, "fill": fill,
                            "cancelled_why": why},
                         notes=())


def row(id, outcome, entry_type="market", planned=None, currency="USD", mode="replay", tags=(),
        version=1):
    return Row(id, "s-r1", version, mode, tags, entry_type, currency, outcome, planned)


def summ(rows, **kw):
    return summarise(rows, **{"strategy": "s", "version": None, "run": None, "mode": None,
                              "tags": (), "conforming_only": False, "left_out": {}, **kw})


def test_q1_win_loss_breakeven_and_the_win_rate():
    rs = [2, 2, 1.5, 0.1, -1, -1, -1, -1, 0, 0]
    s = summ([row(f"t{i}", o("exited", r=float(r), cash=r * 50.0)) for i, r in enumerate(rs)])
    assert (len(s.wins), len(s.losses), len(s.breakevens)) == (4, 4, 2)
    assert s.wins == ("t0", "t1", "t2", "t3") and s.breakevens == ("t8", "t9")
    assert s.win_rate == 0.4 and s.total_r == pytest.approx(1.6)
    assert s.average_r == pytest.approx(0.16)
    assert (s.average_win, s.average_loss) == (pytest.approx(1.4), -1.0)
    assert s.win_rate * s.average_win + 0.4 * s.average_loss == pytest.approx(s.average_r)


def test_an_r_within_float_residue_of_zero_is_a_breakeven():
    s = summ([row("a", o("exited", r=1e-10, cash=0.0)), row("b", o("exited", r=-1e-10, cash=0.0)),
              row("c", o("exited", r=2e-9, cash=1.0))])
    assert (s.wins, s.losses, s.breakevens) == (("c",), (), ("a", "b"))
    assert s.average_loss is None


def test_q2_closed_without_r_is_its_own_bucket_but_in_pnl():
    rows = [row(f"r{i}", o("exited", r=0.375, cash=75.0)) for i in range(8)]
    rows += [row("nostop", o("exited", cash=200.0)), row("beyond", o("exited", cash=22.0))]
    s = summ(rows)
    assert len(s.closed) == 8 and s.closed_no_r == ("nostop", "beyond")
    assert s.pnl == {"USD": 822.0}
    assert s.win_rate == 1.0 and s.total_r == 3.0   # the two without R enter no R figure


def test_q3_planned_reward_to_risk_over_the_closed_trades():
    s = summ([row("t1", o("exited", r=-1.0, cash=-20.0), planned=2.0),
              row("t2", o("cancelled", fill=None, why="not filled"), "limit", planned=3.0),
              row("t3", o("open"), planned=2.5),
              row("t4", o("exited", r=1.0, cash=20.0))])
    assert s.average_planned_rr == 2.0 and s.no_planned_rr == ("t4",)


def test_q4_the_fill_rate_leaves_out_run_stopped_and_pending():
    def lim(i, out):
        return row(f"l{i}", out, "limit")
    rows = ([lim(i, o("exited", r=1.0, cash=20.0)) for i in range(5)]
            + [lim(5, o("cancelled", fill=None, why="not filled")),
               lim(6, o("cancelled", fill=None, why="not filled")),
               lim(7, o("cancelled", fill=None, why="pulled")),
               lim(8, o("cancelled", fill=None, why="pulled")),
               lim(9, o("cancelled", fill=None, why="run stopped")),
               lim(10, o("pending", fill=None)),
               row("m", o("exited", r=1.0, cash=20.0))])    # a market entry is no limit order
    s = summ(rows)
    assert s.limits["rate"] == pytest.approx(5 / 9) and len(s.limits["placed"]) == 11
    assert s.limits["filled"] == ("l0", "l1", "l2", "l3", "l4")
    assert s.limits["pending"] == ("l10",)
    assert {k: len(v) for k, v in s.limits["cancelled"].items()} == {
        "not filled": 2, "pulled": 2, "run stopped": 1}
    assert s.cancelled == s.limits["cancelled"] and s.pending == ("l10",)


def test_a_reported_cancellation_is_not_a_fill_and_a_rate_over_nothing_is_none():
    s = summ([row("c", o("cancelled", why="pulled"), "limit"),
              row("p", o("pending", fill=None), "limit")])
    assert s.limits["filled"] == () and s.limits["rate"] == 0.0
    s = summ([row("rs", o("cancelled", fill=None, why="run stopped"), "limit")])
    assert s.limits["rate"] is None


def test_nothing_under_a_figure_is_none_never_nan():
    s = summ([])
    assert (s.win_rate, s.average_r, s.total_r, s.limits["rate"], s.pnl) == (
        None, None, None, None, {})
    assert (s.average_win, s.average_loss, s.average_planned_rr) == (None, None, None)
    s = summ([row("o", o("open")), row("p", o("pending", fill=None))])
    assert (s.open, s.pending, s.closed, s.win_rate, s.pnl) == (("o",), ("p",), (), None, {})


def test_pnl_is_per_currency_and_rests_on_says_what_was_pooled():
    rows = [row("u1", o("exited", r=2.0, cash=40.0), mode="live", tags=("a",)),
            row("u2", o("exited", r=-1.0, cash=-20.0), mode="live", tags=("a",)),
            row("c1", o("exited", cash=15.5), currency="CAD", tags=("a", "b"), version=2),
            row("c2", o("open"), currency="CAD", version=2),
            row("c3", o("exited", r=1.0, cash=10.0), currency="CAD", version=2)]
    s = summ(rows)
    assert s.pnl == {"USD": 20.0, "CAD": 25.5}
    assert s.trades == ("u1", "u2", "c1", "c2", "c3") and s.open == ("c2",)
    assert s.rests_on == {
        "mode": {"live": ("u1", "u2"), "replay": ("c1", "c2", "c3")},
        "tags": [{"tags": ["a"], "trades": ("u1", "u2")}, {"tags": ["a", "b"], "trades": ("c1",)},
                 {"tags": [], "trades": ("c2", "c3")}],
        "version": {1: ("u1", "u2"), 2: ("c1", "c2", "c3")}}


def test_the_scope_and_left_out_are_stated_as_given():
    gone = {"breached": ("b",), "unknown": {"min_reward_risk": ("u",)}}
    s = summ([], version=2, mode="live", tags=("paper",), conforming_only=True, left_out=gone)
    assert (s.strategy, s.version, s.run, s.mode, s.tags, s.conforming_only, s.left_out) == (
        "s", 2, None, "live", ("paper",), True, gone)
