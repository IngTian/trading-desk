"""The judge: what a live trade's decisions led to, worked out from completed bars.

A pure function of the decisions and the bars (design §6.2): no connection, clock, path or
network. It runs on live runs only (§14), so the wall clock `now` is always given.

The timing rule (§3): a decision is in force from the first bar opening at or after its `at`, so
the bar holding the entry's `at` is never judged, and every fill and exit is dated at its bar's
close. A pending limit fills at the limit, never better. In the bar it fills, its stop counts,
at the stop price, and its target does not. A position opening (a fill, a market entry) with its
stop then in force at or beyond its price has fired as it opened: out at once at that price, never
better, dated the close of the bar it opened in (rulings R7, R8). An open trade's stop exits at
the stop, or at the bar's open when the bar opened beyond it; its target exits at the target. A
bar reaching both exits at the stop. In the last bar before a hand close, the author's ruling
(2026-11-20): a bar finished at the close is judged in full, and in one still forming at it only
the stop counts.

Missing bars make the trade "not judged" for the range they leave out; nothing past them is
claimed.

The reports (§14), still pure: `replay` reads a replay trade from its reports alone, and `live`
lays a live trade's reports over the judge, keeping the judged outcome for `differs`.
"""
from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace

from desk import model

NO_1R = "the stop is at or beyond the fill, so there is no 1R to measure"


@dataclass(frozen=True)
class Bar:
    open_time: dt.datetime  # aware UTC; the bar closes at open_time + its timeframe
    open: float
    high: float
    low: float
    close: float
    volume: float | None


@dataclass(frozen=True)
class Outcome:
    status: str  # "pending" | "open" | "closing" | "exited" | "cancelled" | "not judged"
    fill: float | None  # the entry price once filled; a market entry fills at once, at `at`
    filled_at: dt.datetime | None
    exit: float | None
    exited_at: dt.datetime | None  # judged: the exit bar's close; reported: the report's `at`
    exit_reason: str | None  # "stop" | "target" | "hand"
    cancelled_at: dt.datetime | None
    # "not filled" | "pulled" | "run stopped" (reported); judged: "run stopped" when the run
    # stop settled the order (a tie with the close included), else "pulled" (a hand close).
    cancelled_why: str | None
    # Against the entry's stop (as corrected); None without one, or with one at or beyond the fill.
    r: float | None
    cash: float | None  # price move x size x point value, in the trade's currency
    # Where the walk ended: the close of the last bar walked, else the entry's `at`; None when
    # no bars were walked (replay, a reported end).
    judged_through: dt.datetime | None
    missing: tuple[dt.datetime, dt.datetime | None] | None  # not judged: from, to (None: onwards)
    notes: tuple[str, ...]
    timeframe: str | None = None  # left None by the judge; the api names the one it judged, 1m
    reported: tuple[str, ...] = ()  # which of "fill", "cancel", "close" come from reports


def _money(side: str, entry: float, stop: float | None, exit: float, size: int,
           point_value: float) -> tuple[float | None, float]:
    """(R, cash) of a trade filled at `entry` and out at `exit`. R is withheld without a stop, and
    for a stop at or beyond the fill, where abs() would read a stop-out as a win (as
    checks._stop_distance refuses it), and when it is not finite (a vanishing risk)."""
    move = model._signed(side, entry, exit)
    risky = stop is not None and model._signed(side, entry, stop) < 0
    r = move / abs(entry - stop) if risky else None
    return (r if r is None or math.isfinite(r) else None), move * size * point_value


def _why(close_at: dt.datetime | None, stopped: dt.datetime | None) -> str:
    """What settled a pending order: the run stop when it came first or tied, else the close."""
    return ("run stopped" if stopped is not None and (close_at is None or stopped <= close_at)
            else "pulled")


_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def _opened(d: model.Decisions, side: str, price: float, at: dt.datetime, m: dt.timedelta,
            bars: Sequence[Bar]) -> dict | None:
    """Ruling R8, the one check for every way a position opens: at `price` at `at`, with the stop
    then in force at or beyond that price, the stop fired as it opened, so it is out at once at the
    price, never better, dated the close of the bar holding `at`. That bar's close is found on the
    bars' grid (the epoch's when there are none) if the bar is missing. None otherwise."""
    stop = model.in_force(d, at)[0]
    if stop is None or model._signed(side, price, stop) < 0:
        return None
    grid = next((b.open_time for b in bars if b.open_time <= at < b.open_time + m),
                bars[0].open_time if bars else _EPOCH)
    return dict(status="exited", exit=price, exit_reason="stop", exited_at=at + m - (at - grid) % m)


