"""Derive current state from the event log.

A trade's current state is computed by folding its events in id order. No state is stored;
everything derives from the append-only log.

## Payload-key contract (what each event kind carries)

- **checked**: nested structure — `plan` (a Plan dict), `results` (list of Result dicts), `ask`,
  `sections_used`, `rules_hash`. Unlike every other kind, `checked` is not flat: to reach the
  entry price you read `payload["plan"]["entry"]`, not `payload["entry"]`. Nesting is deliberate:
  the fold never reads a `checked` event's payload (the check result is advisory, not a fact about
  the trade), so flattening it would merge the plan's keys into the event's namespace and risk
  collision with event-level fields.
- **imported**: (reserved for plan 2; carries what the importer read)
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
    """A single exit fill.

    `realised_r` is this fill's share of the trade's R: the R one unit made, times the share of
    the entered size this fill closed. A trade's fills therefore sum to the trade's R, and
    exiting 4 of 10 shares at +2R realises +0.8R. None when the trade had no initial risk.

    `stopped` is whether the fill was at or through the stop in force WHEN IT FILLED -- a long
    filling at or below it, a short at or above it -- so a stop moved afterwards cannot
    rewrite what happened.
    """
    event_id: int
    at: str
    price: float
    size: int
    realised_r: float | None
    stopped: bool


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
    horizon: str | None
    setup: str | None
    thesis: str | None
    account: str
    run_id: str | None


@dataclass(frozen=True)
class TradeState:
    """The derived current state of a trade."""
    trade_id: str
    # From the trade's identity row, because deriving anything directional or per-ticker from
    # a fold needs them: no_open_trade_in_group needs the ticker, stopped_closes the side, and
    # a cash result the point value and currency.
    ticker: str
    side: str  # long | short
    account: str
    run_id: str | None
    instrument: str
    point_value: float
    currency: str
    status: str  # planned | open | closed | passed
    entry: float | None
    # Two stops, because they answer different questions. `initial_stop` is the stop in force
    # when the trade was entered, and 1R -- the size of the bet -- is measured against it for
    # the trade's whole life. `stop` is the one in force now, which is what a tracker watches.
    # Measuring R against `stop` divided by zero on a breakeven stop and inflated every trailed one.
    initial_stop: float | None
    stop: float | None
    target: float | None
    size_entered: int | None
    size_open: int | None
    size_closed: int | None
    fills: tuple[Fill, ...]
    setup: str | None
    setup_labelled_at: int | None  # event_id when labelled, for hindsight guard
    realised_r: float | None  # in R; None with no exits yet or no initial risk -- never 0
    realised_cash: float | None  # signed points x size x point value, in `currency`
    planned_r: float | None
    opened_at: int | None  # event_id when entered
    flat_at: str | None  # when an exit last took the open size to zero
    closed_at: int | None  # event_id when closed
    # Rule results the trade was taken against, by rule name, as recorded by `entered`.
    taken_against: tuple[str, ...]
    # Shapes the log should never hold (an exit larger than the open size), for integrity.py.
    violations: tuple[str, ...]


def _per_unit_r(side: str, entry: float, price: float, initial_stop: float) -> float | None:
    """R one unit makes exiting at `price`, against the initial risk. None with no risk."""
    risk = abs(entry - initial_stop)
    if risk == 0:
        return None
    return _signed(side, entry, price) / risk


def state_of(con: sqlite3.Connection, trade_id: str) -> TradeState:
    """Derive a trade's current state by folding its events in id order.

    Skips any event named by a later correction. Records shapes that should not exist in
    `violations` rather than raising, so integrity.py can report them.
    """
    trade_row = store.trade(con, trade_id)
    if not trade_row:
        raise ValueError(f"Trade {trade_id} not found")
    side = trade_row["side"]
    point_value = trade_row["point_value"]

    events = store.events(con, trade_id=trade_id)
    corrected_ids = {e["corrects"] for e in events if e["corrects"] is not None}

    status = "planned"
    entry = None
    initial_stop = None
    stop = None
    target = None
    size_entered = None
    size_open = None
    size_closed = 0
    fills: list[Fill] = []
    setup = None
    setup_labelled_at = None
    opened_at = None
    flat_at = None
    closed_at = None
    taken_against: tuple[str, ...] = ()
    violations = []

    for event in events:
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
            initial_stop = stop = payload["stop"]
            target = payload["target"]
            size_entered = size_open = payload["size"]
            setup = payload.get("setup")
            opened_at = event["id"]
            taken_against = tuple(r["name"] for r in payload.get("taken_against", ()))

        elif kind == "stop_moved":
            stop = payload["stop"]

        elif kind == "target_moved":
            target = payload["target"]

        elif kind == "exited":
            exit_price = payload["price"]
            exit_size = payload["size"]

            if size_open is not None and exit_size > size_open:
                violations.append("exit_exceeds_size")

            if size_open is not None:
                size_open -= exit_size
                size_closed += exit_size
                if size_open <= 0:
                    flat_at = event["at"]

            if entry is not None and initial_stop is not None:
                per_unit = _per_unit_r(side, entry, exit_price, initial_stop)
                share = exit_size / size_entered if size_entered else None
                fills.append(Fill(
                    event_id=event["id"],
                    at=event["at"],
                    price=exit_price,
                    size=exit_size,
                    realised_r=None if per_unit is None or share is None else per_unit * share,
                    stopped=stop is not None and _signed(side, exit_price, stop) >= 0,
                ))

        elif kind == "labelled":
            setup = payload["setup"]
            setup_labelled_at = event["id"]

        elif kind == "closed":
            status = "closed"
            closed_at = event["id"]

    # One unknown fill makes the trade's R unknown: a partial sum would read as the whole.
    if not fills or any(f.realised_r is None for f in fills):
        realised_r = None
    else:
        realised_r = sum(f.realised_r for f in fills)

    realised_cash = (
        sum(_signed(side, entry, f.price) * f.size for f in fills) * point_value
        if fills and entry is not None else None
    )

    if entry is not None and initial_stop is not None and target is not None:
        planned_r = _per_unit_r(side, entry, target, initial_stop)
    else:
        planned_r = None

    return TradeState(
        trade_id=trade_id,
        ticker=trade_row["ticker"],
        side=side,
        account=trade_row["account"],
        run_id=trade_row["run_id"],
        instrument=trade_row["instrument"],
        point_value=point_value,
        currency=trade_row["currency"],
        status=status,
        entry=entry,
        initial_stop=initial_stop,
        stop=stop,
        target=target,
        size_entered=size_entered,
        size_open=size_open,
        size_closed=size_closed if size_closed > 0 else None,
        fills=tuple(fills),
        setup=setup,
        setup_labelled_at=setup_labelled_at,
        realised_r=realised_r,
        realised_cash=realised_cash,
        planned_r=planned_r,
        opened_at=opened_at,
        flat_at=flat_at,
        closed_at=closed_at,
        taken_against=taken_against,
        violations=tuple(violations),
    )


def _moment(at: str) -> dt.datetime:
    """An event's time. One written without an offset is read as UTC, which is what we write."""
    moment = dt.datetime.fromisoformat(at)
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


