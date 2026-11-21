"""The desk's one outward face: what plan 5's MCP server wraps, function for function.

Every function takes an open connection and `now` (default: the wall clock, in UTC) so tests
control time. Every refusal is a `Refused` whose message says what to change.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from bisect import bisect_left, bisect_right
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from desk import bars, checks, config, indicators, judge, model, store, strategy, yahoo
from desk.errors import Refused
from desk.rules import Indicator, Rules, _finite, market_keys
from desk.rules import indicators as indicator_checks  # `indicators` is the formulas' module
from desk.stats import Row, Stats, summarise  # `stats` below shadows the module's name
from desk.strategy import minutes  # start_run's `strategy` parameter shadows the module

__all__ = ["Refused", "StrategyView", "save_strategy", "get_strategy", "list_strategies",
           "RunView", "start_run", "stop_run", "list_runs", "TradeView", "CheckView", "trade",
           "move_order", "close_trade", "report", "correct_trade", "void_trade", "get_trade",
           "list_trades", "TradeBars", "trade_bars", "Stats", "stats"]

_SLUG = re.compile(r"[a-z0-9-]{1,64}")  # with fullmatch; the schema's CHECK on strategies.id
_YAHOO_HAS = "1m, 2m, 5m, 15m, 30m, 1h, 90m, 1d or 1w"   # yahoo.INTERVALS, as a refusal names them


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
    rules: dict
    timeframes: tuple[str, ...]
    checklist: tuple[str, ...]
    current_hash: str   # the latest version's text_hash: pass it back as `if_current`
    saved: str          # "version 3 created" | "unchanged"


def _view(con, id: str, version: int | None, saved: str) -> StrategyView:
    rows = store.versions(con, id)
    if not rows:
        raise Refused(f"There is no strategy {id!r}; save it first, or list_strategies to see "
                      "the ones that exist.")
    row = rows[-1] if version is None else next((r for r in rows if r["version"] == version), None)
    if row is None:
        raise Refused(f"Strategy {id!r} has no version {version}; it has versions 1 to "
                      f"{len(rows)}.")
    text = row["text"]
    return StrategyView(
        id=id, name=strategy.name_of(text), version=row["version"], versions=len(rows),
        text=text, rules=json.loads(row["rules"]), timeframes=tuple(json.loads(row["timeframes"])),
        checklist=tuple(json.loads(row["checklist"])),
        current_hash=rows[-1]["text_hash"], saved=saved)


def save_strategy(con: sqlite3.Connection, id: str, markdown: str, *,
                  if_current: str | None = None, now: datetime | None = None) -> StrategyView:
    """Save a strategy (new id: version 1; existing id: needs `if_current`, the `current_hash`
    last read). Text that differs after whitespace is normalised makes a new version."""
    if not _SLUG.fullmatch(_text("id", id)):
        raise Refused(f"Strategy id {id!r} must be 1 to 64 lowercase letters, digits and "
                      "hyphens, like 'dip-buyer'.")
    parsed = strategy.parse(_text("markdown", markdown))
    at = _at(now)
    with store.transaction(con):  # read, compare and write under one write lock
        rows = store.versions(con, id)
        if rows:
            if if_current is None:
                raise Refused(f"Strategy {id!r} already exists; pass if_current (the current_hash "
                              "from get_strategy) so a save cannot overwrite someone else's "
                              "change.")
            if if_current != rows[-1]["text_hash"]:
                raise Refused(f"Strategy {id!r} was changed since you read it; get it again, "
                              "redo your edit on that, and save.")
        elif if_current is not None:
            raise Refused(f"Strategy {id!r} is new, so if_current must be left out.")

        new_text = not rows or rows[-1]["text_hash"] != parsed.text_hash
        if not rows:
            store.add_strategy(con, id, at)
        if new_text:
            store.add_version(con, id, len(rows) + 1, parsed.text, parsed.text_hash,
                              dict(parsed.rules), list(parsed.timeframes),
                              list(parsed.checklist), at)
    return _view(con, id, None, f"version {len(rows) + 1} created" if new_text else "unchanged")


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
    currency: str


def _run_view(con, row: sqlite3.Row) -> RunView:
    when = {e["kind"]: datetime.fromisoformat(e["at"])
            for e in store.events(con, run_id=row["id"]) if e["kind"].startswith("run_")}
    return RunView(id=row["id"], strategy=row["strategy_id"], version=row["version"],
                   mode=row["mode"], tags=tuple(json.loads(row["tags"])),
                   started=when["run_started"], stopped=when.get("run_stopped"),
                   currency=row["currency"])


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


def start_run(con: sqlite3.Connection, strategy: str, *, mode: str, currency=None, tags=(),
              version: int | None = None, at: datetime | None = None,
              now: datetime | None = None) -> RunView:
    """Deploy a strategy version as run `<strategy>-r<N>`. Tags are kept verbatim (deduplicated,
    order kept) and never interpreted. The run trades in one currency, CAD or USD."""
    _currency(currency)
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
        finest = json.loads(next(r for r in rows if r["version"] == v)["timeframes"])[0]
        if mode == "live" and minutes(finest) not in yahoo.INTERVALS:
            raise Refused(f"Live runs read Yahoo, which has no {finest} bars; give the strategy a "
                          f"finest timeframe of {_YAHOO_HAS}, or start a replay run.")
        id = f"{strategy}-r{len(store.runs(con, strategy)) + 1}"
        store.add_run(con, id, strategy, v, mode, list(dict.fromkeys(tags)),
                      currency)
        store.append(con, kind="run_started", run_id=id, at=_at(start), payload={},
                     recorded_at=_at(now))
    return _run_view(con, store.run(con, id))


def stop_run(con: sqlite3.Connection, run: str, *, at: datetime | None = None,
             now: datetime | None = None) -> RunView:
    """Stop the run at `at`: not before its start, and after every non-void trade's entry
    (whichever was logged first), since trade() refuses an entry at or after the stop, and at or
    after every reported fill and cancellation, since a run stop cancels the orders still
    pending."""
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
        standing = [(t, d) for t in store.trades(con, run)
                    if not (d := model.decisions_of(con, t["id"])).voided]
        entries = [(d.entry.at, t["id"]) for t, d in standing]
        if entries and (last := max(entries))[0] >= stop:
            raise Refused(f"Trade {last[1]!r} on run {run!r} was entered at "
                          f"{last[0].isoformat()}; the run cannot stop at or before a trade on "
                          "it, so give a later `at` (or void that trade, if it does not belong).")
        store.append(con, kind="run_stopped", run_id=run, at=_at(stop), payload={},
                     recorded_at=_at(now))
        for t, _ in standing:
            _consistent(con, t)
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
    # The reports over the judge (live), or the reports alone (replay).
    outcome: judge.Outcome
    judged: judge.Outcome | None   # live: the bars' answer to the decisions alone; replay: None
    differs: tuple[str, ...]       # the fields where `outcome` and `judged` differ
    # The run's version's rules as shown at entry; None only for stored results that do not read
    # (integrity reports them).
    checks_shown: tuple[checks.Result, ...] | None = None
    # One {"item", "verdict", "reason"} per checklist item of the version, in its order; an
    # unanswered one reads verdict "not answered", reason None. Never scored.
    checklist: tuple[dict, ...] = ()
    checks_now: tuple[checks.Result, ...] | None = None
    checks_differ: tuple[str, ...] = ()


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


def _timeframes(con, run_row: sqlite3.Row) -> dict[int, str]:
    """The run's version's timeframes, minutes -> name, finest first."""
    return {minutes(n): n for n in _version(con, run_row)[0].timeframes}


def _r6(timeframe: str) -> str:
    """Ruling R6: why a future's daily or weekly Yahoo bar is not read."""
    return (f"Yahoo stamps a future's {timeframe} bar at midnight exchange time, after its session "
            "opened the evening before, so an entry made that evening would be judged against "
            "prices from before it")


