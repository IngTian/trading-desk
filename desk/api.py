"""The desk's one outward face: what desk/server.py serves as MCP tools, function for function.

Every function takes an open connection and `now` (default: the wall clock, in UTC) so tests
control time. Every refusal is a `Refused` whose message says what to change.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from desk import checks, config, model, outcome, store, strategy
from desk.errors import Refused, shown
from desk.rules import Rules, finite, market_keys
from desk.stats import Row, Stats, summarise  # `stats` below shadows the module's name

__all__ = ["Refused", "StrategyView", "save_strategy", "get_strategy", "list_strategies",
           "RunView", "start_run", "stop_run", "list_runs", "TradeView", "CheckView", "trade",
           "move_order", "report", "correct_trade", "void_trade", "get_trade",
           "list_trades", "Stats", "stats", "StoppedRun"]

_SLUG = re.compile(r"[a-z0-9-]{1,64}")  # with fullmatch; the schema's CHECK on strategies.id


def _aware(when, name: str = "now") -> datetime:
    """`when` (default: the wall clock) as an aware datetime, given or read from an ISO-8601
    string with its offset; a naive one is refused, since its zone would be guessed."""
    if when is None:
        return datetime.now(UTC)
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when)
        except ValueError:
            pass
    try:
        if isinstance(when, datetime) and when.tzinfo is not None:
            when.astimezone(UTC)   # an offset time at the calendar's edge overflows here
            return when
    except OverflowError:
        pass
    raise Refused(f"`{name}` must be an ISO-8601 time with its UTC offset, e.g. "
                  f"2026-03-02T14:30:00Z; got {shown(when)}.")


def _at(now: datetime | None) -> str:
    return _aware(now).astimezone(UTC).isoformat()


# The MCP server will pass the agent's JSON straight in, so a parameter can arrive as any JSON type.
def _text(name: str, value) -> str:
    if not isinstance(value, str):
        raise Refused(f"{name} must be text, a string; got {shown(value)}.")
    return value


def _version_number(v) -> int | None:
    if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
        raise Refused("version must be a whole number from 1, e.g. 2, or left out; got "
                      f"{shown(v)}.")
    return v


def _strings(name: str, value) -> tuple[str, ...]:
    """A list of strings; None is none. A bare string is refused, not read letter by letter."""
    if value is None:
        return ()
    if not isinstance(value, list | tuple) or not all(isinstance(v, str) for v in value):
        raise Refused(f'{name} must be a list of strings, e.g. ["a", "b"], or left out; '
                      f"got {shown(value)}.")
    return tuple(value)


@dataclass(frozen=True)
class StrategyView:
    id: str
    name: str
    version: int
    versions: int
    text: str
    rules: dict
    timeframes: tuple[str, ...]
    checklist: tuple[str, ...]
    market_keys: tuple[str, ...]   # what a trade's `market` may supply
    current_hash: str   # the latest version's text_hash: pass it back as `if_current`
    saved: str          # "version 3 created" | "unchanged"


def _view(con, id: str, version: int | None, saved: str) -> StrategyView:
    rows = store.versions(con, id)
    if not rows:
        raise Refused(f"There is no strategy {shown(id)}; save it first, or list_strategies to see "
                      "the ones that exist.")
    row = rows[-1] if version is None else next((r for r in rows if r["version"] == version), None)
    if row is None:
        raise Refused(f"Strategy {shown(id)} has no version {version}; it has versions 1 to "
                      f"{len(rows)}.")
    text = row["text"]
    return StrategyView(
        id=id, name=strategy.name_of(text), version=row["version"], versions=len(rows),
        text=text, rules=json.loads(row["rules"]), timeframes=tuple(json.loads(row["timeframes"])),
        checklist=tuple(json.loads(row["checklist"])),
        market_keys=market_keys(json.loads(row["rules"]),
                                      tuple(json.loads(row["timeframes"]))),
        current_hash=rows[-1]["text_hash"], saved=saved)


def save_strategy(con: sqlite3.Connection, id: str, markdown: str, *,
                  if_current: str | None = None, now: datetime | None = None) -> StrategyView:
    """Save a strategy (new id: version 1; existing id: needs `if_current`, the `current_hash`
    last read). Text that changes after whitespace is normalised makes a new version."""
    if if_current is not None and not isinstance(if_current, str):
        raise Refused("if_current must be text: the current_hash get_strategy returned; got "
                      f"{shown(if_current)}.")
    if not _SLUG.fullmatch(_text("id", id)):
        raise Refused(f"Strategy id {shown(id)} must be 1 to 64 lowercase letters, digits and "
                      "hyphens, like 'dip-buyer'.")
    parsed = strategy.parse(_text("markdown", markdown))
    at = _at(now)
    with store.transaction(con):  # read, compare and write under one write lock
        rows = store.versions(con, id)
        if rows:
            if if_current is None:
                raise Refused(f"Strategy {shown(id)} already exists; pass if_current (the "
                              f"current_hash "
                              "from get_strategy) so a save cannot overwrite someone else's "
                              "change.")
            if if_current != rows[-1]["text_hash"]:
                raise Refused(f"Strategy {shown(id)} was changed since you read it; get it again, "
                              "redo your edit on that, and save.")
        elif if_current is not None:
            raise Refused(f"Strategy {shown(id)} is new, so if_current must be left out.")

        new_text = not rows or rows[-1]["text_hash"] != parsed.text_hash
        if not rows:
            store.add_strategy(con, id, at)
        if new_text:
            store.add_version(con, id, len(rows) + 1, parsed.text, parsed.text_hash,
                              dict(parsed.rules), list(parsed.timeframes),
                              list(parsed.checklist), at)
    return _view(con, id, None, f"version {len(rows) + 1} created" if new_text else "unchanged")


def get_strategy(con: sqlite3.Connection, id: str, version: int | None = None) -> StrategyView:
    _text("id", id)
    return _view(con, id, _version_number(version), "unchanged")


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
    currency: str


def _run_view(con, row: sqlite3.Row) -> RunView:
    when = {e["kind"]: datetime.fromisoformat(e["at"])
            for e in store.events(con, run_id=row["id"]) if e["kind"].startswith("run_")}
    return RunView(id=row["id"], strategy=row["strategy_id"], version=row["version"],
                   mode=row["mode"], tags=tuple(json.loads(row["tags"])),
                   started=when["run_started"], stopped=when.get("run_stopped"),
                   currency=row["currency"])


# The two tails of the after-now refusal: a decision is dated when it happened, a report states
# a state the trade was already in.
_WHEN_IT_HAPPENED = "is dated when it happened, so give that moment, in UTC."
_REPORT_STATE = ("a report states a state the trade was already in, so give the moment it was in "
                 "it, in UTC.")


def _not_future(when: datetime, now: datetime, says: str) -> None:
    """Refuse an `at` after `now`; `says` completes the reason in words."""
    if when > now:
        raise Refused(f"`at` {when.isoformat()} is after now ({now.isoformat()}); {says}")


def _decision_time(mode: str, at: datetime | None, now: datetime, what: str) -> datetime:
    """A live decision defaults to `now` (already an aware time); a replay one never defaults, or
    it would be dated after every bar in the history being replayed."""
    if at is not None:
        when = _aware(at, "at")
        noun = {"enter a trade": "an entry", "move an order": "a move"}.get(what, f"a {what}")
        _not_future(when, now, f"{noun} {_WHEN_IT_HAPPENED}")
        return when
    if mode == "replay":
        raise Refused(f"A replay run needs `at` to {what}: give the moment in the replayed "
                      "history, not the wall clock.")
    return now


def _right_side(side: str, entry, stop, target) -> None:
    """A long's stop is below its entry and its target above; a short's the mirror."""
    long = side == "long"
    if stop is not None and (stop > entry if long else stop < entry):
        raise Refused(f"stop {shown(stop)} is on the wrong side of entry {shown(entry)} for a "
                      f"{side}: a {side}'s stop is {'below' if long else 'above'} its entry, so "
                      "check the stop and the side.")
    if target is not None and (target <= entry if long else target >= entry):
        raise Refused(f"target {shown(target)} is on the wrong side of entry {shown(entry)} for a "
                      f"{side}: a {side}'s target is {'above' if long else 'below'} its entry, "
                      "so check the target and the side.")


def start_run(con: sqlite3.Connection, strategy: str, *, mode: str, currency=None, tags=(),
              version: int | None = None, at: datetime | None = None,
              now: datetime | None = None) -> RunView:
    """Deploy a strategy version as run `<strategy>-r<N>`. Tags are kept verbatim (deduplicated,
    order kept) and never interpreted. The run trades in one currency, CAD or USD."""
    _text("strategy", strategy)
    _version_number(version)
    _currency(currency)
    tags = _strings("tags", tags)
    if mode not in ("live", "replay"):
        raise Refused(f"mode {shown(mode)} is not known; use \"live\" (real time) or \"replay\" "
                      "(re-running past history).")
    now = _aware(now)
    start = _decision_time(mode, at, now, "start")
    with store.transaction(con):  # the run number and version are read under the write lock
        rows = store.versions(con, strategy)
        if not rows:
            raise Refused(f"There is no strategy {shown(strategy)}; save it first, or "
                          f"list_strategies "
                          "to see the ones that exist.")
        v = rows[-1]["version"] if version is None else version
        if v not in {r["version"] for r in rows}:
            raise Refused(f"Strategy {shown(strategy)} has no version {v}; it has versions 1 to "
                          f"{len(rows)}.")
        id = f"{strategy}-r{len(store.runs(con, strategy)) + 1}"
        store.add_run(con, id, strategy, v, mode, list(dict.fromkeys(tags)),
                      currency)
        store.append(con, kind="run_started", run_id=id, at=_at(start), payload={},
                     recorded_at=_at(now))
    return _run_view(con, store.run(con, id))


@dataclass(frozen=True)
class StoppedRun(RunView):
    """What stop_run returns: the run, and `cancelled`, its non-void limit orders with no fill
    whose outcome reads cancelled, why "run stopped", in recording order. An order cancelled for
    another why, and an order exited, are left out."""
    cancelled: tuple[str, ...] = ()


def stop_run(con: sqlite3.Connection, run: str, *, at: datetime | None = None,
             now: datetime | None = None) -> StoppedRun:
    """Stop the run at `at`: not before its start. Every non-void trade of the run is then
    checked against the stop, which cancels its orders still pending and leaves no room for an
    entry at or after it. The reply is a StoppedRun: its `cancelled` holds the run's unfilled
    limit orders the stop cancelled, in recording order."""
    row = store.run(con, _text("run", run))
    if row is None:
        raise Refused(f"There is no run {shown(run)}; list_runs to see the ones that exist.")
    now = _aware(now)
    stop = _decision_time(row["mode"], at, now, "stop")
    with store.transaction(con):  # the run's state is checked under the write lock
        view = _run_view(con, row)
        if view.stopped is not None:
            raise Refused(f"Run {shown(run)} is already stopped; start a new run instead.")
        if stop < view.started:
            raise Refused(f"Run {shown(run)} started at {view.started.isoformat()}; its stop "
                          f"cannot "
                          "be before that, so give a later `at`.")
        standing = [(t, d) for t in store.trades(con, run)
                    if not (d := model.decisions_of(con, t["id"])).voided]
        store.append(con, kind="run_stopped", run_id=run, at=_at(stop), payload={},
                     recorded_at=_at(now))
        for t, _ in standing:
            _consistent(con, t)
    cancelled = [t["id"] for t, d in standing   # read after the transaction
                 if d.entry.type == "limit"
                 and not (d.exited is not None and d.exited.at < stop)
                 and (o := _outcome(con, t, d)).fill is None
                 and o.status == "cancelled" and o.cancelled_why == "run stopped"]
    return StoppedRun(**asdict(_run_view(con, row)), cancelled=tuple(cancelled))


def list_runs(con: sqlite3.Connection, strategy: str | None = None) -> list[RunView]:
    if strategy is not None:
        _text("strategy", strategy)
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
    # What the trade's record reads as.
    outcome: outcome.Outcome
    # The run's version's rules as shown at entry; None only for stored results that do not read
    # (integrity reports them).
    checks_shown: tuple[checks.Result, ...] | None = None
    # One {"item", "verdict", "reason"} per checklist item of the version, in its order; an
    # unanswered one reads verdict "not answered", reason None. Never scored.
    checklist: tuple[dict, ...] = ()
    checks_now: tuple[checks.Result, ...] | None = None
    checks_differ: tuple[str, ...] | None = None


@dataclass(frozen=True)
class CheckView:
    """What trade(check_only=True) returns: the results an entry would be shown, written nowhere."""
    run: str
    at: datetime
    ticker: str
    side: str
    instrument: str
    point_value: float
    currency: str
    checks: tuple[checks.Result, ...]
    checklist: tuple[dict, ...]


def _version(con, run_row: sqlite3.Row) -> tuple[Rules, tuple[str, ...]]:
    """The run's version's rules and checklist items: never the strategy's latest."""
    v = next(r for r in store.versions(con, run_row["strategy_id"])
             if r["version"] == run_row["version"])
    return (Rules(json.loads(v["rules"]), tuple(json.loads(v["timeframes"]))),
            tuple(json.loads(v["checklist"])))


