"""The desk's one outward face: what the MCP server (a later release) wraps, function for function.

Every function takes an open connection and `now` (default: the wall clock, in UTC) so tests
control time. Every refusal is a `Refused` whose message says what to change.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from desk import config, model, store, strategy
from desk.checks import _hours
from desk.errors import Refused

__all__ = ["Refused", "StrategyView", "save_strategy", "get_strategy", "list_strategies",
           "RunView", "start_run", "stop_run", "list_runs", "TradeView", "trade", "move_order",
           "close_trade", "correct_trade", "void_trade", "get_trade", "list_trades"]

_SLUG = re.compile(r"[a-z0-9-]{1,64}")  # with fullmatch; the schema's CHECK on strategies.id
_BARS = re.compile(r"(\d+) bars after exit")
_AFTER = " after exit"


def _aware(when: datetime | None, name: str = "now") -> datetime:
    """`when` (default: the wall clock) as an aware datetime; a naive one is refused, since
    its zone would be guessed."""
    if when is None:
        return datetime.now(UTC)
    if not isinstance(when, datetime) or when.tzinfo is None:
        raise Refused(f"`{name}` must be a time with a timezone, e.g. "
                      f"datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc); got {when!r}.")
    return when


def _at(now: datetime | None) -> str:
    return _aware(now).astimezone(UTC).isoformat()


# The MCP server will pass the agent's JSON straight in, so a parameter can arrive as any JSON type.
def _text(name: str, value) -> str:
    if not isinstance(value, str):
        raise Refused(f"{name} must be text, a string; got {value!r}.")
    return value


def _strings(name: str, value) -> tuple[str, ...]:
    """A list of strings; None is none. A bare string is refused, not read letter by letter."""
    if value is None:
        return ()
    if not isinstance(value, list | tuple) or not all(isinstance(v, str) for v in value):
        raise Refused(f'{name} must be a list of strings, e.g. ["a", "b"], or left out; '
                      f"got {value!r}.")
    return tuple(value)


@dataclass(frozen=True)
class StrategyView:
    id: str
    name: str
    version: int
    versions: int
    text: str
    rules: dict[str, str]
    timeframes: tuple[str, ...]
    tracker_script: str | None
    tracker_limit: str | None
    current_hash: str   # of the latest version and tracker: pass it back as `if_current`
    saved: str          # "version 3 created" | "tracker updated" | "unchanged"


def _current_hash(latest: sqlite3.Row, tracker: sqlite3.Row | None) -> str:
    parts = [latest["text_hash"], (tracker and tracker["script_hash"]) or "",
             (tracker and tracker["tracker_limit"]) or ""]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _view(con, id: str, version: int | None, saved: str) -> StrategyView:
    rows = store.versions(con, id)
    if not rows:
        raise Refused(f"There is no strategy {id!r}; save it first, or list_strategies to see "
                      "the ones that exist.")
    row = rows[-1] if version is None else next((r for r in rows if r["version"] == version), None)
    if row is None:
        raise Refused(f"Strategy {id!r} has no version {version}; it has versions 1 to "
                      f"{len(rows)}.")
    t = store.latest_tracker(con, id)
    text = row["text"]
    return StrategyView(
        id=id, name=strategy.name_of(text), version=row["version"], versions=len(rows),
        text=text, rules=json.loads(row["rules"]), timeframes=tuple(json.loads(row["timeframes"])),
        tracker_script=t and t["script"], tracker_limit=t and t["tracker_limit"],
        current_hash=_current_hash(rows[-1], t), saved=saved)


def _check_limit(limit: str) -> None:
    n = _BARS.fullmatch(limit)
    hours = _hours(limit.removesuffix(_AFTER)) if limit.endswith(_AFTER) else None
    if (n and int(n[1]) > 0) or (hours and hours > 0):
        return
    raise Refused(f"tracker_limit {limit!r} is not readable; write how long the tracker may keep "
                  "watching after the trade closes, e.g. '40 bars after exit' or '90m after exit'.")


def save_strategy(con: sqlite3.Connection, id: str, markdown: str, *,
                  tracker_script: str | None = None, tracker_limit: str | None = None,
                  if_current: str | None = None, now: datetime | None = None) -> StrategyView:
    """Save a strategy (new id: version 1; existing id: needs `if_current`, the `current_hash`
    last read). Text that differs after whitespace is normalised makes a new version; a changed
    tracker script or limit makes a new tracker row and no new version. A tracker argument left
    out keeps what is saved."""
    if not _SLUG.fullmatch(_text("id", id)):
        raise Refused(f"Strategy id {id!r} must be 1 to 64 lowercase letters, digits and "
                      "hyphens, like 'dip-buyer'.")
    parsed = strategy.parse(_text("markdown", markdown))
    if tracker_script is not None:
        _text("tracker_script", tracker_script)
    if tracker_limit is not None:
        _check_limit(_text("tracker_limit", tracker_limit))
    at = _at(now)
    with store.transaction(con):  # read, compare and write under one write lock
        rows = store.versions(con, id)
        old = store.latest_tracker(con, id) if rows else None
        if rows:
            if if_current is None:
                raise Refused(f"Strategy {id!r} already exists; pass if_current (the current_hash "
                              "from get_strategy) so a save cannot overwrite someone else's "
                              "change.")
            if if_current != _current_hash(rows[-1], old):
                raise Refused(f"Strategy {id!r} was changed since you read it; get it again, "
                              "redo your edit on that, and save.")
        elif if_current is not None:
            raise Refused(f"Strategy {id!r} is new, so if_current must be left out.")

        script = tracker_script if tracker_script is not None else (old and old["script"])
        limit = tracker_limit if tracker_limit is not None else (old and old["tracker_limit"])
        if limit and not script:
            raise Refused("tracker_limit needs a tracker_script: give the script, or drop the "
                          "limit.")
        script_hash = hashlib.sha256(script.encode()).hexdigest() if script else None
        new_text = not rows or rows[-1]["text_hash"] != parsed.text_hash
        new_tracker = (script_hash, limit or None) != ((old and old["script_hash"]) or None,
                                                       (old and old["tracker_limit"]) or None)
        if not rows:
            store.add_strategy(con, id, at)
        if new_text:
            store.add_version(con, id, len(rows) + 1, parsed.text, parsed.text_hash,
                              dict(parsed.rules), list(parsed.timeframes), at)
        if new_tracker:
            store.add_tracker(con, id, script, script_hash, limit or None, at)
    saved = (f"version {len(rows) + 1} created" if new_text
             else "tracker updated" if new_tracker else "unchanged")
    return _view(con, id, None, saved)


def get_strategy(con: sqlite3.Connection, id: str, version: int | None = None) -> StrategyView:
    return _view(con, id, version, "unchanged")


def list_strategies(con: sqlite3.Connection) -> list[StrategyView]:
    return [_view(con, r["id"], None, "unchanged")
            for r in con.execute("SELECT id FROM strategies ORDER BY id")]


@dataclass(frozen=True)
class RunView:
    id: str
    strategy: str
    version: int
    mode: str
    tags: tuple[str, ...]
    started: datetime
    stopped: datetime | None


def _run_view(con, row: sqlite3.Row) -> RunView:
    when = {e["kind"]: datetime.fromisoformat(e["at"])
            for e in store.events(con, run_id=row["id"]) if e["kind"].startswith("run_")}
    return RunView(id=row["id"], strategy=row["strategy_id"], version=row["version"],
                   mode=row["mode"], tags=tuple(json.loads(row["tags"])),
                   started=when["run_started"], stopped=when.get("run_stopped"))


def _decision_time(mode: str, at: datetime | None, now: datetime | None, what: str) -> datetime:
    """A live decision defaults to `now`; a replay one never defaults, or it would be dated
    after every bar in the history being replayed."""
    _aware(now)
    if at is not None:
        return _aware(at, "at")
    if mode == "replay":
        raise Refused(f"A replay run needs `at` to {what}: give the moment in the replayed "
                      "history, not the wall clock.")
    return _aware(now)


def start_run(con: sqlite3.Connection, strategy: str, *, mode: str, tags=(),
              version: int | None = None, at: datetime | None = None,
              now: datetime | None = None) -> RunView:
    """Deploy a strategy version as run `<strategy>-r<N>`. Tags are kept verbatim (deduplicated,
    order kept) and never interpreted."""
    tags = _strings("tags", tags)
    if mode not in ("live", "replay"):
        raise Refused(f"mode {mode!r} is not known; use 'live' (real time) or 'replay' "
                      "(re-running past history).")
    start = _decision_time(mode, at, now, "start")
    with store.transaction(con):  # the run number and version are read under the write lock
        rows = store.versions(con, strategy)
        if not rows:
            raise Refused(f"There is no strategy {strategy!r}; save it first, or list_strategies "
                          "to see the ones that exist.")
        v = rows[-1]["version"] if version is None else version
        if v not in {r["version"] for r in rows}:
            raise Refused(f"Strategy {strategy!r} has no version {v}; it has versions 1 to "
                          f"{len(rows)}.")
        id = f"{strategy}-r{len(store.runs(con, strategy)) + 1}"
        store.add_run(con, id, strategy, v, mode, list(dict.fromkeys(tags)))
        store.append(con, kind="run_started", run_id=id, at=_at(start), payload={},
                     recorded_at=_at(now))
    return _run_view(con, store.run(con, id))


def stop_run(con: sqlite3.Connection, run: str, *, at: datetime | None = None,
             now: datetime | None = None) -> RunView:
    """Stop the run at `at`: strictly after its start and after every non-void trade's entry,
    whichever was logged first, since trade() refuses an entry at or after the stop."""
    row = store.run(con, run)
    if row is None:
        raise Refused(f"There is no run {run!r}; list_runs to see the ones that exist.")
    stop = _decision_time(row["mode"], at, now, "stop")
    with store.transaction(con):  # the run's state is checked under the write lock
        view = _run_view(con, row)
        if view.stopped is not None:
            raise Refused(f"Run {run!r} is already stopped; start a new run instead.")
        if stop < view.started:
            raise Refused(f"Run {run!r} started at {view.started.isoformat()}; its stop cannot "
                          "be before that, so give a later `at`.")
        entries = [(d.entry.at, t["id"]) for t in store.trades(con, run)
                   if not (d := model.decisions_of(con, t["id"])).voided]
        if entries and (last := max(entries))[0] >= stop:
            raise Refused(f"Trade {last[1]!r} on run {run!r} was entered at "
                          f"{last[0].isoformat()}; the run cannot stop at or before a trade on "
                          "it, so give a later `at` (or void that trade, if it does not belong).")
        store.append(con, kind="run_stopped", run_id=run, at=_at(stop), payload={},
                     recorded_at=_at(now))
    return _run_view(con, row)


def list_runs(con: sqlite3.Connection, strategy: str | None = None) -> list[RunView]:
    return [_run_view(con, r) for r in store.runs(con, strategy)]


@dataclass(frozen=True)
class TradeView:
    id: str
    run: str
    strategy: str
    version: int
    ticker: str
    side: str
    instrument: str
    point_value: float
    currency: str
    decisions: model.Decisions


def _trade_view(con, row: sqlite3.Row) -> TradeView:
    run = store.run(con, row["run_id"])
    d = model.decisions_of(con, row["id"])
    return TradeView(id=row["id"], run=row["run_id"], strategy=run["strategy_id"],
                     version=run["version"], ticker=row["ticker"], side=row["side"],
                     instrument=row["instrument"], point_value=d.point_value,
                     currency=d.currency, decisions=d)


def _within_run(view: RunView, when: datetime) -> None:
    """A trade's entry falls inside its run: not before the start, and before any stop."""
    if when < view.started:
        raise Refused(f"Run {view.id!r} started at {view.started.isoformat()}; a trade cannot "
                      "be entered before that, so give a later `at`.")
    if view.stopped is not None and when >= view.stopped:
        raise Refused(f"Run {view.id!r} stopped at {view.stopped.isoformat()}; a trade cannot be "
                      "entered at or after that, so give an earlier `at`.")


