"""The desk's one outward face: what desk/server.py serves as MCP tools, function for function.

Every function takes an open connection and `now` (default: the wall clock, in UTC) so tests
control time. Every refusal is a `Refused` whose message says what to change.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import asdict, replace
from datetime import datetime

from desk import checks, config, inputs, model, store, strategy, views
from desk.errors import Refused, shown
from desk.stats import Row, Stats, summarise  # `stats` below shadows the module's name
from desk.views import CheckView, RunView, StoppedRun, StrategyView, TradeView

__all__ = ["Refused", "StrategyView", "save_strategy", "get_strategy", "list_strategies",
           "RunView", "start_run", "stop_run", "list_runs", "TradeView", "CheckView", "trade",
           "move_order", "report", "correct_trade", "void_trade", "get_trade",
           "list_trades", "Stats", "stats", "StoppedRun"]

_SLUG = re.compile(r"[a-z0-9-]{1,64}")  # with fullmatch; the schema's CHECK on strategies.id


def save_strategy(con: sqlite3.Connection, id: str, markdown: str, *,
                  if_current: str | None = None, now: datetime | None = None) -> StrategyView:
    """Save a strategy (new id: version 1; existing id: needs `if_current`, the `current_hash`
    last read). Text that changes after whitespace is normalised makes a new version."""
    if if_current is not None and not isinstance(if_current, str):
        raise Refused("if_current must be text: the current_hash get_strategy returned; got "
                      f"{shown(if_current)}.")
    if not _SLUG.fullmatch(inputs.text("id", id)):
        raise Refused(f"Strategy id {shown(id)} must be 1 to 64 lowercase letters, digits and "
                      "hyphens, like 'dip-buyer'.")
    parsed = strategy.parse(inputs.text("markdown", markdown))
    at = inputs.at(now)
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
    saved = f"version {len(rows) + 1} created" if new_text else "unchanged"
    return views.strategy_view(con, id, None, saved)


def get_strategy(con: sqlite3.Connection, id: str, version: int | None = None) -> StrategyView:
    inputs.text("id", id)
    return views.strategy_view(con, id, inputs.version_number(version), "unchanged")


def list_strategies(con: sqlite3.Connection) -> list[StrategyView]:
    return [views.strategy_view(con, r["id"], None, "unchanged")
            for r in con.execute("SELECT id FROM strategies ORDER BY id")]


def start_run(con: sqlite3.Connection, strategy: str, *, mode: str, currency=None, tags=(),
              version: int | None = None, at: datetime | None = None,
              now: datetime | None = None) -> RunView:
    """Deploy a strategy version as run `<strategy>-r<N>`. Tags are kept verbatim (deduplicated,
    order kept) and never interpreted. The run trades in one currency, CAD or USD."""
    inputs.text("strategy", strategy)
    inputs.version_number(version)
    inputs.currency(currency)
    tags = inputs.strings("tags", tags)
    if mode not in ("live", "replay"):
        raise Refused(f"mode {shown(mode)} is not known; use \"live\" (real time) or \"replay\" "
                      "(re-running past history).")
    now = inputs.aware(now)
    start = inputs.decision_time(mode, at, now, "start")
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
        store.append(con, kind="run_started", run_id=id, at=inputs.at(start), payload={},
                     recorded_at=inputs.at(now))
    return views.run_view(con, store.run(con, id))


def stop_run(con: sqlite3.Connection, run: str, *, at: datetime | None = None,
             now: datetime | None = None) -> StoppedRun:
    """Stop the run at `at`: not before its start. Every non-void trade of the run is then
    checked against the stop, which cancels its orders still pending and leaves no room for an
    entry at or after it. The reply is a StoppedRun: its `cancelled` holds the run's unfilled
    limit orders the stop cancelled, in recording order."""
    row = store.run(con, inputs.text("run", run))
    if row is None:
        raise Refused(f"There is no run {shown(run)}; list_runs to see the ones that exist.")
    now = inputs.aware(now)
    stop = inputs.decision_time(row["mode"], at, now, "stop")
    with store.transaction(con):  # the run's state is checked under the write lock
        view = views.run_view(con, row)
        if view.stopped is not None:
            raise Refused(f"Run {shown(run)} is already stopped; start a new run instead.")
        if stop < view.started:
            raise Refused(f"Run {shown(run)} started at {view.started.isoformat()}; its stop "
                          f"cannot "
                          "be before that, so give a later `at`.")
        standing = [(t, d) for t in store.trades(con, run)
                    if not (d := model.decisions_of(con, t["id"])).voided]
        store.append(con, kind="run_stopped", run_id=run, at=inputs.at(stop), payload={},
                     recorded_at=inputs.at(now))
        for t, _ in standing:
            _consistent(con, t)
    cancelled = [t["id"] for t, d in standing   # read after the transaction
                 if d.entry.type == "limit"
                 and not (d.exited is not None and d.exited.at < stop)
                 and (o := views.outcome_of(con, t, d)).fill is None
                 and o.status == "cancelled" and o.cancelled_why == "run stopped"]
    return StoppedRun(**asdict(views.run_view(con, row)), cancelled=tuple(cancelled))


def list_runs(con: sqlite3.Connection, strategy: str | None = None) -> list[RunView]:
    if strategy is not None:
        inputs.text("strategy", strategy)
    return [views.run_view(con, r) for r in store.runs(con, strategy)]


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


# ------------------------------------------------- the checks, as of a trade's `at` ---


def trade(con: sqlite3.Connection, run: str, *, ticker: str, side: str, entry_type: str,
          entry: float, size: int, reasons: str, stop: float | None = None,
          target: float | None = None, screenshots=(), at: datetime | None = None,
          instrument: str | None = None, point_value: float | None = None, market=None,
          checklist=None, check_only=None, now: datetime | None = None) -> TradeView | CheckView:
    """Enter a trade on a run: one `trades` row and one `entered` event, written together, with
    the results of the run's version's rules as of `at`, as shown: a breach never refuses a
    trade. `market` holds the agent's values for the keys the rules read, `checklist` its
    answers. With check_only, validate and check exactly as an entry, write nothing, and return
    a CheckView. Everything is validated, and every screenshot file read, before anything is
    written."""
    now = inputs.aware(now)
    row = store.run(con, inputs.text("run", run))
    if row is None:
        raise Refused(f"There is no run {shown(run)}; list_runs to see the ones that exist.")
    when = inputs.decision_time(row["mode"], at, now, "enter a trade")
    if side not in ("long", "short"):
        raise Refused(f"side {shown(side)} is not known; use \"long\" (profit if the price rises) "
                      f"or "
                      "\"short\" (profit if it falls).")
    if entry_type not in ("market", "limit"):
        raise Refused(f"entry_type {shown(entry_type)} is not known; use \"market\" (fill at once "
                      f"at the "
                      "going price) or \"limit\" (fill only at your price or better).")
    symbol, implied = model.canonical_ticker(inputs.text("ticker", ticker))
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
        inputs.price("point_value", point_value)
    elif point_value is not None:  # dropping it would understate the risk for good
        raise Refused(f"point_value is only for a future and {shown(ticker)} reads as "
                      f"{'an' if kind == 'etf' else 'a'} {kind}; pass instrument=\"future\" (or a "
                      f"continuous ticker such as \"GC1!\"), or leave point_value out.")
    else:
        point_value = 1.0
    inputs.price("entry", entry)
    inputs.price("stop", stop, required=False)
    inputs.price("target", target, required=False)
    if stop == entry:
        raise Refused("stop equals entry, so the risk (the distance to the stop, 1R) is zero and "
                      "no reward-to-risk can be measured; put the stop away from the entry.")
    inputs.right_side(side, entry, stop, target)
    inputs.size(size)
    inputs.text("reasons", reasons)
    if check_only is not None and not isinstance(check_only, bool):
        raise Refused(f"check_only must be true, false or left out; got {shown(check_only)}.")
    rules, items = views.version_rules(con, row)
    market = inputs.market(rules, market)
    answers = inputs.answers(items, checklist)
    shots = [inputs.screenshot(s) for s in inputs.strings("screenshots", screenshots)]
    plan = model.Plan(symbol, side, kind, float(point_value), float(entry),
                      stop and float(stop), target and float(target), size)
    record = bool(set(checks.RECORD) & set(rules.values))   # else no rule reads another trade

    def as_shown(others: tuple[checks.Other, ...], seq: int | None) -> tuple[checks.Result, ...]:
        return tuple(checks.run_checks(plan, rules, checks.Context(
            at=when, seq=seq, currency=row["currency"], others=others, market=market)))
    if check_only:   # a preview writes nothing, so it checks without the write lock
        _within_run(views.run_view(con, row), when)
        others = views.others(con, row) if record else ()
        return CheckView(run=run, at=when, ticker=symbol, side=side, instrument=kind,
                         point_value=float(point_value), currency=row["currency"],
                         checks=as_shown(others, None),
                         checklist=views.checklist_view(items, answers))
    if not reasons.strip():
        raise Refused("reasons is empty; say why you are taking this trade, in your own words.")
    with store.transaction(con):  # the run's state and the id's collisions are read under the lock
        _within_run(views.run_view(con, row), when)
        results = as_shown(views.others(con, row) if record else (), len(store.trades(con, run)))
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
        store.add_trade(con, id, run, symbol, side, kind, float(point_value))
        store.append(con, kind="entered", run_id=run, trade_id=id, at=inputs.at(when),
                     recorded_at=inputs.at(now),
                     payload={"entry_type": entry_type, "entry": float(entry),
                              "stop": stop and float(stop), "target": target and float(target),
                              "size": size, "reasons": reasons,
                              "screenshots": [s[0] for s in shots],
                              "checks": [asdict(r) for r in results], "market": market,
                              "checklist": list(answers)})
        _consistent(con, store.trade(con, id))
    return views.trade_view(con, store.trade(con, id), now)


def _trade_row(con, trade: str) -> sqlite3.Row:
    row = store.trade(con, trade)
    if row is None:
        raise Refused(f"There is no trade {shown(trade)}; list_trades to see the ones that exist.")
    return row


def _decision(con, trade: str, reason) -> sqlite3.Row:
    """The trade's identity row, once the id is text, the decision's reason is given and the
    trade exists."""
    inputs.text("trade", trade)
    if not inputs.text("reason", reason).strip():
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
    run = views.run_view(con, store.run(con, row["run_id"]))
    problem = model.record_problem(row["id"], model.decisions_of(con, row["id"]),
                                   side=row["side"], started=run.started, stopped=run.stopped)
    if problem:
        raise Refused(problem)


def move_order(con: sqlite3.Connection, trade: str, *, reason: str, stop: float | None = None,
               target: float | None = None, at: datetime | None = None,
               now: datetime | None = None) -> TradeView:
    """Move the trade's stop, its target or both, from `at` on."""
    now = inputs.aware(now)
    row = _decision(con, trade, reason)
    when = inputs.decision_time(store.run(con, row["run_id"])["mode"], at, now, "move an order")
    if stop is None and target is None:
        raise Refused("A move names a new stop, a new target, or both; give at least one.")
    inputs.price("stop", stop, required=False)
    inputs.price("target", target, required=False)
    with store.transaction(con):
        _not_void(con, trade)
        moved = {k: float(v) for k, v in (("stop", stop), ("target", target)) if v is not None}
        store.append(con, kind="order_moved", run_id=row["run_id"], trade_id=trade,
                     at=inputs.at(when), recorded_at=inputs.at(now),
                     payload={**moved, "reason": reason})
        _consistent(con, row)
    return views.trade_view(con, row, now)


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
    now = inputs.aware(now)
    if state not in model.REPORTS:  # a tuple: a JSON list or dict compares, never hashes
        raise Refused(f"state {shown(state)} is not known; use \"filled\" (the limit order "
                      f"filled), "
                      "\"cancelled\" (it never will), \"open\" (the position is still open) or "
                      "\"exited\" (the position is out, at its stop, at its target or by hand).")
    if at is None:
        raise Refused("A report states the trade's state as of `at`, so it needs `at`, in live as "
                      "in replay: give the moment the order or the position was in that state, "
                      "not the moment you report it.")
    when = inputs.aware(at, "at")
    inputs.not_future(when, now, inputs.REPORT_STATE)
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
    inputs.price("price", price, required=False)
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
        store.append(con, kind=state, run_id=row["run_id"], trade_id=trade, at=inputs.at(when),
                     recorded_at=inputs.at(now), payload=payload)
        _consistent(con, row)
    return views.trade_view(con, row, now)


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
    now = inputs.aware(now)
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
            inputs.price(name, fields[name])
    for name in ("stop", "target", "price"):
        inputs.price(name, fields.get(name), required=False)
    if "kind" in fields:
        _kind(fields["kind"])
    if "why" in fields:
        _why(fields["why"])
    if "size" in fields:
        inputs.size(fields["size"])
    if "point_value" in fields and row["instrument"] != "future":
        raise Refused(f"Trade {shown(trade)} is a {row['instrument']}, whose point value is 1; "
                      f"only a "
                      "future's point value can be corrected.")
    when = inputs.aware(fields["at"], "at") if "at" in fields else None
    if when is not None:
        if which is not None:
            inputs.not_future(when, now, inputs.REPORT_STATE)
        else:
            noun = "a move" if index is not None else "an entry"
            inputs.not_future(when, now, f"{noun} {inputs.WHEN_IT_HAPPENED}")

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
            inputs.right_side(row["side"], fields.get("entry", d.entry.price),
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
            stored["at"] = inputs.at(when)
        store.append(con, kind="corrected", run_id=row["run_id"], trade_id=trade, at=inputs.at(now),
                     recorded_at=inputs.at(now), corrects=corrects,
                     payload={"fields": stored, "reason": reason})
        _consistent(con, row)
    return views.trade_view(con, row, now)


def void_trade(con: sqlite3.Connection, trade: str, *, reason: str, report: str | None = None,
               now: datetime | None = None) -> TradeView:
    """Void a trade: it stays in the log; statistics and the checks of other trades leave it out.
    With `report` ("filled", "cancelled", "open" or "exited"), withdraw that standing report
    ("open": the latest, leaving the one before it standing): it is read as absent, and the
    trade stands."""
    now = inputs.aware(now)
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
        corrects = None if report is None else reports[report]
        store.append(con, kind="voided", run_id=row["run_id"], trade_id=trade, at=inputs.at(now),
                     recorded_at=inputs.at(now), corrects=corrects,
                     payload={"reason": reason})
        if report is not None:
            _consistent(con, row)
    return views.trade_view(con, row, now)


def get_trade(con: sqlite3.Connection, trade: str, *, now: datetime | None = None) -> TradeView:
    now = inputs.aware(now)
    row = _trade_row(con, inputs.text("trade", trade))
    view = views.trade_view(con, row, now)
    d = view.decisions
    if d.voided:
        return view
    run = store.run(con, row["run_id"])
    others = (views.others(con, run, skip=row["id"])
              if set(checks.RECORD) & set(views.version_rules(con, run)[0].values) else ())
    here = views.results_now(con, row, d, others)
    return replace(view, checks_now=here, checks_differ=views.differ(view.checks_shown, here))


def _open(v: TradeView) -> bool:
    """Not void, and still in the market or waiting to be."""
    return not v.decisions.voided and v.outcome.status in ("pending", "open")


def list_trades(con: sqlite3.Connection, run: str | None = None, strategy: str | None = None,
                *, open: bool | None = None, now: datetime | None = None) -> list[TradeView]:
    """Every trade in the order it was recorded, voided ones included, narrowed to a run and a
    strategy when given, and to the open trades (open=True) or the rest (open=False)."""
    now = inputs.aware(now)
    for name, value in (("run", run), ("strategy", strategy)):
        if value is not None:
            inputs.text(name, value)
    if open is not None and not isinstance(open, bool):
        raise Refused(f"open must be true, false or left out; got {shown(open)}.")
    rows = [r for r in store.trades(con, run)
            if strategy in (None, store.run(con, r["run_id"])["strategy_id"])]
    return [v for v in (views.trade_view(con, r, now) for r in rows) if open in (None, _open(v))]


_NO_SCOPE = ("stats needs one scope: a run, a strategy, or a strategy with a version; every "
             "strategy pooled would be account-level P&L, which the desk does not report")


def stats(con: sqlite3.Connection, *, strategy: str | None = None, version: int | None = None,
          run: str | None = None, mode: str | None = None, tags=None, conforming_only=None,
          now: datetime | None = None) -> Stats:
    """Counts, rates and averages over one scope's non-void trades, from their outcomes: a run, a
    strategy's version, or a strategy. Narrowed by mode, by tags (every one given also on the run,
    exactly) and, with conforming_only, to the trades whose results now are all kept. Never a
    verdict or a ranking."""
    now = inputs.aware(now)
    for name, value in (("strategy", strategy), ("run", run)):
        if value is not None:
            inputs.text(name, value)
    inputs.version_number(version)
    if mode not in (None, "live", "replay"):
        raise Refused(f"mode {shown(mode)} is not known; use \"live\" or \"replay\", or leave it "
                      f"out.")
    tags = inputs.strings("tags", tags)
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
        # refuses an unknown strategy or version
        views.strategy_view(con, strategy, version, "unchanged")
        runs = [r for r in store.runs(con, strategy) if version in (None, r["version"])]
    scope = [(r, run_tags) for r in runs if mode in (None, r["mode"])
             and set(tags) <= set(run_tags := tuple(json.loads(r["tags"])))]
    # Narrowed before any trade is read; a void is left out.
    read = [(r, run_tags, t, views.trade_view(con, t, now), seq) for r, run_tags in scope
            for seq, t in enumerate(store.trades(con, r["id"]))
            if not model.decisions_of(con, t["id"]).voided]
    left_out: dict = {}
    if conforming_only:
        others: dict[str, list[checks.Other]] = {}   # each run's, built once
        for r, _, t, v, seq in read:
            others.setdefault(r["id"], []).append(views.other_of(t, v.decisions, v.outcome, seq))
        breached, unknown, kept = [], {}, []
        for r, run_tags, t, v, seq in read:
            results = views.results_now(con, t, v.decisions,
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
                planned_rr=checks.reward_risk(views.plan_of(t, v.decisions))[0])
            for r, run_tags, t, v, _ in read]
    return summarise(rows, strategy=strategy, version=version, run=run, mode=mode, tags=tags,
                     conforming_only=bool(conforming_only), left_out=left_out)
