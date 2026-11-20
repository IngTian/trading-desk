"""The desk's one outward face: what the MCP server (plan 5) wraps, function for function.

Every function takes an open connection and `now` (default: the wall clock, in UTC) so tests
control time. Every refusal is a `Refused` whose message says what to change.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from desk import config, model, store, strategy
from desk.checks import _hours
from desk.errors import Refused

__all__ = ["Refused", "StrategyView", "save_strategy", "get_strategy", "list_strategies",
           "RunView", "start_run", "stop_run", "list_runs", "TradeView", "trade"]

_SLUG = re.compile(r"[a-z0-9-]+")
_BARS = re.compile(r"(\d+) bars after exit")
_AFTER = " after exit"


def _aware(when: datetime | None, name: str = "now") -> datetime:
    """`when` (default: the wall clock) as an aware datetime; a naive one is refused, since
    its zone would be guessed."""
    if when is None:
        return datetime.now(UTC)
    if when.tzinfo is None:
        raise Refused(f"`{name}` has no timezone; give an aware time, e.g. "
                      "datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc).")
    return when


def _at(now: datetime | None) -> str:
    return _aware(now).astimezone(UTC).isoformat()


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
        id=id, name=strategy.parse(text).name, version=row["version"], versions=len(rows),
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
    if not _SLUG.fullmatch(id):
        raise Refused(f"Strategy id {id!r} must be lowercase letters, digits and hyphens, "
                      "like 'dip-buyer'.")
    parsed = strategy.parse(markdown)
    if tracker_limit is not None:
        _check_limit(tracker_limit)
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
        try:
            if not rows:
                store.add_strategy(con, id, at)
            if new_text:
                store.add_version(con, id, len(rows) + 1, parsed.text, parsed.text_hash,
                                  dict(parsed.rules), list(parsed.timeframes), at)
            if new_tracker:
                store.add_tracker(con, id, script, script_hash, limit or None, at)
        except sqlite3.IntegrityError as e:
            raise Refused(f"Strategy {id!r} was changed by another session; get it again, "
                          "redo your edit on that, and save.") from e
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
    return TradeView(id=row["id"], run=row["run_id"], strategy=run["strategy_id"],
                     version=run["version"], ticker=row["ticker"], side=row["side"],
                     instrument=row["instrument"], point_value=row["point_value"],
                     currency=row["currency"], decisions=model.decisions_of(con, row["id"]))


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


def _screenshot(item) -> tuple[dict, bytes | None, str | None]:
    """(payload entry, file bytes, stored name) for one screenshot, or Refused. Writes nothing."""
    text = str(item)
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
    symbol, implied = model.canonical_ticker(ticker)
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
    else:
        point_value = 1.0
    _price("entry", entry)
    _price("stop", stop, required=False)
    _price("target", target, required=False)
    if stop == entry:
        raise Refused("stop equals entry, so the risk (the distance to the stop, 1R) is zero and "
                      "no reward-to-risk can be measured; put the stop away from the entry.")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise Refused(f"size must be a whole number above zero, e.g. 10; got {size!r}.")
    if currency not in config.CURRENCIES:
        raise Refused(f"currency {currency!r} is not known; use one of "
                      f"{', '.join(config.CURRENCIES)}.")
    if not reasons.strip():
        raise Refused("reasons is empty; say why you are taking this trade, in your own words.")
    shots = [_screenshot(s) for s in screenshots]
    started = _run_view(con, row).started
    if when < started:
        raise Refused(f"Run {run!r} started at {started.isoformat()}; a trade cannot be entered "
                      "before that, so give a later `at`.")
    with store.transaction(con):  # the run's state and the id's collisions are read under the lock
        view = _run_view(con, row)
        if view.stopped is not None and when >= view.stopped:
            raise Refused(f"Run {run!r} stopped at {view.stopped.isoformat()}; a trade cannot be "
                          "entered at or after that, so give an earlier `at`.")
        for _, data, stored in shots:  # after every refusal; content-addressed, so a repeat skips
            if data is not None and not (config.attachments_dir() / stored).exists():
                config.attachments_dir().mkdir(parents=True, exist_ok=True)
                (config.attachments_dir() / stored).write_bytes(data)
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