def _generate_trade_id(ticker: str, local_date: str, existing_ids) -> str:
    """ticker-YYYYMMDD on the trader's local date; "-2", "-3"... on collision. Only [a-z0-9]
    survive from the ticker so an id types unquoted in any shell."""
    base = f"{re.sub(r'[^a-z0-9]', '', ticker.lower())}-{local_date}"
    ids = set(existing_ids)
    n = 1
    while (id := base if n == 1 else f"{base}-{n}") in ids:
        n += 1
    return id


def _price(name: str, v, *, required: bool = True) -> None:
    if v is None and not required:
        return
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) or v <= 0:
        raise Refused(f"{name} must be a number above zero, e.g. 101.5; got {v!r}.")


def _size(v) -> None:
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise Refused(f"size must be a whole number above zero, e.g. 10; got {v!r}.")


def _currency(v) -> None:
    if v not in config.CURRENCIES:
        raise Refused(f"currency {v!r} is not known; use one of {', '.join(config.CURRENCIES)}.")


def _screenshot(text: str) -> tuple[dict, bytes | None, str | None]:
    """(payload entry, file bytes, stored name) for one screenshot, or Refused. Writes nothing."""
    if text.startswith("https://"):
        return {"link": text}, None, None
    path = Path(text).expanduser()
    if not path.is_file():
        raise Refused(f"screenshot {text!r} is not an existing file or an https:// link; give "
                      "the path of an image file, or a link starting https://.")
    data = path.read_bytes()
    stored = f"{hashlib.sha256(data).hexdigest()}{path.suffix.lower()}"
    return {"file": stored, "name": path.name}, data, stored