def _not_judged(row: sqlite3.Row, d: model.Decisions, note: str, *, live: bool
                ) -> tuple[judge.Outcome, judge.Outcome | None]:
    """The reports over "not judged", and it (None when not live): the reports need no bars."""
    market = d.entry.type == "market"
    nj = judge.Outcome(**{**judge._BLANK, "status": "not judged",
                          "fill": d.entry.price if market else None,
                          "filled_at": d.entry.at if market else None,
                          "judged_through": d.entry.at, "missing": (d.entry.at, None)},
                       notes=(note,))
    try:
        base = nj
        if d.fill is not None:
            fill, filled_at = judge._position(d)
            base = replace(nj, fill=fill, filled_at=filled_at)
        o = judge._ends(d, base, side=row["side"])
    except Exception:
        o = nj
    return (o, nj) if live else (o, None)


def _outcomes(con, row: sqlite3.Row, d: model.Decisions, now: datetime, *, fetch: bool = True
              ) -> tuple[judge.Outcome, judge.Outcome | None]:
    """(outcome, judged). Replay: the reports alone, and no bars.db. Live: judged on the run's
    version's finest timeframe from the Yahoo bars kept, fetching more only while the trade has
    not ended (an end in complete, insert-only bars moves only with a decision), and never past
    the bar holding its end. Never raises."""
    run = store.run(con, row["run_id"])
    mode = run["mode"]
    try:
        stopped = _run_view(con, run).stopped
        if mode == "replay":
            return judge.replay(d, side=row["side"], stopped=stopped), None
        m, name = next(iter(_timeframes(con, run).items()))
        future = row["instrument"] == "future"
        if future and m in (1440, 10080):  # ruling R6; drop once a real daily future bar is checked
            return _not_judged(row, d, (
                f"not judged: {_r6(name)}; the outcome is what you report: the exit, or the "
                "order's cancellation"), live=True)
        symbol = yahoo.ticker_symbol(row["ticker"], row["instrument"], d.currency)
        start = d.entry.at - timedelta(minutes=m)
        with closing(bars.open()) as bcon:
            def judged(unavailable):
                return judge.live(d, side=row["side"], minutes=m,
                                  bars=bars.read(bcon, yahoo.SOURCE, symbol, m, start),
                                  covered=bars.covered(bcon, yahoo.SOURCE, symbol, m),
                                  now=now, stopped=stopped, unavailable=unavailable)
            o, j = judged(None)
            if fetch and not {o.status, j.status} <= {"exited", "cancelled"}:
                ended = o.exited_at or o.cancelled_at   # the judged walk closes there too
                o, j = judged(yahoo.ensure(bcon, symbol, m, start, now,
                                           end=ended and ended + timedelta(minutes=m)))
        if d.cancel or d.close_price is not None or d.close_kind in ("stop", "target"):
            # a reported end reaches the judge as a hand close the author never decided
            j = replace(j, notes=tuple(n for n in j.notes if not n.startswith("the close at ")))
        notes = (f"judged on Yahoo's {symbol}, a rolling front-month series: at a roll its bars "
                 "switch to the next contract",) if future else ()
        return tuple(replace(x, timeframe=name, notes=x.notes + notes) for x in (o, j))
    except Exception as e:  # a read never fails, and never drops a report (Q9)
        return _not_judged(row, d, f"bars could not be read: {type(e).__name__}: {e}",
                           live=mode == "live")


def _trade_view(con, row: sqlite3.Row, now: datetime) -> TradeView:
    """Built after any transaction, so a fetch never runs under desk.db's write lock."""
    run = store.run(con, row["run_id"])
    d = model.decisions_of(con, row["id"])
    o, j = _outcomes(con, row, d, now)
    try:
        shown = None if d.checks is None else tuple(checks.Result(**c) for c in d.checks)
    except (TypeError, KeyError):   # stored results that do not read: integrity reports them
        shown = None
    return TradeView(id=row["id"], run=row["run_id"], strategy=run["strategy_id"],
                     version=run["version"], ticker=row["ticker"], side=row["side"],
                     instrument=row["instrument"], point_value=d.point_value,
                     currency=d.currency, decisions=d, outcome=o, judged=j,
                     differs=judge.differs(o, j, next(iter(_timeframes(con, run)))),
                     checks_shown=shown, checklist=_checklist_view(_version(con, run)[1],
                                                                   d.checklist))


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
    if isinstance(v, bool) or not isinstance(v, int | float) or not _finite(v) or v <= 0:
        raise Refused(f"{name} must be a number above zero, e.g. 101.5; got {v!r}.")


def _size(v) -> None:
    # a size past a float's range would overflow the checks' arithmetic
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0 or not _finite(v):
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


# ------------------------------------------------- the checks, as of a trade's `at` (§7) ---
def _settles(o: judge.Outcome) -> str:
    """The report that settles a trade whose state is unknown."""
    if o.fill is None:
        return ("report its fill or its cancellation (report(trade, state='filled' | "
                "'cancelled', at=…))")
    return ("report its exit, or that it is still open (report(trade, state='exited' | 'open', "
            "at=…))")


