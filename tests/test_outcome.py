"""A trade read from its record alone. Every figure is invented."""
from datetime import UTC, datetime, timedelta

import pytest

from desk import model, outcome

B = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)   # decision n is at B + n hours


def h(n):
    return B + timedelta(hours=n)


def rec(type="market", price=100.0, stop=98.0, target=104.0, moves=(), fill=None, cancel=None,
        why="pulled", open=None, exited=None, kind=None, exit_price=None):
    """A trade entered at h(1): fill=(n, price or None), cancel=n with its why, open=n an open
    report, exited=n with its kind and price."""
    return model.Decisions(
        entry=model.Entry(type=type, price=price, stop=stop, target=target, size=10, at=h(1)),
        moves=tuple(model.Move(at=h(a), stop=s, target=g, reason="r") for a, s, g in moves),
        voided=False, reasons="r", screenshots=(), point_value=1.0, currency="USD",
        filled=None if fill is None else model.Report(at=h(fill[0]), price=fill[1], reason="r"),
        cancelled=None if cancel is None else model.Report(at=h(cancel), price=None, reason="r",
                                                           why=why),
        open=None if open is None else model.Report(at=h(open), price=None, reason="r"),
        exited=None if exited is None else model.Report(at=h(exited), price=exit_price, reason="r",
                                                        kind=kind or "hand"))


LIMIT = dict(type="limit")
CASES = [  # (id, rec kwargs, of() kwargs, expected Outcome fields)
    ("a market entry with nothing reported is open", {}, {},
     dict(status="open", fill=100.0, filled_at=h(1), exit=None)),
    ("a limit with nothing reported is pending", LIMIT, {},
     dict(status="pending", fill=None, filled_at=None)),
    ("a reported fill opens it, at the limit when no price is given",
     dict(LIMIT, fill=(2, None)), {},
     dict(status="open", fill=100.0, filled_at=h(2))),
    ("a reported fill at its own price", dict(LIMIT, fill=(2, 99.5)), {},
     dict(status="open", fill=99.5, filled_at=h(2))),
    ("a reported cancellation ends the order, with its cause",
     dict(LIMIT, cancel=3, why="not filled"), {},
     dict(status="cancelled", cancelled_at=h(3), cancelled_why="not filled", fill=None)),
    ("a run stop cancels a pending limit", LIMIT, dict(stopped=h(3)),
     dict(status="cancelled", cancelled_at=h(3), cancelled_why="run stopped")),
    ("a run stop closes no position", dict(LIMIT, fill=(2, None)), dict(stopped=h(3)),
     dict(status="open", fill=100.0)),
    ("a stop exit with no price is at the stop", dict(exited=3, kind="stop"), {},
     dict(status="exited", exit=98.0, exited_at=h(3), exit_kind="stop", r=-1.0, cash=-20.0)),
    ("a target exit at its reported price", dict(exited=3, kind="target", exit_price=104.5), {},
     dict(exit=104.5, exit_kind="target", r=2.25, cash=45.0)),
    ("a hand exit at its price", dict(exited=3, exit_price=101.0), {},
     dict(exit=101.0, exit_kind="hand", r=0.5, cash=10.0)),
    ("a stop exit is at the stop as last moved",
     dict(moves=((2, 99.0, None),), exited=3, kind="stop"), {},
     dict(exit=99.0, r=-0.5, cash=-10.0)),
    ("R and cash are measured from a reported fill",
     dict(LIMIT, fill=(2, 99.5), exited=3, kind="stop"), {},
     dict(fill=99.5, filled_at=h(2), exit=98.0, r=-1.0, cash=-15.0)),
    ("no stop at entry: no R, cash still", dict(stop=None, exited=3, exit_price=101.0), {},
     dict(r=None, cash=10.0)),
    ("a fill beyond the stop: R withheld, said; cash from the fill",
     dict(LIMIT, fill=(2, 97.5), exited=3, kind="stop", exit_price=97.25), {},
     dict(status="exited", fill=97.5, exit=97.25, exit_kind="stop", r=None, cash=-2.5,
          note="no 1R")),
    ("an open report changes nothing: a position reads open until its exit is reported",
     dict(open=2), {}, dict(status="open", fill=100.0, filled_at=h(1), exit=None)),
    ("a reported cancellation replaces an exit", dict(LIMIT, cancel=3, exited=4), {},
     dict(status="cancelled", cancelled_at=h(3), exit=None)),
    ("short: a stop exit with no price is at the stop above",
     dict(stop=102.0, target=96.0, exited=3, kind="stop"), dict(side="short"),
     dict(exit=102.0, r=-1.0, cash=-20.0)),
]


@pytest.mark.parametrize(("d", "kw", "want"), [c[1:] for c in CASES],
                         ids=[c[0] for c in CASES])
def test_a_trade_is_read_from_its_record(d, kw, want):
    want = dict(want)
    note = want.pop("note", None)
    o = outcome.of(rec(**d), **{"side": "long", **kw})
    assert {k: getattr(o, k) for k in want} == want
    assert (o.notes == ()) if note is None else any(note in n for n in o.notes), o.notes