def trade(con: sqlite3.Connection, run: str, *, ticker: str, side: str, entry_type: str,
          entry: float, size: int, currency: str, reasons: str, stop: float | None = None,
          target: float | None = None, screenshots=(), at: datetime | None = None,
          instrument: str | None = None, point_value: float | None = None,
          now: datetime | None = None) -> TradeView:
    """Enter a trade on a run: one `trades` row and one `entered` event, written together.
    Everything is validated, and every screenshot file read, before anything is written."""
    row = store.run(con, run)
    if row is None:
        raise Refused(f"There is no run {run!r}; list_runs to see the ones that exist.")
    when = _decision_time(row["mode"], at, now, "enter a trade")
    if side not in ("long", "short"):
        raise Refused(f"side {side!r} is not known; use 'long' (profit if the price rises) or "
                      "'short' (profit if it falls).")
    if entry_type not in ("market", "limit"):
        raise Refused(f"entry_type {entry_type!r} is not known; use 'market' (fill at once at the "
                      "going price) or 'limit' (fill only at your price or better).")
    symbol, implied = model.canonical_ticker(_text("ticker", ticker))
    if not symbol:
        raise Refused("ticker is empty; give the symbol, e.g. 'ACME' or 'GC1!'.")
    if instrument not in (None, "stock", "etf", "future"):
        raise Refused(f"instrument {instrument!r} is not known; use 'stock', 'etf' or 'future'.")
    if implied == "future" and instrument not in (None, "future"):
        raise Refused(f"{ticker!r} is a continuous futures contract, so instrument must be "
                      f"'future', not {instrument!r}; leave instrument out.")
    kind = implied or instrument or "stock"
    if kind == "future":
        if point_value is None:
            raise Refused("A future needs point_value: the cash a one-point move is worth on one "
                          "contract, e.g. 100.")
        _price("point_value", point_value)
    elif point_value is not None:  # dropping it would understate the risk for good
        raise Refused(f"point_value is only for a future and {ticker!r} reads as "
                      f"{'an' if kind == 'etf' else 'a'} {kind}; pass instrument='future' (or a "
                      f"continuous ticker such as '{symbol}1!'), or leave point_value out.")
    else:
        point_value = 1.0
    _price("entry", entry)
    _price("stop", stop, required=False)
    _price("target", target, required=False)
    if stop == entry:
        raise Refused("stop equals entry, so the risk (the distance to the stop, 1R) is zero and "
                      "no reward-to-risk can be measured; put the stop away from the entry.")
    _size(size)
    _currency(currency)
    if not _text("reasons", reasons).strip():
        raise Refused("reasons is empty; say why you are taking this trade, in your own words.")
    shots = [_screenshot(s) for s in _strings("screenshots", screenshots)]
    with store.transaction(con):  # the run's state and the id's collisions are read under the lock
        _within_run(_run_view(con, row), when)
        for _, data, stored in shots:  # after every refusal; content-addressed, so a repeat skips
            if data is None:  # a link: nothing to store
                continue
            final = config.attachments_dir() / stored
            if not final.exists():
                final.parent.mkdir(parents=True, exist_ok=True)
                # Whole or not at all under the final name: a short file there would be skipped
                # by exists() forever. The write lock serialises writers, so one temp name does.
                partial = final.with_name(f"{stored}.partial")
                with partial.open("wb") as out:
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(partial, final)
        id = _generate_trade_id(symbol, when.astimezone().strftime("%Y%m%d"),
                                [t["id"] for t in store.trades(con)])
        store.add_trade(con, id, run, symbol, side, kind, float(point_value), currency)
        store.append(con, kind="entered", run_id=run, trade_id=id, at=_at(when),
                     recorded_at=_at(now),
                     payload={"entry_type": entry_type, "entry": float(entry),
                              "stop": stop and float(stop), "target": target and float(target),
                              "size": size, "reasons": reasons,
                              "screenshots": [s[0] for s in shots]})
    return _trade_view(con, store.trade(con, id))