def stopped_closes(
    con: sqlite3.Connection,
    *,
    account: str | None = None,
    run_id: str | None = None,
) -> dict[str, dt.datetime]:
    """Ticker -> when the most recent trade stopped out on that ticker finished.

    Derived state, so it folds the log here rather than in a consumer: it feeds
    `checks.Context.stopped_closes`, which `cooldown_after_stop` measures against.

    WHAT COUNTS AS STOPPED: a trade with a fill at or through the stop in force when that fill
    happened (`Fill.stopped`) -- a long filling at or below its stop, a short at or above it.
    Judged at fill time, so a stop moved after the exit cannot erase a stop-out. An exit at a
    trailed stop counts too, even in profit: that is the owner's call to revisit, not this
    function's.

    ONLY FINISHED TRADES COUNT. The rule is "time since the last stopped trade's close", so a
    partial fill through the stop on a trade still holding shares is a trade in progress.
    Finished means flat -- an exit took the open size to zero -- or, failing that, `closed`.

    WHICH TIME: the moment the trade went flat. Exiting and closing are separate acts, and the
    cooldown is about when the owner was stopped out, not when they did the bookkeeping. Before
    this, a stop-out nobody had closed started no cooldown at all. A trade closed while still
    holding size (which integrity reports) dates from its `closed` event instead.

    An empty dict means the log was read and holds no stop. That is a different answer from
    "not supplied", which the caller says by passing None, and the two must not be confused:
    one lets a trade through, the other withholds the figure.

    With account=None, returns stops from all accounts; with account given, only that account's
    stops. When run_id is also given, narrows to that run: a run is a bounded block of trading,
    so run 3's cooldowns count only run 3's stops.
    """
    latest: dict[str, dt.datetime] = {}
    for state in _states(con, account=account, run_id=run_id):
        if not any(f.stopped for f in state.fills):
            continue
        at = state.flat_at
        if at is None and state.status == "closed":
            times = {e["id"]: e["at"] for e in store.events(con, trade_id=state.trade_id)}
            at = times.get(state.closed_at)
        if at is None:  # still holding: a trade in progress
            continue
        moment = _moment(at)
        if state.ticker not in latest or moment > latest[state.ticker]:
            latest[state.ticker] = moment
    return latest


def realised_r_on(
    con: sqlite3.Connection,
    day: dt.date,
    *,
    account: str | None = None,
    run_id: str | None = None,
) -> float | None:
    """The R realised on one local calendar day, for `daily_loss_limit`.

    Each exit fill realises R on its share on the day it filled, so a partial exit counts on
    its own day and a trade still open contributes what it has already realised. "Day" is the
    owner's local date, the same one trade ids use.

    0.0 when the log was read and holds no fill that day: that is a fact about the log, unlike
    `sum([])` over data that was never fetched. None when any of that day's fills has no R (a
    trade entered with no initial risk) -- one unknown makes the total unknown, never smaller.
    """
    total = 0.0
    for state in _states(con, account=account, run_id=run_id):
        for f in state.fills:
            if _moment(f.at).astimezone().date() != day:
                continue
            if f.realised_r is None:
                return None
            total += f.realised_r
    return total


def _states(
    con: sqlite3.Connection,
    *,
    account: str | None = None,
    run_id: str | None = None,
) -> list[TradeState]:
    """Every trade's state, narrowed to an account and a run when they are given."""
    return [
        state for state in (state_of(con, trade_id) for trade_id in store.trade_ids(con))
        if (account is None or state.account == account)
        and (run_id is None or state.run_id == run_id)
    ]


def open_trades(
    con: sqlite3.Connection,
    *,
    account: str | None = None,
    run_id: str | None = None,
) -> list[TradeState]:
    """Return all trades with status 'open'.

    With account=None, returns open trades from all accounts; with account given, only that
    account's trades. When run_id is also given, narrows to that run: a run is a bounded block
    of trading, so run 3's max_open_trades counts only run 3's trades.
    """
    return [s for s in _states(con, account=account, run_id=run_id) if s.status == "open"]