def _other(row: sqlite3.Row, d: model.Decisions, o: judge.Outcome, seq: int, *, live: bool,
           minutes: int) -> checks.Other:
    """Another trade of the run as the checks read it, from its effective outcome. An exit always
    carries its cash (judge._money gives one), which the record rules add up. `live` and `minutes`
    are its run's mode and finest timeframe."""
    unknown_from = closed_at = None
    settles = _settles(o)
    if o.status == "not judged":   # a reported open position is known open up to its report
        unknown_from = o.missing[0] if d.held is None else max(o.missing[0], d.held.at)
        closed_at = d.close_at
    elif o.status == "closing":    # a hand close whose last bar is still forming
        unknown_from = closed_at = d.close_at
        settles = ("report its exit with the broker's price (report(trade, state='exited', "
                   "price=…, at=…))")
    elif live and row["instrument"] == "future" and o.status in ("open", "pending"):
        # Yahoo delays futures (Notes 5): an exit in the bar after judged_through is dated at its
        # close, so it is unknown from there; checks._unknown's `<` is strict, hence the 1 µs.
        unknown_from = o.judged_through + timedelta(minutes=minutes, microseconds=-1)
    return checks.Other(id=row["id"], ticker=row["ticker"], entered=d.entry.at, seq=seq,
                        ended=o.exited_at or o.cancelled_at, exited=o.status == "exited",
                        r=o.r, cash=o.cash, unknown_from=unknown_from, closed_at=closed_at,
                        settles=settles)


def _others(con, run_row: sqlite3.Row, now: datetime, *, fetch: bool,
            skip: str | None = None) -> tuple[checks.Other, ...]:
    """The run's non-void trades but `skip`, each from its effective outcome now (the checks read
    them as of `at` from those dates). `seq` is the index in the unfiltered store.trades, so
    leaving voids out never renumbers one."""
    out = []
    kw = dict(live=run_row["mode"] == "live", minutes=next(iter(_timeframes(con, run_row))))
    for seq, t in enumerate(store.trades(con, run_row["id"])):
        if t["id"] == skip:
            continue
        d = model.decisions_of(con, t["id"])
        if not d.voided:
            out.append(_other(t, d, _outcomes(con, t, d, now, fetch=fetch)[0], seq, **kw))
    return tuple(out)


def _plan(row: sqlite3.Row, d: model.Decisions) -> model.Plan:
    """The trade's plan as recorded: the identity row and the entry as corrected."""
    e = d.entry
    return model.Plan(row["ticker"], row["side"], row["instrument"], d.point_value, d.currency,
                      e.price, e.stop, e.target, e.size)


def _results_now(con, row: sqlite3.Row, d: model.Decisions, others: tuple[checks.Other, ...],
                 now: datetime) -> tuple[checks.Result, ...]:
    """The run's version's rules on the record as it stands, as of the entry's `at`. `others` never
    holds this trade. It fetches, so it is never called under the write lock."""
    run = store.run(con, row["run_id"])
    rules = _version(con, run)[0]
    seq = next(i for i, t in enumerate(store.trades(con, run["id"])) if t["id"] == row["id"])
    # a damaged stored value reads not supplied (integrity reports it), as a checklist does (R5)
    market = {k: v for k, v in (d.market.items() if isinstance(d.market, dict) else ())
              if isinstance(v, int | float) and not isinstance(v, bool) and _finite(v)}
    return tuple(checks.run_checks(_plan(row, d), rules, checks.Context(
        at=d.entry.at, seq=seq, currency=run["currency"], others=others, market=market,
        computed=_figures(con, run, row["ticker"], row["instrument"], d.currency, rules,
                          d.entry.at, now))))


def _differ(shown: tuple[checks.Result, ...] | None, now: tuple[checks.Result, ...]
            ) -> tuple[str, ...]:
    """The names whose (ok, figure) differ between the results shown and now, or are in one only."""
    if shown is None:
        return ()
    then = {r.name: (r.ok, r.figure) for r in shown}
    here = {r.name: (r.ok, r.figure) for r in now}
    return tuple(dict.fromkeys(n for n in [*here, *then]
                               if n not in then or n not in here or then[n] != here[n]))


def _span(w: int, m: int) -> timedelta:
    """How far back `w` bars of `m` minutes reach, nights, weekends and holidays included."""
    # shortcut: a calendar estimate of W bars; widen it if a strategy reads unknown for want of
    # warm-up
    if m < 1440:
        return timedelta(minutes=w * m * 6, days=4)
    return timedelta(days=w * 1.6 + 10) if m == 1440 else timedelta(weeks=w * 1.1 + 3)


def _value(i: Indicator, used: list[judge.Bar]) -> float | None:
    """Indicator check `i`'s value on the bars `used`, by Pine's formula."""
    closes, s = [b.close for b in used], i.settings
    if i.rule == "stop_atr":
        return indicators.atr(used, s["length"])
    if i.rule == "macd":
        out = indicators.macd(closes, s["fast"], s["slow"], s["signal"])
        return None if out is None else out[("line", "signal", "histogram").index(s["output"])]
    return {"rsi": indicators.rsi, "ema": indicators.ema, "sma": indicators.sma}[i.rule](
        closes, s["length"])


def _computed(bcon, symbol: str, instrument: str, tf: str, group: list[Indicator], at: datetime,
              now: datetime) -> dict[str, checks.Figure]:
    """The figures of the indicator checks on one Yahoo timeframe, from its completed bars before
    `at`: never an older bar in silence (Notes 5), and each from exactly its warm-up's bars."""
    m = minutes(tf)
    bar = timedelta(minutes=m)
    start = at - _span(max(indicators.warmup(i.rule, i.settings) for i in group), m)
    days = yahoo.RETENTION_DAYS.get(m)
    # Fetched no further back than Yahoo keeps, so `why` names no bars the warm-up does not
    # need; bars kept from an earlier fetch are still read, so a recompute reads the same ones.
    why = yahoo.ensure(bcon, symbol, m, start if days is None else
                       max(start, now - timedelta(days=days)), now, end=at)
    kept = [b for b in bars.read(bcon, yahoo.SOURCE, symbol, m, start, at)
            if b.open_time + bar <= at]
    covered = bars.covered(bcon, yahoo.SOURCE, symbol, m)
    keys = [k for i in group for k in i.keys]

    def unknown(reason: str) -> dict[str, checks.Figure]:
        return {k: checks.Figure(None, f"{reason}; supply {k}") for k in keys}
    if not kept:
        return unknown(f"no {tf} bars kept before {at.isoformat()}" + (f"; {why}" if why else ""))
    newest = kept[-1]
    c = newest.open_time + bar
    if not any(f <= c and to >= at for f, to in covered) and c + bar <= at:
        if instrument == "future":
            return unknown(f"the newest {tf} bar kept closed at {c.isoformat()}, a bar or more "
                           f"before {at.isoformat()}: the bar acted on is not in Yahoo's feed yet "
                           "(Yahoo delays futures 10 to 30 minutes)")
        if why:   # else a stock's or an ETF's empty stretch reads as the market shut
            return unknown(why)
    opened = newest.open_time.astimezone(UTC)
    says = f"computed on the {tf} bar that opened {opened:%Y-%m-%d %H:%M} UTC"
    figures = {}
    for i in group:
        n = indicators.warmup(i.rule, i.settings)
        used, key = kept[-n:], i.keys[0]
        value = None
        if gaps := yahoo._gaps(covered, used[0].open_time, c):
            reason = "bars are missing " + ", ".join(
                f"from {a.isoformat()} to {b.isoformat()}" for a, b in gaps)
        elif len(used) < n:
            reason = f"{len(used)} of the {n} {tf} bars this needs are kept" + (
                f", as Yahoo keeps {tf} bars for about {days} days" if days
                else f"; {why}" if why else "")
        elif (value := _value(i, used)) is None:
            reason = f"no close moved in the last {n} {tf} bars, so {key} has no value"
        figures[key] = (checks.Figure(value, says) if value is not None
                        else checks.Figure(None, f"{reason}; supply {key}"))
        figures.update({k: checks.Figure(newest.close, says) for k in i.keys[1:]})  # close_<tf>
    return figures