def judge(d: model.Decisions, *, side: str, minutes: int, bars: Sequence[Bar],
          covered: Sequence[tuple[dt.datetime, dt.datetime]], now: dt.datetime,
          stopped: dt.datetime | None = None, unavailable: str | None = None,
          opens: bool = True) -> Outcome:
    """What `d` led to on `bars`: complete bars of one `minutes` timeframe, sorted, one per
    open_time. `covered` holds the sorted, merged [from, to) ranges the bars were fetched for, so
    a gap inside one is a closed market and a gap between two is missing bars. `stopped` is the
    run's stop; `unavailable` says why bars past the covered range could not be had. `opens` is
    False when a market entry stands for a position reported open at its `at` (judge.live), not one
    opening there: its stop is then a level to walk, not one fired as it opened (R8).

    Reads only d.entry, d.moves and d.close_at (a hand close)."""
    e, close_at = d.entry, d.close_at
    m = dt.timedelta(minutes=minutes)
    notes: list[str] = []

    def u(p: float) -> float:  # in favourable units: higher is better for the side held
        return model._signed(side, 0.0, p)

    def low(b: Bar) -> float:
        return min(u(b.low), u(b.high))

    def high(b: Bar) -> float:
        return max(u(b.low), u(b.high))

    def reaches(x: dt.datetime, until: dt.datetime) -> bool:
        """Whether the range holding x (its `to` included: x is usually a bar's close) runs to
        `until`, so no bar between them can be missing."""
        return any(f <= x <= to and to >= until for f, to in covered)

    def settle() -> dict:
        """The close or the run stop, reached with no bar left to judge before it."""
        if pending:
            return dict(status="cancelled", cancelled_at=due, cancelled_why=_why(close_at, stopped))
        if last is None:  # entered while the market was shut, closed before it reopened
            notes.append("no bar traded between the entry and the close")
            return dict(status="exited", exit=e.price, exited_at=close_at, exit_reason="hand")
        # Any walked bar here would have been the last before the close, so this is the entry
        # bar, which is never judged (Q27): out at its close.
        return dict(status="exited", exit=last.close, exited_at=last.open_time + m,
                    exit_reason="hand")

    def not_judged(start: dt.datetime, until: dt.datetime) -> dict:
        notes.append(f"no bars from {start.isoformat()} to {until.isoformat()}")
        if unavailable:
            notes.append(unavailable)
        return dict(status="not judged", missing=(start, until))

    # The last bar seen, first the entry bar: it opened before the entry, so it is never judged,
    # and it is noted when it reached what would have mattered.
    last = next((b for b in bars if b.open_time < e.at < b.open_time + m), None)
    if last is not None and (low(last) <= u(e.price) if e.type == "limit" else (
            (e.stop is not None and low(last) <= u(e.stop))
            or (e.target is not None and high(last) >= u(e.target)))):
        notes.append("entry bar skipped")
    pending = e.type == "limit"
    fill, filled_at = (None, None) if pending else (e.price, e.at)
    due = min((x for x in (close_at, stopped) if x is not None), default=None)  # settles a limit
    t = e.at
    end = None if pending or not opens else _opened(d, side, e.price, e.at, m, bars)
    at_once = end is not None  # out as it opened (R8): no bar is walked
    walked = [] if at_once else [b for b in bars if b.open_time >= e.at]
    for i, b in enumerate(walked):
        s = due if pending else close_at
        if s is not None and s <= b.open_time and reaches(t, s):
            end = settle()
            break
        if not any(f <= t and to > b.open_time for f, to in covered):
            end = not_judged(t, next((f for f, to in covered if f <= b.open_time < to),
                                     b.open_time))
            break
        last = b
        t = b.open_time + m
        nxt = walked[i + 1].open_time if i + 1 < len(walked) else None
        final = (close_at is not None and b.open_time < close_at
                 and (nxt is None or nxt >= close_at) and reaches(b.open_time, close_at))
        forming = final and t > close_at  # the author's ruling: in it only the stop counts
        stop, target = model.in_force(d, b.open_time)
        hit_stop = stop is not None and low(b) <= u(stop)
        if pending:
            if low(b) > u(e.price):
                if final:
                    end = settle()
                    break
                continue
            pending, fill, filled_at = False, e.price, t
            if hit_stop:  # price passed the limit before the stop, so never at the open
                # (a stop at or beyond the fill is always reached here, and is R8's)
                end = (_opened(d, side, e.price, b.open_time, m, bars)
                       or dict(status="exited", exit=stop, exit_reason="stop"))
            elif final:  # the target never counts in the fill bar
                end = dict(status="exited", exit=b.close, exit_reason="hand")
        elif hit_stop:
            end = dict(status="exited", exit=b.open if u(b.open) <= u(stop) else stop,
                       exit_reason="stop")
        elif forming:
            end = dict(status="exited", exit=b.close, exit_reason="hand")
        elif target is not None and high(b) >= u(target):
            end = dict(status="exited", exit=target, exit_reason="target")
        elif final:
            end = dict(status="exited", exit=b.close, exit_reason="hand")
        if end is not None:
            end["exited_at"] = t
            break

    if end is None:  # the bars ran out while the trade is pending or open
        s = due if pending else close_at
        if s is not None and reaches(t, s):
            end = settle()
        elif unavailable and t + m <= now:  # a complete bar could be missing
            end = not_judged(t, now)
        elif close_at is not None:  # a hand close past the bars kept so far (Q28)
            notes.append("closing at the close of the last bar opening before "
                         f"{close_at:%H:%M} UTC")
            end = dict(status="closing")
        else:
            end = dict(status="pending" if pending else "open")

    r = cash = None
    if end["status"] == "exited":
        r, cash = _money(side, e.price, e.stop, end["exit"], e.size, d.point_value)
        if e.stop is not None and r is None:
            notes.append(NO_1R)
        # when the exit was read: the exit bar's open, or the entry's `at` when out at once
        out_at = end["exited_at"]
        opened = e.at if at_once else out_at - m
        notes += [f"the move at {mv.at.isoformat()} had no effect: the trade exited at "
                  f"{out_at.isoformat()}" for mv in d.moves if mv.at > opened]
        if close_at is not None and close_at > opened and end["exit_reason"] != "hand":
            notes.append(f"the close at {close_at.isoformat()} had no effect: the trade exited "
                         f"at {out_at.isoformat()}")
    return Outcome(**{**dict.fromkeys(("exit", "exited_at", "exit_reason", "cancelled_at",
                                       "cancelled_why", "missing")), **end},
                   fill=fill, filled_at=filled_at, r=r, cash=cash, judged_through=t,
                   notes=tuple(notes))


