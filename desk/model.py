"""A trade's decisions, folded from the event log. Nothing is stored but the decisions.

`decisions_of` reads a trade's `entered` event, its moves, its hand close and its void, with
every correction read in place of the event it names. What the decisions led to -- fills,
exits, R and cash -- will be derived from them and the bars by the judge (a later release),
which takes `Decisions` as its input.

## Payload-key contract (what each trade event carries)

- **entered**: `entry_type`, `entry`, `stop`, `target`, `size`, `reasons`, `screenshots`
- **order_moved**: `stop` and/or `target` (only what moved), `reason`
- **closed**: `reason`
- **corrected**: `fields` (the named event's fields that change, `at` among them), `reason`;
  `corrects` names the event. Its own `at` is when it was written, which the fold never reads.
- **voided**: `reason`, with `at` as for `corrected`
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


def decisions_of(con: sqlite3.Connection, trade_id: str) -> Decisions:
    """What was decided on a trade. Each correction is read in place of the event it names,
    only its named fields changing; a correction of a correction is read at the original's
    place, and of two naming one event the later wins."""
    return _fold(con, trade_id)[0]


def _fold(con: sqlite3.Connection, trade_id: str) -> tuple[Decisions, int, tuple[int, ...]]:
    """`decisions_of`, plus the ids a correction names: the `entered` event's, and each move's
    in `moves` order."""
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

    def when(id: int) -> dt.datetime:
        return dt.datetime.fromisoformat(at[id])

    def of(k: str) -> list[int]:
        return [id for id in kind if kind[id] == k]

    entered = of("entered")[0]
    moves = sorted(of("order_moved"), key=when)  # stable: a tie keeps recording order
    closed = next(iter(of("closed")), None)
    p, row = payload[entered], store.trade(con, trade_id)
    return Decisions(
        entry=Entry(type=p["entry_type"], price=p["entry"], stop=p["stop"], target=p["target"],
                    size=p["size"], at=when(entered)),
        moves=tuple(Move(at=when(m), stop=payload[m].get("stop"), target=payload[m].get("target"),
                         reason=payload[m]["reason"]) for m in moves),
        close_at=None if closed is None else when(closed),
        close_reason=None if closed is None else payload[closed]["reason"],
        voided=bool(of("voided")), reasons=p["reasons"], screenshots=tuple(p["screenshots"]),
        point_value=p.get("point_value", row["point_value"]),
        currency=p.get("currency", row["currency"]),
    ), entered, tuple(moves)