def _figures(con, run_row: sqlite3.Row, ticker: str, instrument: str, currency: str,
             rules: Rules, at: datetime, now: datetime) -> dict[str, checks.Figure]:
    """The desk's own figure, or why there is none, for every indicator key the rules read; facts
    are never computed. It always fetches, so it is never called under the write lock."""
    found = indicator_checks(rules.values, rules.timeframes)
    if run_row["mode"] == "replay":
        return {k: checks.Figure(None, f"a replay run keeps no bars: supply {k}, read off the "
                                       "chart at the bar acted on")
                for i in found for k in i.keys}
    figures: dict[str, checks.Figure] = {}
    for tf in dict.fromkeys(i.timeframe for i in found):
        group = [i for i in found if i.timeframe == tf]
        keys = [k for i in group for k in i.keys]
        if instrument == "future" and minutes(tf) in (1440, 10080):
            figures.update({k: checks.Figure(None, f"{_r6(tf)}; supply {k}") for k in keys})
        elif minutes(tf) not in yahoo.INTERVALS:
            figures.update({k: checks.Figure(None, f"Yahoo has no {tf} bars: supply {k}")
                            for k in keys})
        else:
            symbol = yahoo.ticker_symbol(ticker, instrument, currency)
            try:
                with closing(bars.open()) as bcon:
                    figures.update(_computed(bcon, symbol, instrument, tf, group, at, now))
            except Exception as e:  # bars.db locked, corrupt or foreign: a check never refuses
                figures.update({k: checks.Figure(None, f"bars could not be read: "
                                                       f"{type(e).__name__}: {e}; supply {k}")
                                for k in keys})
    return figures


def _market(rules: Rules, market) -> dict[str, float]:
    """The agent's values (design §7.5): only the keys the version's rules read, each a finite
    number, kept as floats. None is none."""
    if market is None:
        return {}
    if not isinstance(market, dict):
        raise Refused("market must be a mapping of market keys to numbers, e.g. "
                      f"{{'rsi_14_15m': 28.5}}, or left out; got {market!r}.")
    keys = market_keys(rules.values, rules.timeframes)
    for k, v in market.items():
        if not isinstance(k, str) or k not in keys:
            raise Refused(f"market key {k!r} is not one this version's rules read; " + (
                f"they read {', '.join(keys)}." if keys
                else "this version's rules read no market values, so leave market out."))
        if isinstance(v, bool) or not isinstance(v, int | float) or not _finite(v):
            raise Refused(f"market[{k!r}] must be a finite number, e.g. 28.5; got {v!r}.")
    return {k: float(v) for k, v in market.items()}


