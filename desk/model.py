"""Derive current state from the event log.

A trade's current state is computed by folding its events in id order. No state is stored;
everything derives from the append-only log.

## Payload-key contract (what each event kind carries)

- **entered**: `entry`, `stop`, `target`, `size`, `horizon`, `setup`, `thesis`
- **stop_moved**: `stop`
- **target_moved**: `target`
- **exited**: `price`, `size`
- **labelled**: `setup`
- **closed**: (no additional fields)
- **passed**: `setup`, `thesis`

## Withholding rather than guessing

A figure whose inputs are missing is `None`, never `0.0`. This codebase has shipped the
opposite twice: a total summed over an empty list reported zero and read as a fact.
`sum([])` is `0.0` and `0.0` is a claim we cannot make.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass

from desk import store


def _signed(side: str, a: float, b: float) -> float:
    """b - a for a long, a - b for a short. The only place the side flips a figure."""
    return (b - a) if side == "long" else (a - b)


@dataclass(frozen=True)
class Fill:
    """A single exit fill."""
    event_id: int
    price: float
    size: int
    realised_r: float


@dataclass(frozen=True)
class Plan:
    """The input to every check; built by the CLI in Task 6 and by the importers in plan 2."""
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
    horizon: str
    setup: str
    thesis: str
    account: str
    run_id: str | None


@dataclass(frozen=True)
class TradeState:
    """The derived current state of a trade."""
    trade_id: str
    # From the trade's identity row, because deriving anything directional or per-ticker from
    # a fold needs them: no_open_trade_in_group needs the ticker, stopped_closes the side.
    ticker: str
    side: str  # long | short
    status: str  # planned | open | closed | passed
    entry: float | None
    stop: float | None
    target: float | None
    size_open: int | None
    size_closed: int | None
    fills: tuple[Fill, ...]
    setup: str | None
    setup_labelled_at: int | None  # event_id when labelled, for hindsight guard
    realised_r: float | None  # None if no exits yet, otherwise sum across fills
    planned_r: float | None
    opened_at: int | None  # event_id when entered
    closed_at: int | None  # event_id when closed
    taken_against: tuple[str, ...]  # invariant violations


def state_of(con: sqlite3.Connection, trade_id: str) -> TradeState:
    """Derive a trade's current state by folding its events in id order.

    Skips any event named by a later correction. Detects invariant violations and records
    them in taken_against.
    """
    # Get the trade metadata
    trade_row = store.trade(con, trade_id)

    if not trade_row:
        raise ValueError(f"Trade {trade_id} not found")

    side = trade_row["side"]

    # Get all events for this trade
    events = store.events(con, trade_id=trade_id)

    # Build a set of corrected event IDs to skip
    corrected_ids = {e["corrects"] for e in events if e["corrects"] is not None}

    # Initial state
    status = "planned"
    entry = None
    stop = None
    target = None
    size_open = None
    size_closed = 0
    fills = []
    setup = None
    setup_labelled_at = None
    opened_at = None
    closed_at = None
    violations = []

    # Fold the events
    for event in events:
        # Skip corrected events
        if event["id"] in corrected_ids:
            continue

        kind = event["kind"]
        payload = json.loads(event["payload"])

        if kind == "passed":
            status = "passed"
            setup = payload.get("setup")

        elif kind == "entered":
            status = "open"
            entry = payload["entry"]
            stop = payload["stop"]
            target = payload["target"]
            size_open = payload["size"]
            setup = payload.get("setup")
            opened_at = event["id"]

        elif kind == "stop_moved":
            stop = payload["stop"]

        elif kind == "target_moved":
            target = payload["target"]

        elif kind == "exited":
            exit_price = payload["price"]
            exit_size = payload["size"]

            # Check for over-exit
            if size_open is not None and exit_size > size_open:
                violations.append("exit_exceeds_size")

            # Update open/closed sizes
            if size_open is not None:
                size_open -= exit_size
                size_closed += exit_size

            # Compute realised R for this fill
            if entry is not None and stop is not None:
                fill_r = _signed(side, entry, exit_price) / abs(entry - stop) * exit_size
                fills.append(Fill(
                    event_id=event["id"],
                    price=exit_price,
                    size=exit_size,
                    realised_r=fill_r,
                ))

        elif kind == "labelled":
            setup = payload["setup"]
            setup_labelled_at = event["id"]

        elif kind == "closed":
            status = "closed"
            closed_at = event["id"]

    # Compute derived fields
    realised_r = sum(f.realised_r for f in fills) if fills else None

    # Planned R: target R - risk is 1R by definition
    if entry is not None and stop is not None and target is not None:
        planned_r = _signed(side, entry, target) / abs(entry - stop)
    else:
        planned_r = None

    return TradeState(
        trade_id=trade_id,
        ticker=trade_row["ticker"],
        side=side,
        status=status,
        entry=entry,
        stop=stop,
        target=target,
        size_open=size_open,
        size_closed=size_closed if size_closed > 0 else None,
        fills=tuple(fills),
        setup=setup,
        setup_labelled_at=setup_labelled_at,
        realised_r=realised_r,
        planned_r=planned_r,
        opened_at=opened_at,
        closed_at=closed_at,
        taken_against=tuple(violations),
    )


def _moment(at: str) -> dt.datetime:
    """An event's time. One written without an offset is read as UTC, which is what we write."""
    moment = dt.datetime.fromisoformat(at)
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


def stopped_closes(con: sqlite3.Connection) -> dict[str, dt.datetime]:
    """Ticker -> when the most recent trade stopped out on that ticker closed.

    Derived state, so it folds the log here rather than in a consumer: it feeds
    `checks.Context.stopped_closes`, which `cooldown_after_stop` measures against.

    WHAT COUNTS AS STOPPED: a fill at or beyond the trade's stop on the losing side of it,
    `_signed(side, price, stop) >= 0` -- a long filling at or below its stop, a short at or
    above it. The stop is the one the fold ends with, which is the stop as it stood when the
    trade finished.

    WHICH TIME: the `closed` event's, since a cooldown runs from the trade's close; and when
    there is no `closed` event yet, the stopping fill's own time, so a stop nobody has written
    up still starts the clock instead of being invisible.

    An empty dict means the log was read and holds no stop. That is a different answer from
    "not supplied", which the caller says by passing None, and the two must not be confused:
    one lets a trade through, the other withholds the figure.
    """
    latest: dict[str, dt.datetime] = {}
    for trade_id in store.trade_ids(con):
        state = state_of(con, trade_id)
        if state.stop is None or not state.fills:
            continue
        stopped = [f for f in state.fills if _signed(state.side, f.price, state.stop) >= 0]
        if not stopped:
            continue
        times = {e["id"]: e["at"] for e in store.events(con, trade_id=trade_id)}
        at = times.get(state.closed_at) or times.get(stopped[-1].event_id)
        if at is None:
            continue
        moment = _moment(at)
        if state.ticker not in latest or moment > latest[state.ticker]:
            latest[state.ticker] = moment
    return latest


def open_trades(con: sqlite3.Connection) -> list[TradeState]:
    """Return all trades with status 'open'."""
    # Get all trade IDs
    trade_ids = store.trade_ids(con)

    # Compute state for each and filter to open ones
    return [
        state for state in (state_of(con, trade_id) for trade_id in trade_ids)
        if state.status == "open"
    ]