def _outcome(con, row: sqlite3.Row, d: model.Decisions) -> outcome.Outcome:
    """The trade's outcome, read from its record alone, on a live run as on a replay one."""
    return outcome.of(d, side=row["side"], stopped=_run_view(con, store.run(
        con, row["run_id"])).stopped)


def _trade_view(con, row: sqlite3.Row, now: datetime) -> TradeView:
    """Built after any transaction, so nothing is read under desk.db's write lock."""
    run = store.run(con, row["run_id"])
    d = model.decisions_of(con, row["id"])
    try:
        kept = None if d.checks is None else tuple(checks.Result(**c) for c in d.checks)
    except (TypeError, KeyError):   # stored results that do not read: integrity reports them
        kept = None
    return TradeView(id=row["id"], run=row["run_id"], strategy=run["strategy_id"],
                     version=run["version"], ticker=row["ticker"], side=row["side"],
                     instrument=row["instrument"], point_value=d.point_value,
                     currency=d.currency, decisions=d, outcome=_outcome(con, row, d),
                     checks_shown=kept, checklist=_checklist_view(_version(con, run)[1],
                                                                   d.checklist))


def _within_run(view: RunView, when: datetime) -> None:
    """A trade's entry falls inside its run: not before the start, and before any stop. The
    validator reads the same rule, but a check_only entry writes nothing, so it never reaches it."""
    if outside := model.entry_outside_run(when, view.started, view.stopped):
        raise Refused(f"A trade on run {shown(view.id)} cannot be {outside}")


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
    if isinstance(v, bool) or not isinstance(v, int | float) or not finite(v) or v <= 0:
        raise Refused(f"{name} must be a number above zero, e.g. 101.5; got {shown(v)}.")