def _trade_row(con, trade: str) -> sqlite3.Row:
    row = store.trade(con, trade)
    if row is None:
        raise Refused(f"There is no trade {trade!r}; list_trades to see the ones that exist.")
    return row


def _decision(con, trade: str, reason) -> sqlite3.Row:
    """The trade's identity row, once the decision's reason is given and the trade exists."""
    if not _text("reason", reason).strip():
        raise Refused("reason is empty; say why, in your own words.")
    return _trade_row(con, trade)


def _not_void(con, trade: str) -> tuple[model.Decisions, int, tuple[int, ...]]:
    """The trade's fold; call it under the write lock, since what it reads decides the write."""
    folded = model._fold(con, trade)
    if folded[0].voided:
        raise Refused(f"Trade {trade!r} is void, so it takes no further decision; if it should "
                      "count, enter it again with its original `at`.")
    return folded


def _after_entry(trade: str, d: model.Decisions, when: datetime) -> None:
    if when < d.entry.at:
        raise Refused(f"Trade {trade!r} was entered at {d.entry.at.isoformat()}; a decision on it "
                      "cannot be before that, so give a later `at`.")


def _before_close(trade: str, d: model.Decisions, when: datetime) -> None:
    """A move is strictly before the hand close: one at the close's `at` would
    take effect only from the bar after the exit."""
    if d.close_at is not None and when >= d.close_at:
        raise Refused(f"Trade {trade!r} was closed by hand at {d.close_at.isoformat()}; a move "
                      "at or after that has no order to move, so give an earlier `at`.")