def _answers(items: tuple[str, ...], checklist) -> tuple[dict, ...]:
    """The agent's checklist answers (design §7.6), kept as given in the order given. None is
    none."""
    if checklist is None:
        return ()
    form = "{'item': …, 'verdict': 'pass' or 'fail', 'reason': …}"
    if not isinstance(checklist, list):
        raise Refused(f"checklist must be a list of answers, each {form}, or left out; got "
                      f"{checklist!r}.")
    seen = set()
    for a in checklist:
        if not isinstance(a, dict) or set(a) != {"item", "verdict", "reason"}:
            raise Refused(f"A checklist answer is {form}, with exactly those keys; got {a!r}.")
        if a["item"] not in items:
            listed = "; ".join(repr(i) for i in items) or "it has none"
            raise Refused(f"checklist item {a['item']!r} is not one of this version's items, "
                          f"which are matched by their exact text: {listed}.")
        if a["item"] in seen:
            raise Refused(f"checklist item {a['item']!r} has a second answer; give one.")
        seen.add(a["item"])
        if a["verdict"] not in ("pass", "fail"):
            raise Refused(f"checklist verdict {a['verdict']!r} is not known; use pass or fail.")
        if not isinstance(a["reason"], str) or not a["reason"].strip():
            raise Refused(f"checklist item {a['item']!r} has no reason; say why, in one line.")
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
    written; every fetch runs before the write lock."""
    now = _aware(now)
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
    if currency != row["currency"]:  # a run's currency never changes, so no lock is needed
        raise Refused(f"Run {run!r} is in {row['currency']}; this trade is in {currency}. A run "
                      f"is in one currency: enter it on a {currency} run, or give "
                      f"currency={row['currency']!r}.")
    if not _text("reasons", reasons).strip():
        raise Refused("reasons is empty; say why you are taking this trade, in your own words.")
    if check_only is not None and not isinstance(check_only, bool):
        raise Refused(f"check_only must be true, false or left out; got {check_only!r}.")
    rules, items = _version(con, row)
    market = _market(rules, market)
    answers = _answers(items, checklist)
    shots = [_screenshot(s) for s in _strings("screenshots", screenshots)]
    plan = model.Plan(symbol, side, kind, float(point_value), currency, float(entry),
                      stop and float(stop), target and float(target), size)
    _within_run(_run_view(con, row), when)   # before any fetch; again under the lock
    others = _others(con, row, now, fetch=True)   # the fetches, before the write lock
    # market data from insert-only bars, not the record: computed once, used again under the lock
    figures = _figures(con, row, symbol, kind, currency, rules, when, now)

    def shown(others: tuple[checks.Other, ...], seq: int | None) -> tuple[checks.Result, ...]:
        return tuple(checks.run_checks(plan, rules, checks.Context(
            at=when, seq=seq, currency=row["currency"], others=others, market=market,
            computed=figures)))
    if check_only:
        return CheckView(run=run, at=when, ticker=symbol, side=side, instrument=kind,
                         point_value=float(point_value), currency=currency,
                         checks=shown(others, None), checklist=_checklist_view(items, answers))
    with store.transaction(con):  # the run's state and the id's collisions are read under the lock
        _within_run(_run_view(con, row), when)
        results = shown(_others(con, row, now, fetch=False), len(store.trades(con, run)))
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
                              "screenshots": [s[0] for s in shots],
                              "checks": [asdict(r) for r in results], "market": market,
                              "checklist": list(answers)})
    return _trade_view(con, store.trade(con, id), now)


def _trade_row(con, trade: str) -> sqlite3.Row:
    row = store.trade(con, trade)
    if row is None:
        raise Refused(f"There is no trade {trade!r}; list_trades to see the ones that exist.")
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
    folded = model._fold(con, trade)
    if folded[0].voided:
        raise Refused(f"Trade {trade!r} is void, so it takes no further decision; if it should "
                      "count, enter it again with its original `at`.")
    return folded


def _after_entry(trade: str, d: model.Decisions, when: datetime) -> None:
    if when < d.entry.at:
        raise Refused(f"Trade {trade!r} was entered at {d.entry.at.isoformat()}; a decision on it "
                      "cannot be before that, so give a later `at`.")


def _the_close(d: model.Decisions) -> str:
    """The standing close as a refusal names it: how, when, and the price when the record holds
    one (a stop or target exit given no price: the level in force at the close)."""
    at, price = d.close_at.isoformat(), d.close_price
    if d.close_kind == "hand":
        how = f"closed by hand at {at}"
    else:
        how = f"closed at its {d.close_kind} at {at}"
        if price is None:
            price = model.in_force(d, d.close_at)[0 if d.close_kind == "stop" else 1]
    return how if price is None else f"{how}, at {price}"


def _before_close(trade: str, d: model.Decisions, when: datetime) -> None:
    """A move is strictly before the close: one at the close's `at` would take effect only
    from the bar after the exit."""
    if d.close_at is not None and when >= d.close_at:
        raise Refused(f"Trade {trade!r} was {_the_close(d)}; a move at or after that has no "
                      "order to move, so give an earlier `at`.")


def _not_closed(trade: str, d: model.Decisions) -> None:
    if d.close_at is not None:
        raise Refused(f"Trade {trade!r} was already closed at {d.close_at.isoformat()}: "
                      f"{d.close_reason!r}. A trade is closed once: to give that close the "
                      "broker's price, correct it (correct_trade with fields={'report': 'close', "
                      "'price': …}); if it was a mistake, correct it (fields={'report': 'close', "
                      "…}) or withdraw it (void_trade with report='close').")


def _close_order(trade: str, d: model.Decisions, when: datetime) -> None:
    """A close, by close_trade or an exit report, is strictly after the entry and every move."""
    _after_entry(trade, d, when)
    if when == d.entry.at:  # a close is strictly after the entry
        raise Refused(f"Trade {trade!r} was entered at {d.entry.at.isoformat()}; a close at "
                      "that same moment leaves it no time open, so give a later `at`.")
    if d.moves and when <= d.moves[-1].at:  # moves are in `at` order: the last is latest
        raise Refused(f"Trade {trade!r} has a move at {d.moves[-1].at.isoformat()}; a close "
                      "at or before it would leave that move after the close, so give a "
                      "later `at`.")


def _not_ended(trade: str, o: judge.Outcome, minutes: int, when: datetime, what: str) -> None:
    """Refuse a `what` ("move" or "close") at `when` that the judged end overtook: an exit in a bar
    that opened before `when` (Q31), or a cancellation at or before it. A reported end is in the
    record, where plan 2's checks and report_problem already order decisions against it."""
    if {"exit", "cancel"} & set(o.reported):
        return
    if o.status == "exited" and o.exited_at - timedelta(minutes=minutes) < when:
        out = o.exited_at.isoformat()
        if what == "move":
            fix = (f"If it is in fact still open at the broker, report it open at or after {out}, "
                   "the close of the bar that ended it (report(trade, state='open', at=…, "
                   "reason=…)); an open report inside that bar leaves its exit standing. The judge "
                   "then goes on from that report, so this move meets only an end the bars find "
                   "after it.")
        elif when > o.exited_at:
            fix = (f"If the trade was in fact still open then, report it open at or after {out} "
                   "and before this close (report(trade, state='open', at=…, reason=…)), then "
                   "close again; or report the exit the broker made, with its price "
                   "(report(trade, state='exited', price=…, at=…, reason=…)): a report is not "
                   "refused by the bars.")
        else:  # rule 12 puts an open report before the close; the restart needs it at or after out
            fix = (f"This close is not after the bar that exited the trade at {out}, so an open "
                   "report cannot set that exit aside (it would have to come before the close and "
                   "at or after the exit); report the exit the broker made, with its price "
                   "(report(trade, state='exited', price=…, at=…, reason=…)): a report is not "
                   "refused by the bars.")
        raise Refused(f"Trade {trade!r} already exited at {out}, at {o.exit} ({o.exit_reason}), in "
                      f"a bar that opened before the {what} at {when.isoformat()}, so the {what} "
                      f"has nothing to act on. {fix}")
    if o.status == "cancelled" and o.cancelled_at <= when:
        raise Refused(f"Trade {trade!r}'s limit order was cancelled at "
                      f"{o.cancelled_at.isoformat()}; there is no order to {what}.")


def _finest(con, row: sqlite3.Row) -> int:
    """The minutes of the trade's run's finest timeframe, the one it is judged on."""
    return next(iter(_timeframes(con, store.run(con, row["run_id"]))))


def _consistent(con, row: sqlite3.Row) -> None:
    """Refuse when the trade's reports, as the record now stands, do not fit it. Called after
    the append, inside the transaction, so a refusal rolls the append back."""
    run = store.run(con, row["run_id"])
    problem = model.report_problem(row["id"], model.decisions_of(con, row["id"]),
                                   side=row["side"], mode=run["mode"],
                                   stopped=_run_view(con, run).stopped)
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
    _outcomes(con, row, model.decisions_of(con, trade), now)  # the fetch, before the write lock
    with store.transaction(con):
        d = _not_void(con, trade)[0]
        _after_entry(trade, d, when)
        _before_close(trade, d, when)
        # judged without the move: it takes effect from the first bar opening at or after `at`
        _not_ended(trade, _outcomes(con, row, d, now, fetch=False)[0], _finest(con, row), when,
                   "move")
        moved ={k: float(v) for k, v in (("stop", stop), ("target", target)) if v is not None}
        store.append(con, kind="order_moved", run_id=row["run_id"], trade_id=trade, at=_at(when),
                     recorded_at=_at(now), payload={**moved, "reason": reason})
        _consistent(con, row)
    return _trade_view(con, row, now)