def _size(v) -> None:
    # a size past a float's range would overflow the checks' arithmetic
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0 or not finite(v):
        raise Refused(f"size must be a whole number above zero, e.g. 10; got {shown(v)}.")


def _currency(v) -> None:
    if v not in config.CURRENCIES:
        raise Refused(f"currency {shown(v)} is not known; use one of "
                      f"{', '.join(config.CURRENCIES)}.")


def _screenshot(text: str) -> tuple[dict, bytes | None, str | None]:
    """(payload entry, file bytes, stored name) for one screenshot, or Refused. Writes nothing."""
    if text.startswith("https://"):
        return {"link": text}, None, None
    try:
        path = Path(text).expanduser()
    except RuntimeError:   # "~nosuchuser": no home to expand
        path = Path(text)
    if not path.is_absolute():
        raise Refused(f"screenshot {shown(text)} is not a full path; give the path from / or ~, "
                      "since the desk's working folder is not yours, or an https:// link.")
    if not path.is_file():
        raise Refused(f"screenshot {shown(text)} is not an existing file or an https:// link; give "
                      "the path of an image file, or a link starting https://.")
    data = path.read_bytes()
    stored = f"{hashlib.sha256(data).hexdigest()}{path.suffix.lower()}"
    return {"file": stored, "name": path.name}, data, stored


# ------------------------------------------------- the checks, as of a trade's `at` ---
def _other(row: sqlite3.Row, d: model.Decisions, o: outcome.Outcome, seq: int) -> checks.Other:
    """Another trade of the run as the checks read it, from its outcome. An exit always carries
    its cash (outcome._money gives one), which the record rules add up."""
    return checks.Other(id=row["id"], ticker=row["ticker"], entered=d.entry.at, seq=seq,
                        ended=o.exited_at or o.cancelled_at, exited=o.status == "exited",
                        r=o.r, cash=o.cash)