def move_order(con: sqlite3.Connection, trade: str, *, reason: str, stop: float | None = None,
               target: float | None = None, at: datetime | None = None,
               now: datetime | None = None) -> TradeView:
    """Move the trade's stop, its target or both, from `at` on."""
    now = _aware(now)
    row = _decision(con, trade, reason)
    when = _decision_time(store.run(con, row["run_id"])["mode"], at, now, "move an order")
    if stop is None and target is None:
        raise Refused("A move names a new stop, a new target, or both; give at least one.")
    _price("stop", stop, required=False)
    _price("target", target, required=False)
    with store.transaction(con):
        d = _not_void(con, trade)[0]
        _after_entry(trade, d, when)
        _before_close(trade, d, when)
        moved ={k: float(v) for k, v in (("stop", stop), ("target", target)) if v is not None}
        store.append(con, kind="order_moved", run_id=row["run_id"], trade_id=trade, at=_at(when),
                     recorded_at=_at(now), payload={**moved, "reason": reason})
    return _trade_view(con, row)


def close_trade(con: sqlite3.Connection, trade: str, *, reason: str, at: datetime | None = None,
                now: datetime | None = None) -> TradeView:
    """Close the trade by hand at `at`. A trade is closed once."""
    now = _aware(now)
    row = _decision(con, trade, reason)
    when = _decision_time(store.run(con, row["run_id"])["mode"], at, now, "close a trade")
    with store.transaction(con):
        d = _not_void(con, trade)[0]
        if d.close_at is not None:
            raise Refused(f"Trade {trade!r} was already closed at {d.close_at.isoformat()}: "
                          f"{d.close_reason!r}. A trade is closed once; if that close was a "
                          "mistake, void the trade and enter it again.")
        _after_entry(trade, d, when)
        if when == d.entry.at:  # a close is strictly after the entry
            raise Refused(f"Trade {trade!r} was entered at {d.entry.at.isoformat()}; a close at "
                          "that same moment leaves it no time open, so give a later `at`.")
        if d.moves and when <= d.moves[-1].at:  # moves are in `at` order: the last is latest
            raise Refused(f"Trade {trade!r} has a move at {d.moves[-1].at.isoformat()}; a close "
                          "at or before it would leave that move after the close, so give a "
                          "later `at`.")
        store.append(con, kind="closed", run_id=row["run_id"], trade_id=trade, at=_at(when),
                     recorded_at=_at(now), payload={"reason": reason})
    return _trade_view(con, row)


_ENTRY_FIELDS = ("entry", "stop", "target", "size", "at", "point_value", "currency")
_MOVE_FIELDS = ("stop", "target", "at")