def close_trade(con: sqlite3.Connection, trade: str, *, reason: str, at: datetime | None = None,
                now: datetime | None = None) -> TradeView:
    """Close the trade by hand at `at`: the author's decision to get out. A trade is closed once.
    On a limit still pending it cancels the order. A live position is priced from the bars; a
    replay one has none, so its exit is reported with its price instead
    (report(state="exited", price=…))."""
    now = _aware(now)
    row = _decision(con, trade, reason)
    when = _decision_time(store.run(con, row["run_id"])["mode"], at, now, "close a trade")

    def closed(d: model.Decisions) -> model.Decisions:
        return replace(d, close_at=when, close_reason=reason, close_kind="hand", close_price=None)
    _outcomes(con, row, closed(model.decisions_of(con, trade)), now)  # the fetch, before the lock
    with store.transaction(con):
        d = _not_void(con, trade)[0]
        _not_closed(trade, d)
        _close_order(trade, d, when)
        # Judged with the close, which changes what counts in the last bar opening before `at`
        # (the author's ruling, Option A, 2026-11-20): a bar finished at `at` is judged in full,
        # one still forming at it counts only the stop. The close's own result is not refused.
        o = _outcomes(con, row, closed(d), now, fetch=False)[0]
        if o.exit_reason != "hand" and not (o.status == "cancelled" and o.cancelled_at == when):
            _not_ended(trade, o, _finest(con, row), when, "close")
        store.append(con, kind="closed", run_id=row["run_id"], trade_id=trade, at=_at(when),
                     recorded_at=_at(now), payload={"reason": reason})
        _consistent(con, row)
    return _trade_view(con, row, now)


_TAKES = {"filled": ("price",), "cancelled": ("why",), "open": (), "exited": ("price", "kind")}


def _kind(kind) -> str:
    if kind not in ("hand", "stop", "target"):
        raise Refused(f"kind {kind!r} is not known; use 'hand' (closed by hand), 'stop' (the stop "
                      "filled) or 'target' (the target filled), or leave it out for 'hand'.")
    return kind


def _why(why) -> str:
    if why not in ("not filled", "pulled"):
        raise Refused(f"why {why!r} is not known; use 'not filled' (the market never reached the "
                      "limit, or the order expired unfilled) or 'pulled' (the author withdrew "
                      "it).")
    return why


def _not_after_now(when: datetime, now: datetime) -> None:
    """A report states a state the trade was already in, live or replay."""
    if when > now:
        raise Refused(f"`at` {when.isoformat()} is after now ({now.isoformat()}); a report states "
                      "a state the trade was already in, so give the moment it was in it, in "
                      "UTC.")


def report(con: sqlite3.Connection, trade: str, *, state: str, at: datetime, reason: str,
           price: float | None = None, kind: str | None = None, why: str | None = None,
           now: datetime | None = None) -> TradeView:
    """Report the trade's state as of `at`, which is required in both modes:
    "filled" (the limit order filled; price None: at the limit), "cancelled" (it never will;
    why "not filled" or "pulled"), "open" (the position is still held) or "exited" (kind "stop",
    "target" or "hand", None meaning "hand"; a hand exit gives its price, and a stop or target
    exit given none is at the level in force). Checked against the record alone."""
    row = _decision(con, trade, reason)
    now = _aware(now)
    if state not in tuple(_TAKES):  # a tuple: a JSON list or dict compares, never hashes
        raise Refused(f"state {state!r} is not known; use 'filled' (the limit order filled), "
                      "'cancelled' (it never will), 'open' (the position is still held) or "
                      "'exited' (it closed at its stop, at its target or by hand).")
    if at is None:
        raise Refused("A report states the trade's state as of `at`, so it needs `at`, in live as "
                      "in replay: give the moment the order or the position was in that state, "
                      "not the moment you report it.")
    when = _aware(at, "at")
    _not_after_now(when, now)
    for name, value in (("price", price), ("kind", kind), ("why", why)):
        if value is not None and name not in _TAKES[state]:
            raise Refused(f"{name} does not go with state={state!r}: price goes with 'filled' and "
                          "'exited', kind with 'exited', why with 'cancelled'.")
    if state == "cancelled":
        _why(why)
    if state == "exited" and _kind("hand" if kind is None else kind) == "hand" and price is None:
        raise Refused("A hand exit is reported with the price it filled at; give price=…. To "
                      "have the bars price a live close instead, call close_trade.")
    _price("price", price, required=False)
    given = {"why": why, "kind": kind, "price": None if price is None else float(price)}
    payload = {"reason": reason, **{k: v for k, v in given.items() if v is not None}}
    with store.transaction(con):
        d = _not_void(con, trade)[0]
        for asked, noun, standing, word in (("filled", "fill", d.fill, "fill"),
                                            ("cancelled", "cancellation", d.cancel, "cancel")):
            if state == asked and standing is not None:
                raise Refused(f"Trade {trade!r}'s {noun} was already reported at "
                              f"{standing.at.isoformat()}; correct it (correct_trade with "
                              f"fields={{'report': '{word}', …}}) or withdraw it (void_trade "
                              f"with report='{word}').")
        if state == "exited":
            _not_closed(trade, d)
            _close_order(trade, d, when)
        store.append(con, kind={"open": "held", "exited": "closed"}.get(state, state),
                     run_id=row["run_id"], trade_id=trade, at=_at(when), recorded_at=_at(now),
                     payload=payload)
        _consistent(con, row)
    return _trade_view(con, row, now)


_ENTRY_FIELDS = ("entry", "stop", "target", "size", "at", "point_value", "currency")
_MOVE_FIELDS = ("stop", "target", "at")
# A report as correct_trade and void_trade name it (the record's words, since a `closed` event
# holds an exit report and close_trade's decision alike), and the fields a correction changes.
_REPORT_FIELDS = {"fill": ("price", "at"), "cancel": ("at", "why"), "open": ("at",),
                  "close": ("price", "at", "kind")}
_NOUNS = {"fill": "fill", "cancel": "cancellation", "open": "open report", "close": "close"}