def _others(con, run_row: sqlite3.Row, skip: str | None = None) -> tuple[checks.Other, ...]:
    """The run's non-void trades but `skip`, each from its outcome (the checks read them as of
    `at` from those dates). `seq` is the index in the unfiltered store.trades, so leaving voids
    out never renumbers one."""
    out = []
    for seq, t in enumerate(store.trades(con, run_row["id"])):
        if t["id"] == skip:
            continue
        d = model.decisions_of(con, t["id"])
        if not d.voided:
            out.append(_other(t, d, _outcome(con, t, d), seq))
    return tuple(out)


def _plan(row: sqlite3.Row, d: model.Decisions) -> model.Plan:
    """The trade's plan as recorded: the identity row and the entry as corrected."""
    e = d.entry
    return model.Plan(row["ticker"], row["side"], row["instrument"], d.point_value, d.currency,
                      e.price, e.stop, e.target, e.size)


def _results_now(con, row: sqlite3.Row, d: model.Decisions, others: tuple[checks.Other, ...]
                 ) -> tuple[checks.Result, ...]:
    """The run's version's rules on the record as it stands, as of the entry's `at`. `others` never
    holds this trade."""
    run = store.run(con, row["run_id"])
    rules = _version(con, run)[0]
    seq = next(i for i, t in enumerate(store.trades(con, run["id"])) if t["id"] == row["id"])
    # a damaged stored value reads not supplied (integrity reports it), as a checklist does
    market = {k: v for k, v in (d.market.items() if isinstance(d.market, dict) else ())
              if isinstance(v, int | float) and not isinstance(v, bool) and finite(v)}
    return tuple(checks.run_checks(_plan(row, d), rules, checks.Context(
        at=d.entry.at, seq=seq, currency=run["currency"], others=others, market=market)))


def _differ(shown: tuple[checks.Result, ...] | None, now: tuple[checks.Result, ...]
            ) -> tuple[str, ...] | None:
    """The names whose (ok, figure) differ between the results shown and now, or are in one only;
    None when no results were shown to compare."""
    if shown is None:
        return None
    then = {r.name: (r.ok, r.figure) for r in shown}
    here = {r.name: (r.ok, r.figure) for r in now}
    return tuple(dict.fromkeys(n for n in [*here, *then]
                               if n not in then or n not in here or then[n] != here[n]))


def _market(rules: Rules, market) -> dict[str, float]:
    """The agent's values: only the keys the version's rules read, each a finite
    number, kept as floats. None is none."""
    if market is None:
        return {}
    if not isinstance(market, dict):
        raise Refused("market must be a mapping of market keys to numbers, e.g. "
                      f"{{\"rsi_14_15m\": 28.5}}, or left out; got {shown(market)}.")
    keys = market_keys(rules.values, rules.timeframes)
    for k, v in market.items():
        if not isinstance(k, str) or k not in keys:
            raise Refused(f"market key {shown(k)} is not one this version's rules read; " + (
                f"they read {', '.join(keys)}." if keys
                else "this version's rules read no market values, so leave market out."))
        if isinstance(v, bool) or not isinstance(v, int | float) or not finite(v):
            raise Refused(f"market[{shown(k)}] must be a finite number, e.g. 28.5; got {shown(v)}.")
    return {k: float(v) for k, v in market.items()}


def _answers(items: tuple[str, ...], checklist) -> tuple[dict, ...]:
    """The agent's checklist answers, kept as given in the order given. None is
    none."""
    if checklist is None:
        return ()
    form = '{"item": …, "verdict": "pass" or "fail", "reason": …}'
    if not isinstance(checklist, list):
        raise Refused(f"checklist must be a list of answers, each {form}, or left out; got "
                      f"{shown(checklist)}.")
    seen = set()
    for a in checklist:
        if not isinstance(a, dict) or set(a) != {"item", "verdict", "reason"}:
            raise Refused(f"A checklist answer is {form}, with exactly those keys; got {shown(a)}.")
        if a["item"] not in items:
            listed = "; ".join(shown(i) for i in items) or "it has none"
            raise Refused(f"checklist item {shown(a['item'])} is not one of this version's items, "
                          f"which are matched by their exact text: {listed}.")
        if a["item"] in seen:
            raise Refused(f"checklist item {shown(a['item'])} has a second answer; give one.")
        seen.add(a["item"])
        if a["verdict"] not in ("pass", "fail"):
            raise Refused(f"checklist verdict {shown(a['verdict'])} is not known; use pass or "
                          f"fail.")
        if not isinstance(a["reason"], str) or not a["reason"].strip():
            raise Refused(f"checklist item {shown(a['item'])} has no reason; say why, in one line.")
    return tuple(checklist)


def _checklist_view(items: tuple[str, ...], answers) -> tuple[dict, ...]:
    """One answer per item of the version, in its order. An unanswered item reads "not answered",
    as does one whose stored answer trade() would not write (integrity's entered_checks reports
    that one), so a damaged record never fails a read."""
    given = {a["item"]: a for a in (answers if isinstance(answers, tuple | list) else ())
             if model.is_answer(a)}
    return tuple(given.get(i, {"item": i, "verdict": "not answered", "reason": None})
                 for i in items)