_COMPARED = ("status", "fill", "filled_at", "exit", "exited_at", "exit_reason", "cancelled_at",
             "r", "cash")
_BLANK = dict.fromkeys(_COMPARED + ("cancelled_why", "judged_through", "missing"))


def _position(d: model.Decisions) -> tuple[float, dt.datetime]:
    """The position's price and `at`: a standing reported fill's (its price, else the limit), else
    the entry's. The one home of that default (F7)."""
    f = d.fill
    if f is None:
        return d.entry.price, d.entry.at
    return (d.entry.price if f.price is None else f.price), f.at


def _ends(d: model.Decisions, base: Outcome, *, side: str) -> Outcome:
    """`base` with d's reports laid over it: a reported cancellation or exit replaces it whole, and
    a reported fill only marks it (the caller has already set the fill)."""
    e, c = d.entry, d.cancel
    if c is not None:
        return Outcome(**{**_BLANK, "status": "cancelled", "cancelled_at": c.at,
                          "cancelled_why": c.why}, notes=(), reported=("cancel",))
    if d.close_at is not None and (d.close_price is not None or d.close_kind in ("stop", "target")):
        fill, filled_at = _position(d)
        exit = d.close_price
        if exit is None:
            stop, target = model.in_force(d, d.close_at)
            exit = stop if d.close_kind == "stop" else target
        r, cash = _money(side, fill, e.stop, exit, e.size, d.point_value)
        return Outcome(**{**_BLANK, "status": "exited", "fill": fill, "filled_at": filled_at,
                          "exit": exit, "exited_at": d.close_at, "exit_reason": d.close_kind,
                          "r": r, "cash": cash},
                       notes=(NO_1R,) if e.stop is not None and r is None else (),
                       reported=("fill", "close") if d.fill else ("close",))
    return replace(base, reported=("fill",)) if d.fill else base