def correct_trade(con: sqlite3.Connection, trade: str, *, fields: dict, reason: str,
                  now: datetime | None = None) -> TradeView:
    """Correct the entry, with `move: <index>` (its place in get_trade's moves) one move, or
    with `report: "fill" | "cancel" | "open" | "close"` one standing report ("open": the latest).
    Appended as a `corrected` event naming that event, read in its place; only the fields named
    change. A field given as None is not given, so a stop or target is never removed by a
    correction. A wrong run, ticker or side is not corrected either: void the trade and enter
    it again."""
    now = _aware(now)
    row = _decision(con, trade, reason)
    if not isinstance(fields, dict):
        raise Refused("fields must be a mapping of the fields to correct to their corrected "
                      f"values, e.g. {{'stop': 97.5}}; got {fields!r}.")
    asked, fields = fields, {k: v for k, v in fields.items() if v is not None}
    index, which = fields.pop("move", None), fields.pop("report", None)
    if index is not None and which is not None:
        raise Refused("fields names both a move and a report; correct one at a time.")
    if which is not None and which not in tuple(_REPORT_FIELDS):
        raise Refused(f"report {which!r} is not known; use 'fill', 'cancel', 'open' or 'close'.")
    if which is not None:
        names, noun = _REPORT_FIELDS[which], _NOUNS[which]
        on, redo = (f"{'an' if which == 'open' else 'a'} {noun}",
                    f"withdraw it (void_trade with report={which!r}) and report it again")
    else:
        names = _ENTRY_FIELDS if index is None else _MOVE_FIELDS
        on, redo = "an entry" if index is None else "a move", "void the trade and enter it again"
    if not fields:
        removal = (" A stop or target is not removed by a correction: void the trade and enter "
                   "it again without it." if {"stop", "target"} & asked.keys() else "")
        raise Refused("fields names nothing to correct (a field given as None is read as not "
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
    if "currency" in fields:
        _currency(fields["currency"])
        run_currency = store.run(con, row["run_id"])["currency"]
        if fields["currency"] != run_currency:
            raise Refused(f"Trade {trade!r} is in a {run_currency} run, so its currency stays "
                          f"{run_currency}. A trade in another currency belongs on another run: "
                          "void it and enter it there.")
    if "point_value" in fields and row["instrument"] != "future":
        raise Refused(f"Trade {trade!r} is a {row['instrument']}, whose point value is 1; only a "
                      "future's point value can be corrected.")
    when = _aware(fields["at"], "at") if "at" in fields else None
    if which is not None and when is not None:
        _not_after_now(when, now)

    def unmoved(d: model.Decisions) -> model.Decisions:  # as move_order judges, before its move
        return replace(d, moves=tuple(mv for i, mv in enumerate(d.moves) if i != index))
    if index is not None and when is not None:  # a move's `at` (Q32): the fetch, before the lock
        _outcomes(con, row, unmoved(model.decisions_of(con, trade)), now)
    with store.transaction(con):  # the decisions the correction is checked against, under the lock
        d, entered, move_ids, reports = _not_void(con, trade)
        if which is not None:
            if which not in reports:
                raise Refused(f"Trade {trade!r} has no {noun} to correct; get_trade shows its "
                              "reports.")
            if which == "close" and when is not None:
                _close_order(trade, d, when)
            corrects = reports[which]
        elif index is None:
            if fields.get("entry", d.entry.price) == fields.get("stop", d.entry.stop):
                raise Refused("The corrected stop equals the entry, so the risk (the distance to "
                              "the stop, 1R) is zero; put the stop away from the entry.")
            if when is not None:
                _within_run(_run_view(con, store.run(con, row["run_id"])), when)
                later = [m.at for m in d.moves]
                if d.close_at is not None and when >= d.close_at:  # an entry is before the close
                    raise Refused(f"Trade {trade!r} was {_the_close(d)}; its entry must be "
                                  "before that, so give an earlier `at`.")
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
                _not_ended(trade, _outcomes(con, row, unmoved(d), now, fetch=False)[0],
                           _finest(con, row), when, "move")
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
    With `report` ("fill", "cancel", "open" or "close"), withdraw that standing report instead
    ("open": the latest, leaving the one before it standing): it is read as absent, and the
    trade stands."""
    now = _aware(now)
    row = _decision(con, trade, reason)
    if report is not None and report not in tuple(_REPORT_FIELDS):
        raise Refused(f"report {report!r} is not known; use 'fill', 'cancel', 'open' or 'close', "
                      "or leave it out to void the whole trade.")
    with store.transaction(con):
        reports = _not_void(con, trade)[3]
        if report is not None and report not in reports:
            raise Refused(f"Trade {trade!r} has no {_NOUNS[report]} to withdraw; get_trade shows "
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
    others = _others(con, store.run(con, row["run_id"]), now, fetch=True, skip=row["id"])
    here = _results_now(con, row, d, others, now)
    return replace(view, checks_now=here, checks_differ=_differ(view.checks_shown, here))


def _open(v: TradeView) -> bool:
    """Not void, and still in the market or waiting to be: a trade not judged is, until the
    record ends it."""
    o, d = v.outcome, v.decisions
    return not d.voided and (o.status in ("pending", "open", "closing") or (
        o.status == "not judged" and d.close_at is None and d.cancel is None))


def list_trades(con: sqlite3.Connection, run: str | None = None, strategy: str | None = None,
                *, open: bool | None = None, now: datetime | None = None) -> list[TradeView]:
    """Every trade in the order it was recorded, voided ones included, narrowed to a run and a
    strategy when given, and to the open trades (open=True) or the rest (open=False)."""
    now = _aware(now)
    for name, value in (("run", run), ("strategy", strategy)):
        if value is not None:
            _text(name, value)
    if open is not None and not isinstance(open, bool):
        raise Refused(f"open must be true, false or left out; got {open!r}.")
    rows = [r for r in store.trades(con, run)  # narrowed first: a view may fetch bars
            if strategy in (None, store.run(con, r["run_id"])["strategy_id"])]
    return [v for v in (_trade_view(con, r, now) for r in rows) if open in (None, _open(v))]


@dataclass(frozen=True)
class TradeBars:
    trade: str
    timeframe: str
    bars: tuple[judge.Bar, ...]
    # the uncovered ranges between the entry and the end anchor
    missing: tuple[tuple[datetime, datetime | None], ...]
    notes: tuple[str, ...]   # why bars are still missing (no yfinance, a failed fetch), if known


def trade_bars(con: sqlite3.Connection, trade: str, *, timeframe: str | None = None,
               before: int | None = 0, after: int | None = 0,
               now: datetime | None = None) -> TradeBars:
    """A live trade's bars at one of its run's version's timeframes (default: the finest): from
    `before` bars ahead of the bar holding the entry through `after` bars past the last bar
    opening before the exit or cancellation, or through the latest bar while it has not ended.
    `missing`: what no fetch covered from the entry to its end, or to now while it has not ended;
    `notes`: why."""
    now = _aware(now)
    row = _trade_row(con, _text("trade", trade))
    run = store.run(con, row["run_id"])
    if run["mode"] == "replay":
        raise Refused(f"Trade {trade!r} is on replay run {row['run_id']!r}, which keeps no bars: "
                      "a replay trade is read from what is reported. trade_bars serves live "
                      "trades.")
    names = tuple(_timeframes(con, run).values())
    if timeframe is None:
        timeframe = names[0]
    elif timeframe not in names:
        raise Refused(f"timeframe {timeframe!r} is not one of run {row['run_id']!r}'s timeframes; "
                      f"use one of {', '.join(names)}, or leave it out for the finest.")
    m = minutes(timeframe)
    if m not in yahoo.INTERVALS:
        raise Refused(f"Live runs read Yahoo, which has no {timeframe} bars; ask for another of "
                      f"run {row['run_id']!r}'s timeframes ({', '.join(names)}) that Yahoo has: "
                      f"{_YAHOO_HAS}.")
    for name, v in (("before", before), ("after", after)):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
            raise Refused(f"{name} must be a whole number of bars, 0 or more, e.g. 3, or left "
                          f"out for 0; got {v!r}.")
    before, after = before or 0, after or 0
    d = model.decisions_of(con, row["id"])
    o = _outcomes(con, row, d, now)[0]   # the effective end; fetches only until the trade ends
    ended = o.exited_at or o.cancelled_at
    bar = timedelta(minutes=m)
    symbol = yahoo.ticker_symbol(row["ticker"], row["instrument"], d.currency)
    # shortcut: 4 days spans a long weekend; a longer market closure returns fewer than `before`
    # bars, so count back in sessions if that bites
    start = d.entry.at - (before + 1) * bar - timedelta(days=4)
    # an ended trade's window is fetched, never the time since
    end = None if ended is None else min(now, ended + (after + 1) * bar + timedelta(days=4))
    with closing(bars.open()) as bcon:
        why = yahoo.ensure(bcon, symbol, m, start, now, end=end)
        got = bars.read(bcon, yahoo.SOURCE, symbol, m, start, end)
        missing = yahoo._gaps(bars.covered(bcon, yahoo.SOURCE, symbol, m), d.entry.at,
                              ended or now)
    times = [b.open_time for b in got]
    first = max(bisect_right(times, d.entry.at) - 1 - before, 0)   # the entry anchor, less `before`
    last = len(got) if ended is None else bisect_left(times, ended) + after
    return TradeBars(trade, timeframe, tuple(got[first:last]), tuple(missing),
                     () if why is None else (why,))


_NO_SCOPE = ("stats needs one scope: a run, a strategy, or a strategy with a version; every "
             "strategy pooled would be account-level P&L, which the desk does not report")


def stats(con: sqlite3.Connection, *, strategy: str | None = None, version: int | None = None,
          run: str | None = None, mode: str | None = None, tags=None, conforming_only=None,
          now: datetime | None = None) -> Stats:
    """Counts, rates and averages over one scope's non-void trades, from their effective outcomes
    (design §8): a run, a strategy's version, or a strategy. Narrowed by mode, by tags (every one
    given held by the run, exactly) and, with conforming_only, to the trades whose results now are
    all kept. Never a verdict or a ranking; a trade that cannot be judged reads "not judged"."""
    now = _aware(now)
    for name, value in (("strategy", strategy), ("run", run)):
        if value is not None:
            _text(name, value)
    if version is not None and (isinstance(version, bool) or not isinstance(version, int)):
        raise Refused(f"version must be a whole number, e.g. 2, or left out; got {version!r}.")
    if mode not in (None, "live", "replay"):
        raise Refused(f"mode {mode!r} is not known; use 'live' or 'replay', or leave it out.")
    tags = _strings("tags", tags)
    if conforming_only is not None and not isinstance(conforming_only, bool):
        raise Refused(f"conforming_only must be true, false or left out; got {conforming_only!r}.")
    if run is not None:
        if strategy is not None or version is not None:
            raise Refused("Give one scope: a run alone, a strategy, or a strategy with a version; "
                          "a run already names its strategy and its version.")
        row = store.run(con, run)
        if row is None:
            raise Refused(f"There is no run {run!r}; list_runs to see the ones that exist.")
        runs, strategy, version = [row], row["strategy_id"], row["version"]
    elif strategy is None:
        raise Refused(_NO_SCOPE if version is None else "A version is named with its strategy: "
                      "give strategy=… beside version=…, or give a run.")
    else:
        _view(con, strategy, version, "unchanged")   # refuses an unknown strategy or version
        runs = [r for r in store.runs(con, strategy) if version in (None, r["version"])]
    scope = [(r, held) for r in runs if mode in (None, r["mode"])
             and set(tags) <= set(held := tuple(json.loads(r["tags"])))]
    # Narrowed before any trade is read; a void is left out before its bars are fetched.
    read = [(r, held, t, _trade_view(con, t, now), seq) for r, held in scope
            for seq, t in enumerate(store.trades(con, r["id"]))
            if not model.decisions_of(con, t["id"]).voided]
    left_out: dict = {}
    if conforming_only:
        others: dict[str, list[checks.Other]] = {}   # each run's, built once (B6)
        for r, _, t, v, seq in read:
            others.setdefault(r["id"], []).append(_other(
                t, v.decisions, v.outcome, seq, live=r["mode"] == "live",
                minutes=next(iter(_timeframes(con, r)))))
        breached, unknown, kept = [], {}, []
        for r, held, t, v, seq in read:
            results = _results_now(con, t, v.decisions,
                                   tuple(o for o in others[r["id"]] if o.id != v.id), now)
            if any(x.ok is False for x in results):
                breached.append(v.id)
            elif all(x.ok is True for x in results):   # no rules: kept
                kept.append((r, held, t, v, seq))
            else:
                for x in results:
                    if x.ok is None:
                        unknown.setdefault(x.name, []).append(v.id)
        read = kept
        left_out = {"breached": tuple(breached),
                    "unknown": {name: tuple(ids) for name, ids in unknown.items()}}
    rows = [Row(id=v.id, run=v.run, version=v.version, mode=r["mode"], tags=held,
                entry_type=v.decisions.entry.type, currency=v.currency, outcome=v.outcome,
                planned_rr=checks.reward_risk(_plan(t, v.decisions))[0],
                settles=_settles(v.outcome))
            for r, held, t, v, _ in read]
    return summarise(rows, strategy=strategy, version=version, run=run, mode=mode, tags=tags,
                     conforming_only=bool(conforming_only), left_out=left_out)