def trade(con: sqlite3.Connection, run: str, *, ticker: str, side: str, entry_type: str,
          entry: float, size: int, currency: str, reasons: str, stop: float | None = None,
          target: float | None = None, screenshots=(), at: datetime | None = None,
          instrument: str | None = None, point_value: float | None = None, market=None,
          checklist=None, check_only=None, now: datetime | None = None) -> TradeView | CheckView:
    """Enter a trade on a run: one `trades` row and one `entered` event, written together, with
    the results of the run's version's rules as of `at`, as shown: a breach never refuses a
    trade. `market` holds the agent's values for the keys the rules read, `checklist` its
    answers. With check_only, validate and check exactly as an entry, write nothing, and return
    a CheckView. Everything is validated, and every screenshot file read, before anything is
    written."""
    now = _aware(now)
    row = store.run(con, _text("run", run))
    if row is None:
        raise Refused(f"There is no run {shown(run)}; list_runs to see the ones that exist.")
    when = _decision_time(row["mode"], at, now, "enter a trade")
    if side not in ("long", "short"):
        raise Refused(f"side {shown(side)} is not known; use \"long\" (profit if the price rises) "
                      f"or "
                      "\"short\" (profit if it falls).")
    if entry_type not in ("market", "limit"):
        raise Refused(f"entry_type {shown(entry_type)} is not known; use \"market\" (fill at once "
                      f"at the "
                      "going price) or \"limit\" (fill only at your price or better).")
    symbol, implied = model.canonical_ticker(_text("ticker", ticker))
    if not symbol:
        raise Refused("ticker is empty; give the symbol, e.g. \"ACME\" or \"GC1!\".")
    if instrument not in (None, "stock", "etf", "future"):
        raise Refused(f"instrument {shown(instrument)} is not known; use \"stock\", \"etf\" or "
                      f"\"future\".")
    if implied == "future" and instrument not in (None, "future"):
        raise Refused(f"{shown(ticker)} is a continuous futures contract, so instrument must be "
                      f"\"future\", not {shown(instrument)}; leave instrument out.")
    if implied is None and instrument is None:
        raise Refused(f"instrument is required for {shown(ticker)}: say \"stock\", \"etf\" or "
                      "\"future\" (a future also needs point_value), or write a future in its "
                      "continuous form, such as \"GC1!\".")
    kind = implied or instrument
    if kind == "future":
        if point_value is None:
            raise Refused("A future needs point_value: the cash a one-point move is worth on one "
                          "contract, e.g. 100.")
        _price("point_value", point_value)
    elif point_value is not None:  # dropping it would understate the risk for good
        raise Refused(f"point_value is only for a future and {shown(ticker)} reads as "
                      f"{'an' if kind == 'etf' else 'a'} {kind}; pass instrument=\"future\" (or a "
                      f"continuous ticker such as \"GC1!\"), or leave point_value out.")
    else:
        point_value = 1.0
    _price("entry", entry)
    _price("stop", stop, required=False)
    _price("target", target, required=False)
    if stop == entry:
        raise Refused("stop equals entry, so the risk (the distance to the stop, 1R) is zero and "
                      "no reward-to-risk can be measured; put the stop away from the entry.")
    _right_side(side, entry, stop, target)
    _size(size)
    _currency(currency)
    if currency != row["currency"]:  # a run's currency never changes, so no lock is needed
        raise Refused(f"Run {shown(run)} is in {row['currency']}; this trade is in {currency}. A "
                      f"run "
                      f"is in one currency: enter it on a {currency} run, or give "
                      f"currency={shown(row['currency'])}.")
    _text("reasons", reasons)
    if check_only is not None and not isinstance(check_only, bool):
        raise Refused(f"check_only must be true, false or left out; got {shown(check_only)}.")
    rules, items = _version(con, row)
    market = _market(rules, market)
    answers = _answers(items, checklist)
    shots = [_screenshot(s) for s in _strings("screenshots", screenshots)]
    plan = model.Plan(symbol, side, kind, float(point_value), currency, float(entry),
                      stop and float(stop), target and float(target), size)
    record = bool(set(checks.RECORD) & set(rules.values))   # else no rule reads another trade

    def as_shown(others: tuple[checks.Other, ...], seq: int | None) -> tuple[checks.Result, ...]:
        return tuple(checks.run_checks(plan, rules, checks.Context(
            at=when, seq=seq, currency=row["currency"], others=others, market=market)))
    if check_only:   # a preview writes nothing, so it checks without the write lock
        _within_run(_run_view(con, row), when)
        others = _others(con, row) if record else ()
        return CheckView(run=run, at=when, ticker=symbol, side=side, instrument=kind,
                         point_value=float(point_value), currency=currency,
                         checks=as_shown(others, None), checklist=_checklist_view(items, answers))
    if not reasons.strip():
        raise Refused("reasons is empty; say why you are taking this trade, in your own words.")
    with store.transaction(con):  # the run's state and the id's collisions are read under the lock
        _within_run(_run_view(con, row), when)
        results = as_shown(_others(con, row) if record else (), len(store.trades(con, run)))
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
                dfd = os.open(final.parent, os.O_RDONLY)  # a rename is durable once its dir is
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
        id = _generate_trade_id(symbol, when.astimezone().strftime("%Y%m%d"),
                                [t["id"] for t in store.trades(con)])
        store.add_trade(con, id, run, symbol, side, kind, float(point_value), currency)
        store.append(con, kind="entered", run_id=run, trade_id=id, at=_at(when),
                     recorded_at=_at(now),
                     payload={"entry_type": entry_type, "entry": float(entry),
                              "stop": stop and float(stop), "target": target and float(target),
                              "size": size, "reasons": reasons,
                              "screenshots": [s[0] for s in shots],
                              "checks": [asdict(r) for r in results], "market": market,
                              "checklist": list(answers)})
        _consistent(con, store.trade(con, id))
    return _trade_view(con, store.trade(con, id), now)


def _trade_row(con, trade: str) -> sqlite3.Row:
    row = store.trade(con, trade)
    if row is None:
        raise Refused(f"There is no trade {shown(trade)}; list_trades to see the ones that exist.")
    return row


def _decision(con, trade: str, reason) -> sqlite3.Row:
    """The trade's identity row, once the id is text, the decision's reason is given and the
    trade exists."""
    _text("trade", trade)
    if not _text("reason", reason).strip():
        raise Refused("reason is empty; say why, in your own words.")
    return _trade_row(con, trade)