def replay(d: model.Decisions, *, side: str, stopped: dt.datetime | None = None) -> Outcome:
    """A replay trade's outcome from its record alone, with no bars (§14). A position reads open
    until its exit is reported (an open report changes nothing); a pending limit is cancelled at
    the earlier of a hand close and the run stop; a run stop closes no position."""
    if d.entry.type == "market" or d.fill is not None:
        fill, filled_at = _position(d)
        base = dict(status="open", fill=fill, filled_at=filled_at)
    elif d.close_at is not None or stopped is not None:
        due = min(x for x in (d.close_at, stopped) if x is not None)
        base = dict(status="cancelled", cancelled_at=due, cancelled_why=_why(d.close_at, stopped))
    else:
        base = dict(status="pending")
    return _ends(d, Outcome(**{**_BLANK, **base}, notes=()), side=side)


def _decided(d: model.Decisions) -> model.Decisions:
    """What the bars are read against: d with the trade's end (a close of any kind, or a reported
    cancellation) as a hand close at its `at`. The judge reads only the entry, the moves and
    close_at, so no reported price, kind, fill or open report reaches it."""
    return replace(d, close_at=d.cancel.at if d.cancel else d.close_at)


def live(d: model.Decisions, *, side: str, minutes: int, bars: Sequence[Bar],
         covered: Sequence[tuple[dt.datetime, dt.datetime]], now: dt.datetime,
         stopped: dt.datetime | None = None, unavailable: str | None = None
         ) -> tuple[Outcome, Outcome]:
    """(effective, judged): the judge on `bars`, and the outcome with d's reports laid over it.
    The judge continues from the latest report: from a reported fill as a market entry there (its
    own bar unjudged, Q27, ruling R4; out at once at or beyond its stop, R8), and from an open
    report at h at the open of the bar holding h, every bar before it set aside, ends and holes
    alike."""
    kw = dict(side=side, minutes=minutes, bars=bars, covered=covered, now=now, stopped=stopped,
              unavailable=unavailable)
    decided = _decided(d)
    judged = base = judge(decided, **kw)
    if d.fill is not None or d.held is not None:
        price, at = _position(d)
        m = dt.timedelta(minutes=minutes)
        t0 = at
        if d.held is not None:
            h = d.held.at
            t0 = max(at, next((b.open_time for b in bars if b.open_time <= h < b.open_time + m),
                              h))
        base = judge(replace(decided, entry=replace(d.entry, type="market", price=price, at=t0)),
                     opens=t0 == at, **kw)  # from a later open report nothing opens: it walks on
        base = replace(base, fill=price, filled_at=at)
        if (d.held is not None and base.exit_reason in ("stop", "target")
                and base.exited_at - m <= h < base.exited_at):
            out = base.exited_at.isoformat()
            base = replace(base, notes=base.notes + (
                f"the open report at {h.isoformat()} is inside the bar that ended the trade at "
                f"{out}; a bar cannot say which came first, so the exit stands: report open at "
                f"or after {out} to set it aside",))
    return _ends(d, base, side=side), judged


def differs(o: Outcome, judged: Outcome | None, minutes: int) -> tuple[str, ...]:
    """The fields among _COMPARED whose values differ between `o` and `judged`, in that order; ()
    when there is nothing to compare: no judged outcome, or one whose bars have not yet reached
    the report (a close not reached, a fill reported after the last bar walked), which the next
    read compares. A time given in both is the same when `o`'s lies in the judged one's bar: the
    bars date an end at the bar's close, a report at its instant. cancelled_why is not compared:
    the cause is the record's, never the bars'."""
    if judged is None or judged.status in ("not judged", "closing") or (
            judged.status == "pending" and o.filled_at is not None
            and judged.judged_through < o.filled_at):
        return ()
    m = dt.timedelta(minutes=minutes)

    def same(k: str) -> bool:
        a, b = getattr(o, k), getattr(judged, k)
        if k.endswith("_at") and a is not None and b is not None:
            return b - m < a <= b
        return a == b

    return tuple(k for k in _COMPARED if not same(k))
