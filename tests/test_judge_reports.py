"""Reports and the judge: a replay trade read from its reports alone, and a live trade's
judged outcome with the reports laid over it. Every figure is invented."""
from datetime import UTC, datetime, timedelta

import pytest

from desk import judge, model

B = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)   # bar n opens at B + n hours; 1h bars


def h(n):
    return B + timedelta(hours=n)


def bar(n, o, hi, lo, c):
    return judge.Bar(h(n), o, hi, lo, c, None)


def rec(type="market", price=100.0, stop=98.0, target=104.0, moves=(), fill=None, cancel=None,
        why="pulled", held=None, close=None, kind=None, close_price=None):
    """A trade entered at h(1): fill=(n, price or None), cancel=n with its why, held=n an open
    report, close=n with its kind and price."""
    return model.Decisions(
        entry=model.Entry(type=type, price=price, stop=stop, target=target, size=10, at=h(1)),
        moves=tuple(model.Move(at=h(a), stop=s, target=t, reason="r") for a, s, t in moves),
        close_at=None if close is None else h(close),
        close_reason=None if close is None else "r",
        voided=False, reasons="r", screenshots=(), point_value=1.0, currency="USD",
        fill=None if fill is None else model.Report(at=h(fill[0]), price=fill[1], reason="r"),
        cancel=None if cancel is None else model.Report(at=h(cancel), price=None, reason="r",
                                                        why=why),
        held=None if held is None else model.Report(at=h(held), price=None, reason="r"),
        close_kind=None if close is None else (kind or "hand"), close_price=close_price)


LIMIT = dict(type="limit")
REPLAY = [  # (id, rec kwargs, replay kwargs, expected Outcome fields)
    ("a market entry with nothing reported is open", {}, {},
     dict(status="open", fill=100.0, filled_at=h(1), exit=None, reported=())),
    ("a limit with nothing reported is pending", LIMIT, {},
     dict(status="pending", fill=None, filled_at=None)),
    ("a reported fill opens it, at the limit when no price is given",
     dict(LIMIT, fill=(2, None)), {},
     dict(status="open", fill=100.0, filled_at=h(2), reported=("fill",))),
    ("a reported fill at its own price", dict(LIMIT, fill=(2, 99.5)), {},
     dict(status="open", fill=99.5, filled_at=h(2))),
    ("a reported cancellation ends the order, with its cause",
     dict(LIMIT, cancel=3, why="not filled"), {},
     dict(status="cancelled", cancelled_at=h(3), cancelled_why="not filled", fill=None,
          reported=("cancel",))),
    ("a run stop cancels a pending limit", LIMIT, dict(stopped=h(3)),
     dict(status="cancelled", cancelled_at=h(3), cancelled_why="run stopped", reported=())),
    ("a hand close cancels a pending limit: pulled", dict(LIMIT, close=3), {},
     dict(status="cancelled", cancelled_at=h(3), cancelled_why="pulled", reported=())),
    ("the earlier of a hand close and a run stop cancels it", dict(LIMIT, close=4),
     dict(stopped=h(3)), dict(status="cancelled", cancelled_at=h(3), cancelled_why="run stopped")),
    ("a run stop closes no position", dict(LIMIT, fill=(2, None)), dict(stopped=h(3)),
     dict(status="open", fill=100.0)),
    ("a stop exit with no price is at the stop", dict(close=3, kind="stop"), {},
     dict(status="exited", exit=98.0, exited_at=h(3), exit_reason="stop", r=-1.0, cash=-20.0,
          reported=("exit",))),
    ("a target exit at its reported price", dict(close=3, kind="target", close_price=104.5), {},
     dict(exit=104.5, exit_reason="target", r=2.25, cash=45.0)),
    ("a hand exit at its price", dict(close=3, close_price=101.0), {},
     dict(exit=101.0, exit_reason="hand", r=0.5, cash=10.0)),
    ("a stop exit is at the stop as last moved",
     dict(moves=((2, 99.0, None),), close=3, kind="stop"), {}, dict(exit=99.0, r=-0.5, cash=-10.0)),
    ("R and cash are measured from a reported fill",
     dict(LIMIT, fill=(2, 99.5), close=3, kind="stop"), {},
     dict(fill=99.5, filled_at=h(2), exit=98.0, r=-1.0, cash=-15.0, reported=("fill", "exit"))),
    ("no stop at entry: no R, cash still", dict(stop=None, close=3, close_price=101.0), {},
     dict(r=None, cash=10.0)),
    ("a fill beyond the stop: R withheld, said; cash from the fill",
     dict(LIMIT, fill=(2, 97.5), close=3, kind="stop", close_price=97.25), {},
     dict(status="exited", fill=97.5, exit=97.25, exit_reason="stop", r=None, cash=-2.5,
          note="no 1R")),
    ("an open report changes nothing: a position reads open until its exit is reported",
     dict(held=2), {}, dict(status="open", fill=100.0, filled_at=h(1), exit=None, reported=())),
    ("short: a stop exit with no price is at the stop above",
     dict(stop=102.0, target=96.0, close=3, kind="stop"), dict(side="short"),
     dict(exit=102.0, r=-1.0, cash=-20.0)),
]