def _not_void(con, trade: str) -> tuple[model.Decisions, int, tuple[int, ...], dict[str, int]]:
    """The trade's fold; call it under the write lock, since what it reads decides the write."""
    folded = model.fold(con, trade)
    if folded[0].voided:
        raise Refused(f"Trade {shown(trade)} is void, so it takes no further decision; if it "
                      f"should "
                      "count, enter it again with its original `at`.")
    return folded


def _not_exited(trade: str, d: model.Decisions) -> None:
    if d.exited is not None:
        raise Refused(f"Trade {shown(trade)} already exited at {d.exited.at.isoformat()}: "
                      f"{shown(d.exited.reason)}. A trade exits once: to give that exit the "
                      "broker's price, correct it (correct_trade with fields "
                      "{\"report\": \"exited\", \"price\": …}); if it was a mistake, correct it "
                      "(fields {\"report\": \"exited\", …}) or withdraw it (void_trade with "
                      "report \"exited\").")


def _consistent(con, row: sqlite3.Row) -> None:
    """Refuse when the trade's record, as it now stands, is wrong: fold it and ask the one
    validator. Called after the append, inside the transaction, so a refusal rolls it back."""
    run = _run_view(con, store.run(con, row["run_id"]))
    problem = model.record_problem(row["id"], model.decisions_of(con, row["id"]),
                                   side=row["side"], started=run.started, stopped=run.stopped)
    if problem:
        raise Refused(problem)


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
        _not_void(con, trade)
        moved = {k: float(v) for k, v in (("stop", stop), ("target", target)) if v is not None}
        store.append(con, kind="order_moved", run_id=row["run_id"], trade_id=trade, at=_at(when),
                     recorded_at=_at(now), payload={**moved, "reason": reason})
        _consistent(con, row)
    return _trade_view(con, row, now)


# What each report state takes; a correction of one takes the same fields plus `at`.
_TAKES = {"filled": ("price",), "cancelled": ("why",), "open": (), "exited": ("price", "kind")}


def _kind(kind) -> str:
    if kind not in ("hand", "stop", "target"):
        raise Refused(f"kind {shown(kind)} is not known; use \"hand\" (exited by hand), \"stop\" "
                      f"(the stop "
                      "filled) or \"target\" (the target filled), or leave it out for \"hand\".")
    return kind


def _why(why) -> str:
    if why not in ("not filled", "pulled", "run stopped"):
        raise Refused(f"why {shown(why)} is not known; use \"not filled\" (the market never "
                      f"reached the "
                      "limit, or the order expired unfilled), \"pulled\" (the author withdrew "
                      "it) or \"run stopped\" (the run's stop cancelled it; dated at the stop).")
    return why


def report(con: sqlite3.Connection, trade: str, *, state: str, at: datetime, reason: str,
           price: float | None = None, kind: str | None = None, why: str | None = None,
           now: datetime | None = None) -> TradeView:
    """Report the trade's state as of `at`, which is required in both modes:
    "filled" (the limit order filled; price None: at the limit), "cancelled" (it never will;
    why "not filled", "pulled" or "run stopped" (the run's stop cancelled it, dated at the
    stop)), "open" (the position is still open) or "exited" (kind "stop",
    "target" or "hand", None meaning "hand"; a hand exit gives its price, and a stop or target
    exit given none is at the level in force). Checked against the record alone."""
    row = _decision(con, trade, reason)
    now = _aware(now)
    if state not in model.REPORTS:  # a tuple: a JSON list or dict compares, never hashes
        raise Refused(f"state {shown(state)} is not known; use \"filled\" (the limit order "
                      f"filled), "
                      "\"cancelled\" (it never will), \"open\" (the position is still open) or "
                      "\"exited\" (the position is out, at its stop, at its target or by hand).")
    if at is None:
        raise Refused("A report states the trade's state as of `at`, so it needs `at`, in live as "
                      "in replay: give the moment the order or the position was in that state, "
                      "not the moment you report it.")
    when = _aware(at, "at")
    _not_future(when, now, _REPORT_STATE)
    for name, value in (("price", price), ("kind", kind), ("why", why)):
        if value is not None and name not in _TAKES[state]:
            raise Refused(f"{name} does not go with state={shown(state)}: price goes with "
                          f"\"filled\" and "
                          "\"exited\", kind with \"exited\", why with \"cancelled\".")
    if state == "cancelled":
        _why(why)
    if state == "exited":
        kind = _kind("hand" if kind is None else kind)
        if kind == "hand" and price is None:
            raise Refused("A hand exit is reported with the price it filled at; give price=….")
    _price("price", price, required=False)
    given = {"why": why, "kind": kind, "price": None if price is None else float(price)}
    payload = {"reason": reason, **{k: v for k, v in given.items() if v is not None}}
    with store.transaction(con):
        d = _not_void(con, trade)[0]
        for asked, standing in (("filled", d.filled), ("cancelled", d.cancelled)):
            if state == asked and standing is not None:
                raise Refused(f"The {_NOUNS[asked]} of trade {shown(trade)} was already reported "
                              f"at {standing.at.isoformat()}; correct it (correct_trade with "
                              f"fields {{\"report\": \"{asked}\", …}}) or withdraw it "
                              f"(void_trade with report \"{asked}\").")
        if state == "exited":
            _not_exited(trade, d)
        store.append(con, kind=state, run_id=row["run_id"], trade_id=trade, at=_at(when),
                     recorded_at=_at(now), payload=payload)
        _consistent(con, row)
    return _trade_view(con, row, now)


_ENTRY_FIELDS = ("entry", "stop", "target", "size", "at", "point_value")
_MOVE_FIELDS = ("stop", "target", "at")
_NOUNS = {'filled': 'fill', 'cancelled': 'cancellation', 'open': 'open report', 'exited': 'exit'}