def correct_trade(con: sqlite3.Connection, trade: str, *, fields: dict, reason: str,
                  now: datetime | None = None) -> TradeView:
    """Correct the entry, or with `move: <index>` (its place in get_trade's moves) one move.
    Appended as a `corrected` event naming that `entered` or `order_moved` event, read in its
    place; only the fields named change. A field given as None is not given, so a
    stop or target is never removed by a correction. A wrong run, ticker or side is not
    corrected either: void the trade and enter it again."""
    now = _aware(now)
    row = _decision(con, trade, reason)
    if not isinstance(fields, dict):
        raise Refused("fields must be a mapping of the fields to correct to their corrected "
                      f"values, e.g. {{'stop': 97.5}}; got {fields!r}.")
    asked, fields = fields, {k: v for k, v in fields.items() if v is not None}
    index = fields.pop("move", None)
    names = _ENTRY_FIELDS if index is None else _MOVE_FIELDS
    if not fields:
        removal = (" A stop or target is not removed by a correction: void the trade and enter "
                   "it again without it." if {"stop", "target"} & asked.keys() else "")
        raise Refused("fields names nothing to correct (a field given as None is read as not "
                      f"given); give one or more of {', '.join(names)}.{removal}")
    if unknown := [k for k in fields if k not in names]:
        raise Refused(f"{', '.join(unknown)} cannot be corrected on "
                      f"{'an entry' if index is None else 'a move'}; correct only "
                      f"{', '.join(names)}, or void the trade and enter it again.")
    for name in ("entry", "point_value"):
        if name in fields:
            _price(name, fields[name])
    _price("stop", fields.get("stop"), required=False)
    _price("target", fields.get("target"), required=False)
    if "size" in fields:
        _size(fields["size"])
    if "currency" in fields:
        _currency(fields["currency"])
    if "point_value" in fields and row["instrument"] != "future":
        raise Refused(f"Trade {trade!r} is a {row['instrument']}, whose point value is 1; only a "
                      "future's point value can be corrected.")
    when = _aware(fields["at"], "at") if "at" in fields else None
    with store.transaction(con):  # the decisions the correction is checked against, under the lock
        d, entered, move_ids = _not_void(con, trade)
        if index is None:
            if fields.get("entry", d.entry.price) == fields.get("stop", d.entry.stop):
                raise Refused("The corrected stop equals the entry, so the risk (the distance to "
                              "the stop, 1R) is zero; put the stop away from the entry.")
            if when is not None:
                _within_run(_run_view(con, store.run(con, row["run_id"])), when)
                later = [m.at for m in d.moves]
                if d.close_at is not None and when >= d.close_at:  # an entry is before the close
                    raise Refused(f"Trade {trade!r} was closed by hand at "
                                  f"{d.close_at.isoformat()}; its entry must be before that, so "
                                  "give an earlier `at`.")
                if later and when > min(later):
                    raise Refused(f"Trade {trade!r} has a decision at {min(later).isoformat()}; "
                                  "its entry cannot be after that, so give an `at` at or "
                                  "before it.")
            corrects = entered
        else:
            if (isinstance(index, bool) or not isinstance(index, int)
                    or not 0 <= index < len(d.moves)):
                raise Refused(f"Trade {trade!r} has no move {index!r}; `move` is a move's place "
                              f"in get_trade's moves, from 0, and it has {len(d.moves)}.")
            if when is not None:
                _after_entry(trade, d, when)
                _before_close(trade, d, when)
            corrects = move_ids[index]
        stored = {**fields, **{k: float(fields[k]) for k in ("entry", "stop", "target",
                                                             "point_value") if fields.get(k)}}
        if when is not None:
            stored["at"] = _at(when)
        store.append(con, kind="corrected", run_id=row["run_id"], trade_id=trade, at=_at(now),
                     recorded_at=_at(now), corrects=corrects,
                     payload={"fields": stored, "reason": reason})
    return _trade_view(con, row)


def void_trade(con: sqlite3.Connection, trade: str, *, reason: str,
               now: datetime | None = None) -> TradeView:
    """Void a trade: it stays in the log; later releases' statistics and checks leave it out."""
    now = _aware(now)
    row = _decision(con, trade, reason)
    with store.transaction(con):
        _not_void(con, trade)
        store.append(con, kind="voided", run_id=row["run_id"], trade_id=trade, at=_at(now),
                     recorded_at=_at(now), payload={"reason": reason})
    return _trade_view(con, row)


def get_trade(con: sqlite3.Connection, trade: str) -> TradeView:
    return _trade_view(con, _trade_row(con, trade))


def list_trades(con: sqlite3.Connection, run: str | None = None,
                strategy: str | None = None) -> list[TradeView]:
    """Every trade in the order it was recorded, voided ones included, narrowed to a run and a
    strategy when given."""
    views = [_trade_view(con, r) for r in store.trades(con, run)]
    return [v for v in views if strategy in (None, v.strategy)]