@pytest.mark.parametrize(("d", "kw", "want"), [c[1:] for c in REPLAY],
                         ids=[c[0] for c in REPLAY])
def test_a_replay_trade_is_read_from_its_reports(d, kw, want):
    want = dict(want)
    note = want.pop("note", None)
    o = judge.replay(rec(**d), **{"side": "long", **kw})
    assert {k: getattr(o, k) for k in want} == want
    assert (o.judged_through, o.missing) == (None, None)
    assert (o.notes == ()) if note is None else any(note in n for n in o.notes), o.notes


STOPPED = [bar(1, 100, 101, 97.5, 99), bar(2, 99, 100, 98.5, 99.5), bar(3, 99.5, 101.5, 99, 101)]
FILLS = [bar(1, 101, 101.5, 99.5, 100.5), bar(2, 100.5, 104.5, 100, 104)]
QUIET = [bar(1, 101, 101.5, 99.5, 100.5), bar(2, 100.5, 101, 100, 100.5)]
REOPENED = [bar(1, 100, 101, 97.5, 99), bar(2, 99, 100, 98.5, 99.5), bar(3, 99.5, 100, 97, 97.5)]
REFILLED = [bar(1, 101, 101.5, 99.5, 100.5), bar(2, 100, 100.5, 97.5, 98),
            bar(3, 98.5, 100, 98.25, 99.5), bar(4, 100, 104.5, 99.5, 104)]