def correct_trade(con: sqlite3.Connection, trade: str, *, fields: dict, reason: str,
                  now: datetime | None = None) -> TradeView:
    """Correct the entry, with `move: <index>` (its place in get_trade's moves) one move, or with
    `report: "filled" | "cancelled" | "open" | "exited"` one standing report ("open": the latest).
    Appended as a `corrected` event naming that event, read in its place; only the fields named
    change. A field given as None is not given, so a stop or target is never removed by a
    correction. A wrong run, ticker or side is not corrected either: void the trade and enter
    it again."""
    now = _aware(now)
    row = _decision(con, trade, reason)
    if not isinstance(fields, dict):
        raise Refused("fields must be a mapping of the fields to correct to their corrected "
                      f"values, e.g. {{\"stop\": 97.5}}; got {shown(fields)}.")
    asked, fields = fields, {k: v for k, v in fields.items() if v is not None}
    index, which = fields.pop("move", None), fields.pop("report", None)
    if index is not None and (isinstance(index, bool) or not isinstance(index, int)
                              or index < 0):
        raise Refused("move must be a whole number from 0, a move's place in get_trade's moves; "
                      f"got {shown(index)}.")
    if index is not None and which is not None:
        raise Refused("fields names both a move and a report; correct one at a time.")
    if which is not None and which not in model.REPORTS:
        raise Refused(f"report {shown(which)} is not known; use \"filled\", \"cancelled\", "
                      "\"open\" or \"exited\".")
    if which is not None:
        names, noun = (*_TAKES[which], "at"), _NOUNS[which]
        on, redo = (f"{'an' if which in ('open', 'exited') else 'a'} {noun}",
                    f"withdraw it (void_trade with report {shown(which)}) and report it again")
    else:
        names = _ENTRY_FIELDS if index is None else _MOVE_FIELDS
        on, redo = "an entry" if index is None else "a move", "void the trade and enter it again"
    if not fields:
        removal = (" A stop or target is not removed by a correction: void the trade and enter "
                   "it again without it." if {"stop", "target"} & asked.keys() else "")
        raise Refused("fields names nothing to correct (a field given as null is read as not "
                      f"given); give one or more of {', '.join(names)}.{removal}")
    if unknown := [k for k in fields if k not in names]:
        raise Refused(f"{', '.join(unknown)} cannot be corrected on {on}; correct only "
                      f"{', '.join(names)}, or {redo}.")
    for name in ("entry", "point_value"):
        if name in fields:
            _price(name, fields[name])
    for name in ("stop", "target", "price"):
        _price(name, fields.get(name), required=False)
    if "kind" in fields:
        _kind(fields["kind"])
    if "why" in fields:
        _why(fields["why"])
    if "size" in fields:
        _size(fields["size"])
    if "point_value" in fields and row["instrument"] != "future":
        raise Refused(f"Trade {shown(trade)} is a {row['instrument']}, whose point value is 1; "
                      f"only a "
                      "future's point value can be corrected.")
    when = _aware(fields["at"], "at") if "at" in fields else None
    if when is not None:
        if which is not None:
            _not_future(when, now, _REPORT_STATE)
        else:
            noun = "a move" if index is not None else "an entry"
            _not_future(when, now, f"{noun} {_WHEN_IT_HAPPENED}")

    with store.transaction(con):  # the decisions the correction is checked against, under the lock
        d, entered, move_ids, reports = _not_void(con, trade)
        if which is not None:
            if which not in reports:
                raise Refused(f"Trade {shown(trade)} has no {noun} to correct; get_trade shows its "
                              "reports.")
            corrects = reports[which]
        elif index is None:
            if fields.get("entry", d.entry.price) == fields.get("stop", d.entry.stop):
                raise Refused("The corrected stop equals the entry, so the risk (the distance to "
                              "the stop, 1R) is zero; put the stop away from the entry.")
            _right_side(row["side"], fields.get("entry", d.entry.price),
                        fields.get("stop", d.entry.stop), fields.get("target", d.entry.target))
            corrects = entered
        else:
            if index >= len(d.moves):   # `move`'s type and `>= 0` are checked where it is read
                raise Refused(f"Trade {shown(trade)} has no move {shown(index)}; `move` is a "
                              f"move's place in get_trade's moves, from 0, and it has "
                              f"{len(d.moves)}.")
            corrects = move_ids[index]
        stored = {**fields, **{k: float(fields[k]) for k in ("entry", "stop", "target",
                                                             "point_value", "price")
                               if fields.get(k)}}
        if when is not None:
            stored["at"] = _at(when)
        store.append(con, kind="corrected", run_id=row["run_id"], trade_id=trade, at=_at(now),
                     recorded_at=_at(now), corrects=corrects,
                     payload={"fields": stored, "reason": reason})
        _consistent(con, row)
    return _trade_view(con, row, now)


def void_trade(con: sqlite3.Connection, trade: str, *, reason: str, report: str | None = None,
               now: datetime | None = None) -> TradeView:
    """Void a trade: it stays in the log; statistics and the checks of other trades leave it out.
    With `report` ("filled", "cancelled", "open" or "exited"), withdraw that standing report
    ("open": the latest, leaving the one before it standing): it is read as absent, and the
    trade stands."""
    now = _aware(now)
    row = _decision(con, trade, reason)
    if report is not None and report not in model.REPORTS:
        raise Refused(f"report {shown(report)} is not known; use \"filled\", \"cancelled\", "
                      "\"open\" or \"exited\", or leave it out to void the whole trade.")
    with store.transaction(con):
        reports = _not_void(con, trade)[3]
        if report is not None and report not in reports:
            raise Refused(f"Trade {shown(trade)} has no {_NOUNS[report]} to withdraw; get_trade "
                          f"shows "
                          "its reports.")
        store.append(con, kind="voided", run_id=row["run_id"], trade_id=trade, at=_at(now),
                     recorded_at=_at(now), corrects=None if report is None else reports[report],
                     payload={"reason": reason})
        if report is not None:
            _consistent(con, row)
    return _trade_view(con, row, now)


