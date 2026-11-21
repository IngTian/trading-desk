"""A trade's decisions, folded from the event log. Nothing is stored but the decisions.

`decisions_of` reads a trade's `entered` event, its moves, its reports, its close and its void,
with every correction read in place of the event it names. What the decisions led to -- fills,
exits, R and cash -- will be derived from them and the bars by the judge (a later release),
which takes `Decisions` as its input.

## Payload-key contract (what each trade event carries)

- **entered**: `entry_type`, `entry`, `stop`, `target`, `size`, `reasons`, `screenshots`
- **order_moved**: `stop` and/or `target` (only what moved), `reason`
- **filled**: `reason`, and `price` when given (absent: the limit)
- **cancelled**: `reason`, `why` (`not filled`: the market never reached the limit, or the order
  expired unfilled; `pulled`: the author withdrew it)
- **held**: `reason`: the position was still open at `at` (a report of state `open`)
- **closed**: `reason`, and `kind` when given (`hand`, `stop` or `target`; absent: `hand`) and
  `price` when given. With neither, it is `close_trade`'s decision; with either, an exit report
- **corrected**: `fields` (the named event's fields that change, `at` among them), `reason`;
  `corrects` names an `entered`, `order_moved`, `filled`, `cancelled`, `held` or `closed` event.
  Its own `at` is when it was written, which the fold never reads.
- **voided**: `reason`, with `at` as for `corrected`. `corrects` NULL voids the trade; naming a
  `filled`, `cancelled`, `held` or `closed` event, it **withdraws** that report, which the fold
  then reads as absent. A `voided` naming any other kind of event is not read (integrity reports
  it).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from dataclasses import dataclass

from desk import store

# A continuous futures contract as TradingView writes it ("GC1!", "MNQ2!") and as Yahoo does
# ("GC=F"), optionally behind an exchange prefix ("COMEX:GC1!"). A root may start with a digit
# (6E is a currency future), so the root is any run of letters and digits, matched lazily so the
# contract number is left for the suffix.
_CONTINUOUS = re.compile(r"^(?:[A-Z0-9_]+:)?([A-Z0-9]+?)(?:\d+!|=F)$")


def canonical_ticker(typed: str) -> tuple[str, str | None]:
    """(ticker, instrument it implies): "GC1!" -> ("GC", "future"), "acme" -> ("ACME", None).

    One trade, one ticker. Typing GC once and GC1! the next time made them two tickers, so a
    stop-out on one never cooled the other down, and an id built from "GC1!" carried a "!"
    that zsh expands. And the form itself says it is a future, which is the fact that a
    forgotten --instrument flag used to lose: GC1! checked as a stock worth 1 per point
    reported 100x too little risk and passed a futures ban. A bare root ("GC") says nothing,
    so it is taken as typed.

    Here rather than in the api because every comparison of tickers needs it: the api on what
    was typed, and cooldown_after_stop's same-ticker scope on the tickers it compares.
    """
    upper = typed.strip().upper()
    continuous = _CONTINUOUS.match(upper)
    if continuous:
        return continuous.group(1), "future"
    return upper, None


def _signed(side: str, a: float, b: float) -> float:
    """b - a for a long, a - b for a short. The only place the side flips a figure."""
    return (b - a) if side == "long" else (a - b)



@dataclass(frozen=True)
class Plan:
    """The input to every check: one planned trade, as the checks read it. Nothing in the package
    builds one yet; the release that wires the checks into trade() builds it from an entry."""
    ticker: str
    side: str
    instrument: str
    point_value: float
    currency: str
    entry: float
    # Optional because the rules are what demand them: stop_required and target_required
    # exist precisely because a plan can arrive without one, and a check withholds its figure
    # rather than reading a breach into a missing input.
    stop: float | None
    target: float | None
    size: int | None
    horizon: str | None
    setup: str | None
    thesis: str | None
    account: str   # read by no check: rules belong to a strategy now, not to an account
    run_id: str | None


@dataclass(frozen=True)
class Entry:
    type: str
    price: float
    stop: float | None
    target: float | None
    size: int
    at: dt.datetime


@dataclass(frozen=True)
class Move:
    at: dt.datetime
    stop: float | None
    target: float | None
    reason: str


@dataclass(frozen=True)
class Report:
    """A state the agent reported, taken as given."""
    at: dt.datetime
    price: float | None  # a fill's price as given; None: the limit. Always None on the others
    reason: str
    why: str | None = None  # a cancellation's cause, "not filled" | "pulled"; None on the others


@dataclass(frozen=True)
class Decisions:
    entry: Entry
    moves: tuple[Move, ...]       # in `at` order
    close_at: dt.datetime | None
    close_reason: str | None
    voided: bool
    reasons: str
    screenshots: tuple[dict, ...]
    # From the trade's identity row unless a correction changed them.
    point_value: float
    currency: str
    # The standing reports, last and defaulted so a Decisions built without them still works.
    fill: Report | None = None
    cancel: Report | None = None
    held: Report | None = None        # the standing open report with the latest `at`
    held_first: Report | None = None  # the one with the earliest (the same one when one stands)
    close_kind: str | None = None     # "hand" | "stop" | "target" while closed
    close_price: float | None = None  # as given with the close; None: not given


def decisions_of(con: sqlite3.Connection, trade_id: str) -> Decisions:
    """What was decided on a trade. Each correction is read in place of the event it names,
    only its named fields changing; a correction of a correction is read at the original's
    place, and of two naming one event the later wins. A withdrawn report is read as absent."""
    return _fold(con, trade_id)[0]


_REPORTS = ("filled", "cancelled", "held", "closed")  # the kinds a withdrawal can name


def _fold(con: sqlite3.Connection, trade_id: str
          ) -> tuple[Decisions, int, tuple[int, ...], dict[str, int]]:
    """`decisions_of`, plus the ids a correction names: the `entered` event's, each move's in
    `moves` order, and the standing reports' as {"fill", "cancel", "open", "close"}, each when
    present ("open": the latest standing open report)."""
    events = store.events(con, trade_id=trade_id)
    kind = {e["id"]: e["kind"] for e in events}
    corrects = {e["id"]: e["corrects"] for e in events}
    payload = {e["id"]: json.loads(e["payload"]) for e in events}
    at = {e["id"]: e["at"] for e in events}
    for id in kind:  # id order, so a later correction is merged over an earlier one
        if kind[id] != "corrected":
            continue
        named = corrects[id]
        while kind[named] == "corrected":
            named = corrects[named]
        fields = dict(payload[id]["fields"])
        at[named] = fields.pop("at", at[named])
        payload[named].update(fields)

    withdrawn = {corrects[id] for id in kind
                 if kind[id] == "voided" and kind.get(corrects[id]) in _REPORTS}

    def when(id: int) -> dt.datetime:
        return dt.datetime.fromisoformat(at[id])

    def of(k: str) -> list[int]:
        return [id for id in kind if kind[id] == k and id not in withdrawn]

    def report(id: int | None, price=None, why=None) -> Report | None:
        return None if id is None else Report(at=when(id), price=price,
                                               reason=payload[id]["reason"], why=why)

    entered = of("entered")[0]
    moves = sorted(of("order_moved"), key=when)  # stable: a tie keeps recording order
    # The first standing one by id: the API keeps at most one, and integrity reports a second.
    filled, cancelled, closed = (next(iter(of(k)), None) for k in ("filled", "cancelled", "closed"))
    # Several open reports may stand; a tie on `at` goes to the last by id, or the first.
    latest = max(of("held"), key=lambda id: (when(id), id), default=None)
    earliest = min(of("held"), key=lambda id: (when(id), id), default=None)
    p, row = payload[entered], store.trade(con, trade_id)
    reports = {k: id for k, id in (("fill", filled), ("cancel", cancelled), ("open", latest),
                                   ("close", closed)) if id is not None}
    return Decisions(
        entry=Entry(type=p["entry_type"], price=p["entry"], stop=p["stop"], target=p["target"],
                    size=p["size"], at=when(entered)),
        moves=tuple(Move(at=when(m), stop=payload[m].get("stop"), target=payload[m].get("target"),
                         reason=payload[m]["reason"]) for m in moves),
        close_at=None if closed is None else when(closed),
        close_reason=None if closed is None else payload[closed]["reason"],
        voided=any(kind[id] == "voided" and corrects[id] is None for id in kind),
        reasons=p["reasons"], screenshots=tuple(p["screenshots"]),
        point_value=p.get("point_value", row["point_value"]),
        currency=p.get("currency", row["currency"]),
        # Read as stored, not coerced: a damaged price folds, and report_problem meets it.
        fill=report(filled, price=payload.get(filled, {}).get("price")),
        cancel=report(cancelled, why=payload.get(cancelled, {}).get("why")),
        held=report(latest), held_first=report(earliest),
        close_kind=None if closed is None else payload[closed].get("kind", "hand"),
        close_price=payload.get(closed, {}).get("price"),
    ), entered, tuple(moves), reports


def in_force(d: Decisions, at: dt.datetime) -> tuple[float | None, float | None]:
    """The stop and target in force at `at`: the entry's, each replaced by the latest move dated
    at or before `at` that names it. The one reader of moves by time."""
    stop, target = d.entry.stop, d.entry.target
    for m in d.moves:  # in `at` order, a tie in recording order, so the last read wins
        if m.at > at:
            break
        stop = stop if m.stop is None else m.stop
        target = target if m.target is None else m.target
    return stop, target


def report_problem(trade: str, d: Decisions, *, side: str, mode: str,
                   stopped: dt.datetime | None) -> str | None:
    """The first way d's reports do not fit the record, as a refusal naming the fix; None when
    they fit. The api's `_consistent` and integrity's `reports` both read it, so every report
    rule lives here. A trade's entry against its run, a move against the entry and the close,
    and a close against the entry stay the api's inline checks and integrity's decision_dates.

    `stopped` is the run's stop, when it has one. Rules in order, the numbers the design's."""
    e, f, c, h, h1 = d.entry, d.fill, d.cancel, d.held, d.held_first
    fp = e.price if f is None or f.price is None else f.price
    position = e.type == "market" or f is not None
    closed = d.close_at is not None
    close_at = d.close_at.isoformat() if closed else None
    an_exit = closed and (d.close_price is not None or d.close_kind in ("stop", "target"))
    # 1-5: the fill
    if e.type == "market" and (f or c):
        return (f"Trade {trade!r} was a market entry, filled when it was entered; only a limit "
                "order has a fill or a cancellation to report. To fix a market entry's price, "
                "correct the entry (correct_trade with fields={'entry': …}).")
    if f and c:
        return (f"Trade {trade!r}'s order cannot both fill (at {f.at.isoformat()}) and be "
                f"cancelled (at {c.at.isoformat()}); withdraw the wrong report with "
                "void_trade(trade, reason, report='fill' or 'cancel').")
    if f and f.at < e.at:
        return (f"Trade {trade!r} was entered at {e.at.isoformat()}; its fill at "
                f"{f.at.isoformat()} cannot be before that, so give one of them its real `at`.")
    if f and stopped is not None and f.at > stopped:
        return (f"Trade {trade!r} filled at {f.at.isoformat()}, after its run stopped at "
                f"{stopped.isoformat()}; a run stop cancels the orders still pending, so a fill "
                "comes at or before it: give the fill its real `at`. A run stop, once recorded, "
                "cannot be moved: stop_run, if that is the call refused, takes an `at` at or "
                "after the fill; if the order really filled after the stop, the desk reads it "
                "as cancelled at the stop.")
    if _signed(side, e.price, fp) > 0:  # a fill at or beyond the stop is the broker's fact
        return (f"Trade {trade!r}'s fill at {fp} is worse than its limit {e.price}: a limit "
                "order fills at its price or better, so give the price it filled at.")
    # 6: the vocabulary, before any rule below reads a kind
    if closed and d.close_kind not in ("hand", "stop", "target"):
        return f"Trade {trade!r}'s close has kind {d.close_kind!r}; use 'hand', 'stop' or 'target'."
    if c and c.why not in ("not filled", "pulled"):
        return (f"Trade {trade!r}'s cancellation has why {c.why!r}; use 'not filled' or "
                "'pulled'.")
    # 7-8: the cancellation
    if c and c.at <= e.at:
        return (f"Trade {trade!r} was entered at {e.at.isoformat()}; its cancellation at "
                f"{c.at.isoformat()} must come after that, so give one of them its real `at`.")
    if c and (m := next((m for m in d.moves if m.at >= c.at), None)):
        return (f"Trade {trade!r} has a move at {m.at.isoformat()}; its order's cancellation at "
                f"{c.at.isoformat()} must come after every move, since an order is moved only "
                "while it stands.")
    if c and stopped is not None and c.at > stopped:
        return (f"Trade {trade!r}'s order was cancelled at {c.at.isoformat()}, after its run "
                f"stopped at {stopped.isoformat()}, which had already cancelled it: give the "
                "cancellation its real `at` (at or before the stop), or withdraw it "
                "(void_trade(trade, reason, report='cancel')) and the run stop's cancellation "
                "stands. A run stop, once recorded, cannot be moved: stop_run, if that is the "
                "call refused, takes an `at` at or after the cancellation.")
    if c and closed:
        return (f"Trade {trade!r}'s order was cancelled at {c.at.isoformat()}, so it has no "
                f"position to close (closed at {close_at}); withdraw the wrong one with "
                "void_trade(trade, reason, report='cancel' or 'close').")
    # 9-11: the close
    if an_exit and not position:
        return (f"Trade {trade!r}'s exit at {close_at} needs a position, and its limit order has "
                "no fill reported: report the fill (report(trade, state='filled', …)) before the "
                "exit, and withdraw the exit before the fill.")
    if closed and f and d.close_at < f.at:
        return (f"Trade {trade!r} was closed at {close_at}, before its fill at "
                f"{f.at.isoformat()}; a fill comes at or before the close, so give one of them "
                "its real `at`.")
    if (mode == "replay" and closed and d.close_kind == "hand" and d.close_price is None
            and position):  # close_trade's decision, which replay has no bars to price
        pending = (f" If the close at {close_at} was recorded while the order seemed pending and "
                   "it had in fact filled first, withdraw that close (void_trade(trade, reason, "
                   "report='close')), report the fill, then report the exit with its price."
                   if e.type != "market" else "")
        return (f"Trade {trade!r} is on a replay run, which has no bars to price a hand close "
                "from; report the exit with the price you closed at instead (report(trade, "
                "state='exited', kind='hand', price=…, at=…, reason=…))." + pending)
    if d.close_kind in ("stop", "target"):
        kind = d.close_kind
        level = in_force(d, d.close_at)[0 if kind == "stop" else 1]
        if level is None:
            return (f"Trade {trade!r} has no {kind} in force at {close_at}, so it cannot exit at "
                    "one; report the exit with kind='hand' and its price.")
        if kind == "stop" and d.close_price is None and _signed(side, fp, level) >= 0:
            return (f"Trade {trade!r}'s position opened at {fp}, at or beyond its stop {level}, "
                    "so the stop fired at once, at that price or worse; give the price it "
                    "filled at (price=…).")  # the level would read as a win
        price = level if d.close_price is None else d.close_price
        if kind == "stop" and _signed(side, level, price) > 0:
            return (f"Trade {trade!r}'s stop exit at {price} is better than its stop {level}: a "
                    "stop fills at its price or worse, so give the price it filled at, or "
                    "kind='hand' if it was closed by hand.")
        if kind == "target" and _signed(side, level, price) < 0:
            return (f"Trade {trade!r}'s target exit at {price} is worse than its target "
                    f"{level}: a target fills at its price or better, so give the price it "
                    "filled at, or kind='hand'.")
    # 12: an open report; the mode is not read
    if h and c:
        return (f"Trade {trade!r}'s order was cancelled at {c.at.isoformat()}, so it has no "
                "position to report open; if it had in fact filled, withdraw the cancellation "
                "(void_trade(trade, reason, report='cancel')) and report the fill first.")
    if h and not position:  # pending by the record, as in rule 9: the bars are not read
        return (f"Trade {trade!r}'s limit order has no fill reported, so there is no position to "
                "report open: report the fill first (report(trade, state='filled', at=…, "
                "reason=…)).")
    opened = e.at if f is None else f.at
    if h and h1.at < opened:  # the earliest, so an older open report is held to it too
        return (f"Trade {trade!r}'s position opened at {opened.isoformat()}, so it cannot be "
                f"reported open at {h1.at.isoformat()}, before that; give one of them its real "
                "`at`.")
    if h and closed and h.at >= d.close_at:  # a priceless close too: the author wanted out
        return (f"Trade {trade!r} was closed at {close_at} and reported open at "
                f"{h.at.isoformat()}; every open report comes before the close, so give one of "
                "them its real `at`, or, if the trade is in fact still open, withdraw the close "
                "(void_trade(trade, reason, report='close')).")
    return None