LIVE = [  # (id, rec kwargs, bars, live kwargs, effective fields, judged fields, differs)
    ("nothing reported: the effective outcome is the judged one", {}, STOPPED[:1], {},
     dict(status="exited", exit=98.0, exited_at=h(2), exit_reason="stop", reported=()),
     dict(status="exited", exit=98.0, exited_at=h(2), exit_reason="stop"), ()),
    ("a reported fill is the entry, and the judge goes on from it", dict(LIMIT, fill=(1.5, 99.75)),
     FILLS, {},
     dict(status="exited", fill=99.75, filled_at=h(1.5), exit=104.0, exited_at=h(3),
          exit_reason="target", r=4.25 / 1.75, cash=42.5, reported=("fill",)),
     dict(status="exited", fill=100.0, filled_at=h(2), exit=104.0, exited_at=h(3),
          exit_reason="target", r=2.0, cash=40.0),
     ("fill", "r", "cash")),                      # h(1.5) lies in the judged fill bar, (h(1), h(2)]
    ("a reported exit replaces an earlier judged exit, which is kept",
     dict(close=3.5, close_price=101.0), STOPPED, {},
     dict(status="exited", exit=101.0, exited_at=h(3.5), exit_reason="hand", r=0.5, cash=10.0,
          reported=("exit",), judged_through=None),
     dict(status="exited", exit=98.0, exited_at=h(2), exit_reason="stop", r=-1.0, cash=-20.0),
     ("exit", "exited_at", "exit_reason", "r", "cash")),
    ("a report made before the bars is read, and nothing is compared",
     dict(close=1.5, kind="stop", close_price=97.5), [],
     dict(covered=[], now=h(3), unavailable="Yahoo could not be reached: timed out"),
     dict(status="exited", exit=97.5, exited_at=h(1.5), exit_reason="stop", r=-1.25, cash=-25.0),
     dict(status="not judged", missing=(h(1), h(3))), ()),
    ("an exit reported inside a bar still forming: nothing is compared until it completes",
     dict(close=1.25, kind="stop", close_price=97.9), [bar(0, 100, 101, 99, 100)],
     dict(now=h(1.5)),
     dict(status="exited", exit=97.9, exited_at=h(1.25), exit_reason="stop", reported=("exit",)),
     dict(status="closing", judged_through=h(1)), ()),
    ("a fill reported before its bar completes: nothing is compared until it does",
     dict(LIMIT, fill=(2.5, 99.75)), [bar(1, 101, 101.5, 100.5, 101)], dict(now=h(2.75)),
     dict(status="open", fill=99.75, filled_at=h(2.5), reported=("fill",)),
     dict(status="pending", fill=None, judged_through=h(2)), ()),
    ("a cancellation reported before its bar completes: nothing is compared until it does",
     dict(LIMIT, cancel=2.5, why="pulled"), [bar(1, 101, 101.5, 100.5, 101)], dict(now=h(2.75)),
     dict(status="cancelled", cancelled_at=h(2.5), reported=("cancel",)),
     dict(status="closing", fill=None, judged_through=h(2)), ()),
    ("a fill the bars passed without reaching the limit is compared",
     dict(LIMIT, fill=(1.5, 99.75)), [bar(1, 101, 101.5, 100.5, 101), bar(2, 101, 102, 100.5, 101)],
     dict(now=h(3.5)),
     dict(status="open", fill=99.75, filled_at=h(1.5), reported=("fill",)),
     dict(status="pending", fill=None, judged_through=h(3)), ("status", "fill", "filled_at")),
    ("a reported cancellation replaces a judged fill and exit",
     dict(LIMIT, cancel=3, why="not filled"), QUIET, {},
     dict(status="cancelled", cancelled_at=h(3), cancelled_why="not filled", fill=None, exit=None,
          reported=("cancel",)),
     dict(status="exited", fill=100.0, filled_at=h(2), exit=100.5, exited_at=h(3),
          exit_reason="hand", r=0.25, cash=5.0),
     ("status", "fill", "filled_at", "exit", "exited_at", "exit_reason", "cancelled_at", "r",
      "cash")),
    ("a stop reported without a price, inside the judged stop's bar: nothing differs",
     dict(close=1.5, kind="stop"), STOPPED[:1], dict(now=h(3)),
     dict(status="exited", exit=98.0, exited_at=h(1.5), exit_reason="stop", r=-1.0),
     dict(status="exited", exit=98.0, exited_at=h(2), exit_reason="stop", r=-1.0),
     ()),
    ("a reported fill and exit are both the reports'; "
     "the judge reads the exit's time as a hand close",
     dict(LIMIT, fill=(1.5, 99.75), close=2.5, kind="target"), FILLS, {},
     dict(fill=99.75, filled_at=h(1.5), exit=104.0, exited_at=h(2.5), exit_reason="target",
          r=4.25 / 1.75, cash=42.5, reported=("fill", "exit")),
     dict(fill=100.0, filled_at=h(2), exit=104.0, exited_at=h(3), exit_reason="hand", r=2.0,
          cash=40.0),
     ("fill", "exit_reason", "r", "cash")),
    ("an open report sets aside a judged exit before it; the judge walks on from its bar",
     dict(held=2.5), STOPPED, {},
     dict(status="open", fill=100.0, filled_at=h(1), exit=None, judged_through=h(4), reported=()),
     dict(status="exited", exit=98.0, exited_at=h(2), exit_reason="stop"),
     ("status", "exit", "exited_at", "exit_reason", "r", "cash")),
    ("an end in the bar holding the open report is dated after it, so it stands, and says so",
     dict(held=1.5), STOPPED, {},
     dict(status="exited", exit=98.0, exited_at=h(2), exit_reason="stop", reported=(),
          note=f"report open at or after {h(2).isoformat()}"),
     dict(status="exited", exit=98.0, exited_at=h(2), exit_reason="stop"), ()),
    ("after an open report the judge can still find a later exit", dict(held=2), REOPENED, {},
     dict(status="exited", fill=100.0, filled_at=h(1), exit=98.0, exited_at=h(4),
          exit_reason="stop", r=-1.0),
     dict(status="exited", exit=98.0, exited_at=h(2), exit_reason="stop"), ("exited_at",)),
    ("an open report after a reported fill keeps the fill's price and time",
     dict(LIMIT, fill=(1.5, 99.75), held=3.5), REFILLED, {},
     dict(status="exited", fill=99.75, filled_at=h(1.5), exit=104.0, exited_at=h(5),
          exit_reason="target", r=4.25 / 1.75, cash=42.5, reported=("fill",)),
     dict(status="exited", fill=100.0, filled_at=h(2), exit=98.0, exited_at=h(3),
          exit_reason="stop"),
     ("fill", "exit", "exited_at", "exit_reason", "r", "cash")),
    ("an open report in a reported fill's own bar: the fill's rule wins, that bar unjudged",
     dict(LIMIT, fill=(1.5, 99.75), held=1.75),
     [bar(1, 101, 101.5, 97.5, 98.5), bar(2, 98.5, 100, 98.25, 99.5)], {},
     dict(status="open", fill=99.75, filled_at=h(1.5), exit=None, judged_through=h(3),
          reported=("fill",)),
     dict(status="exited", fill=100.0, filled_at=h(2), exit=98.0, exited_at=h(2),
          exit_reason="stop"),
     ("status", "fill", "exit", "exited_at", "exit_reason", "r", "cash")),
    # Ruling R8: a position opening at or beyond its stop has fired as it opened, at its price.
    ("a reported fill beyond the stop is out at once at the fill, never better",
     dict(LIMIT, fill=(1.5, 97.5)), [bar(1, 99, 99.5, 97.2, 97.4), bar(2, 98.5, 99, 97.0, 97.3)],
     {},
     dict(status="exited", fill=97.5, filled_at=h(1.5), exit=97.5, exited_at=h(2),
          exit_reason="stop", r=None, cash=0.0, reported=("fill",), note="no 1R"),
     dict(status="exited", fill=100.0, filled_at=h(2), exit=98.0, exited_at=h(2),
          exit_reason="stop", r=-1.0, cash=-20.0),
     ("fill", "exit", "r", "cash")),
    ("a reported fill at the stop is out at once at the fill",
     dict(LIMIT, fill=(1.5, 98.0)), [bar(1, 99, 99.5, 97.75, 98.5), bar(2, 98.5, 99, 98.25, 98.75)],
     {},
     dict(status="exited", fill=98.0, exit=98.0, exited_at=h(2), exit_reason="stop", r=None,
          cash=0.0, note="no 1R"),
     dict(status="exited", fill=100.0, exit=98.0, exited_at=h(2), r=-1.0), ("fill", "r", "cash")),
    ("short: a reported fill beyond the stop is out at once at the fill, never better",
     dict(LIMIT, stop=102.0, target=96.0, fill=(1.5, 102.5)),
     [bar(1, 101, 102.8, 100.5, 102.6), bar(2, 101.5, 103, 101, 102.7)], dict(side="short"),
     dict(status="exited", fill=102.5, exit=102.5, exited_at=h(2), exit_reason="stop", r=None,
          cash=0.0, note="no 1R"),
     dict(status="exited", fill=100.0, exit=102.0, exited_at=h(2), r=-1.0, cash=-20.0),
     ("fill", "exit", "r", "cash")),
    ("short: a reported fill at the stop is out at once at the fill",
     dict(LIMIT, stop=102.0, target=96.0, fill=(1.5, 102.0)),
     [bar(1, 101, 102.25, 100.5, 101.5), bar(2, 101.5, 101.75, 101, 101.25)], dict(side="short"),
     dict(status="exited", fill=102.0, exit=102.0, exited_at=h(2), exit_reason="stop", r=None,
          cash=0.0, note="no 1R"),
     dict(status="exited", fill=100.0, exit=102.0, exited_at=h(2), r=-1.0), ("fill", "r", "cash")),
    ("a reported fill beyond the stop before any bar is out at once, dated on the timeframe",
     dict(LIMIT, fill=(1.5, 97.5)), [],
     dict(covered=[], now=h(3), unavailable="Yahoo could not be reached: timed out"),
     dict(status="exited", exit=97.5, exited_at=h(2), exit_reason="stop", cash=0.0),
     dict(status="not judged", missing=(h(1), h(3))), ()),
    ("an open report after the stop trailed beyond the price: a level to walk, not fired at once",
     dict(moves=((2, 101.0, None),), target=110.0, held=3.5),
     [bar(1, 100, 102, 99.5, 101.5), bar(2, 101.5, 103, 101.25, 102.5),
      bar(3, 102.5, 103.5, 102, 103), bar(4, 103, 103.5, 100.5, 101)], {},
     dict(status="exited", exit=101.0, exited_at=h(5), exit_reason="stop", r=0.5, cash=10.0,
          reported=()),
     dict(status="exited", exit=101.0, exited_at=h(5), exit_reason="stop", r=0.5, cash=10.0), ()),
]


@pytest.mark.parametrize(("d", "bars", "kw", "eff", "jud", "diff"), [c[1:] for c in LIVE],
                         ids=[c[0] for c in LIVE])
def test_a_live_trade_is_judged_and_its_reports_laid_over(d, bars, kw, eff, jud, diff):
    eff = dict(eff)
    note = eff.pop("note", None)
    kw = {"side": "long", "minutes": 60, "now": h(48),
          "covered": [(h(0), bars[-1].open_time + timedelta(hours=1))] if bars else [], **kw}
    o, j = judge.live(rec(**d), bars=bars, **kw)
    assert {k: getattr(o, k) for k in eff} == eff
    assert {k: getattr(j, k) for k in jud} == jud
    assert judge.differs(o, j, 60) == diff and j.reported == ()
    assert note is None or any(note in n for n in o.notes), o.notes


def test_differs_has_nothing_to_compare_on_a_replay_trade():
    assert judge.differs(judge.replay(rec(), side="long"), None, 60) == ()