def get_trade(con: sqlite3.Connection, trade: str, *, now: datetime | None = None) -> TradeView:
    now = _aware(now)
    row = _trade_row(con, _text("trade", trade))
    view = _trade_view(con, row, now)
    d = view.decisions
    if d.voided:
        return view
    run = store.run(con, row["run_id"])
    others = (_others(con, run, skip=row["id"])
              if set(checks.RECORD) & set(_version(con, run)[0].values) else ())
    here = _results_now(con, row, d, others)
    return replace(view, checks_now=here, checks_differ=_differ(view.checks_shown, here))


def _open(v: TradeView) -> bool:
    """Not void, and still in the market or waiting to be."""
    return not v.decisions.voided and v.outcome.status in ("pending", "open")


def list_trades(con: sqlite3.Connection, run: str | None = None, strategy: str | None = None,
                *, open: bool | None = None, now: datetime | None = None) -> list[TradeView]:
    """Every trade in the order it was recorded, voided ones included, narrowed to a run and a
    strategy when given, and to the open trades (open=True) or the rest (open=False)."""
    now = _aware(now)
    for name, value in (("run", run), ("strategy", strategy)):
        if value is not None:
            _text(name, value)
    if open is not None and not isinstance(open, bool):
        raise Refused(f"open must be true, false or left out; got {shown(open)}.")
    rows = [r for r in store.trades(con, run)
            if strategy in (None, store.run(con, r["run_id"])["strategy_id"])]
    return [v for v in (_trade_view(con, r, now) for r in rows) if open in (None, _open(v))]


_NO_SCOPE = ("stats needs one scope: a run, a strategy, or a strategy with a version; every "
             "strategy pooled would be account-level P&L, which the desk does not report")


def stats(con: sqlite3.Connection, *, strategy: str | None = None, version: int | None = None,
          run: str | None = None, mode: str | None = None, tags=None, conforming_only=None,
          now: datetime | None = None) -> Stats:
    """Counts, rates and averages over one scope's non-void trades, from their outcomes: a run, a
    strategy's version, or a strategy. Narrowed by mode, by tags (every one given also on the run,
    exactly) and, with conforming_only, to the trades whose results now are all kept. Never a
    verdict or a ranking."""
    now = _aware(now)
    for name, value in (("strategy", strategy), ("run", run)):
        if value is not None:
            _text(name, value)
    _version_number(version)
    if mode not in (None, "live", "replay"):
        raise Refused(f"mode {shown(mode)} is not known; use \"live\" or \"replay\", or leave it "
                      f"out.")
    tags = _strings("tags", tags)
    if conforming_only is not None and not isinstance(conforming_only, bool):
        raise Refused(f"conforming_only must be true, false or left out; got "
                      f"{shown(conforming_only)}.")
    if run is not None:
        if strategy is not None or version is not None:
            raise Refused("Give one scope: a run alone, a strategy, or a strategy with a version; "
                          "a run already names its strategy and its version.")
        row = store.run(con, run)
        if row is None:
            raise Refused(f"There is no run {shown(run)}; list_runs to see the ones that exist.")
        runs, strategy, version = [row], row["strategy_id"], row["version"]
    elif strategy is None:
        raise Refused(_NO_SCOPE if version is None else "A version is named with its strategy: "
                      "give strategy=… beside version=…, or give a run.")
    else:
        _view(con, strategy, version, "unchanged")   # refuses an unknown strategy or version
        runs = [r for r in store.runs(con, strategy) if version in (None, r["version"])]
    scope = [(r, run_tags) for r in runs if mode in (None, r["mode"])
             and set(tags) <= set(run_tags := tuple(json.loads(r["tags"])))]
    # Narrowed before any trade is read; a void is left out.
    read = [(r, run_tags, t, _trade_view(con, t, now), seq) for r, run_tags in scope
            for seq, t in enumerate(store.trades(con, r["id"]))
            if not model.decisions_of(con, t["id"]).voided]
    left_out: dict = {}
    if conforming_only:
        others: dict[str, list[checks.Other]] = {}   # each run's, built once
        for r, _, t, v, seq in read:
            others.setdefault(r["id"], []).append(_other(t, v.decisions, v.outcome, seq))
        breached, unknown, kept = [], {}, []
        for r, run_tags, t, v, seq in read:
            results = _results_now(con, t, v.decisions,
                                   tuple(o for o in others[r["id"]] if o.id != v.id))
            if any(x.ok is False for x in results):
                breached.append(v.id)
            elif all(x.ok is True for x in results):   # no rules: kept
                kept.append((r, run_tags, t, v, seq))
            else:
                for x in results:
                    if x.ok is None:
                        unknown.setdefault(x.name, []).append(v.id)
        read = kept
        left_out = {"breached": tuple(breached),
                    "unknown": {name: tuple(ids) for name, ids in unknown.items()}}
    rows = [Row(id=v.id, run=v.run, version=v.version, mode=r["mode"], tags=run_tags,
                entry_type=v.decisions.entry.type, currency=v.currency, outcome=v.outcome,
                planned_rr=checks.reward_risk(_plan(t, v.decisions))[0])
            for r, run_tags, t, v, _ in read]
    return summarise(rows, strategy=strategy, version=version, run=run, mode=mode, tags=tags,
                     conforming_only=bool(conforming_only), left_out=left_out)
