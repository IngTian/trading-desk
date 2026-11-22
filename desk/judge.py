"""What a trade's decisions led to, read from its record alone.

A pure function of the decisions: no connection, clock, path or network. A trade's fill, exit and
cancellation come only from the reports in its record, so `replay` reads every run the same way.

A position reads open until its exit is reported (an open report changes nothing); a pending
limit reads cancelled at its run's stop once the run has one, and pending before that. An exit
always belongs to a position: the rules in `model.report_problem` refuse one without a fill.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from desk import model

NO_1R = "the stop is at or beyond the fill, so there is no 1R to measure"


@dataclass(frozen=True)
class Outcome:
    status: str  # "pending" | "open" | "exited" | "cancelled"
    fill: float | None  # the entry price once filled; a market entry fills at once, at `at`
    filled_at: dt.datetime | None
    exit: float | None
    exited_at: dt.datetime | None  # the exit report's `at`
    exit_reason: str | None  # "stop" | "target" | "hand"
    cancelled_at: dt.datetime | None
    cancelled_why: str | None  # "not filled" | "pulled" | "run stopped"
    # Against the entry's stop (as corrected); None without one, or with one at or beyond the fill.
    r: float | None
    cash: float | None  # price move x size x point value, in the trade's currency
    notes: tuple[str, ...]


_BLANK = dict.fromkeys(("fill", "filled_at", "exit", "exited_at", "exit_reason", "cancelled_at",
                        "cancelled_why", "r", "cash"))


def _money(side: str, entry: float, stop: float | None, exit: float, size: int,
           point_value: float) -> tuple[float | None, float]:
    """(R, cash) of a trade filled at `entry` and out at `exit`. R is withheld without a stop, and
    for a stop at or beyond the fill, where abs() would read a stop-out as a win (as
    checks._stop_distance refuses it), and when it is not finite (a vanishing risk)."""
    move = model._signed(side, entry, exit)
    risky = stop is not None and model._signed(side, entry, stop) < 0
    r = move / abs(entry - stop) if risky else None
    return (r if r is None or math.isfinite(r) else None), move * size * point_value


def _position(d: model.Decisions) -> tuple[float, dt.datetime]:
    """The position's price and `at`: a standing reported fill's (its price, else the limit), else
    the entry's."""
    f = d.fill
    if f is None:
        return d.entry.price, d.entry.at
    return (d.entry.price if f.price is None else f.price), f.at


def _ends(d: model.Decisions, base: Outcome, *, side: str) -> Outcome:
    """`base` with d's reported ends laid over it: a cancellation or an exit replaces it whole."""
    e, c = d.entry, d.cancel
    if c is not None:
        return Outcome(**{**_BLANK, "status": "cancelled", "cancelled_at": c.at,
                          "cancelled_why": c.why}, notes=())
    if d.close_at is not None:
        fill, filled_at = _position(d)
        exit = d.close_price
        if exit is None:
            stop, target = model.in_force(d, d.close_at)
            exit = stop if d.close_kind == "stop" else target
        r, cash = _money(side, fill, e.stop, exit, e.size, d.point_value)
        return Outcome(**{**_BLANK, "status": "exited", "fill": fill, "filled_at": filled_at,
                          "exit": exit, "exited_at": d.close_at, "exit_reason": d.close_kind,
                          "r": r, "cash": cash},
                       notes=(NO_1R,) if e.stop is not None and r is None else ())
    return base


def replay(d: model.Decisions, *, side: str, stopped: dt.datetime | None = None) -> Outcome:
    """A trade's outcome from its record alone. A market entry, or a limit with a reported fill,
    reads open; a limit whose run has stopped reads cancelled at the stop."""
    if d.entry.type == "market" or d.fill is not None:
        fill, filled_at = _position(d)
        base = dict(status="open", fill=fill, filled_at=filled_at)
    elif stopped is not None:
        base = dict(status="cancelled", cancelled_at=stopped, cancelled_why="run stopped")
    else:
        base = dict(status="pending")
    return _ends(d, Outcome(**{**_BLANK, **base}, notes=()), side=side)
