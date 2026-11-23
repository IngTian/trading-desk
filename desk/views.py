"""What each reply is built from: rows into views, after any transaction, so nothing is
read under desk.db's write lock. A view is a reply the desk hands back; desk/api.py's
public functions build them.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from desk import checks, model, outcome, store, strategy
from desk.errors import Refused, shown
from desk.rules import Rules, finite, market_keys


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


def strategy_view(con, id: str, version: int | None, saved: str) -> StrategyView:
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


def run_view(con, row: sqlite3.Row) -> RunView:
    when = {e["kind"]: datetime.fromisoformat(e["at"])
            for e in store.events(con, run_id=row["id"]) if e["kind"].startswith("run_")}
    return RunView(id=row["id"], strategy=row["strategy_id"], version=row["version"],
                   mode=row["mode"], tags=tuple(json.loads(row["tags"])),
                   started=when["run_started"], stopped=when.get("run_stopped"),
                   currency=row["currency"])


@dataclass(frozen=True)
class StoppedRun(RunView):
    """What stop_run returns: the run, and `cancelled`, its non-void limit orders with no fill
    whose outcome reads cancelled, why "run stopped", in recording order. An order cancelled for
    another why, and an order exited, are left out."""
    cancelled: tuple[str, ...] = ()


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


def version_rules(con, run_row: sqlite3.Row) -> tuple[Rules, tuple[str, ...]]:
    """The run's version's rules and checklist items: never the strategy's latest."""
    v = next(r for r in store.versions(con, run_row["strategy_id"])
             if r["version"] == run_row["version"])
    return (Rules(json.loads(v["rules"]), tuple(json.loads(v["timeframes"]))),
            tuple(json.loads(v["checklist"])))


def outcome_of(con, row: sqlite3.Row, d: model.Decisions) -> outcome.Outcome:
    """The trade's outcome, read from its record alone, on a live run as on a replay one."""
    return outcome.of(d, side=row["side"], stopped=run_view(con, store.run(
        con, row["run_id"])).stopped)


def trade_view(con, row: sqlite3.Row, now: datetime) -> TradeView:
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
                     currency=run["currency"], decisions=d, outcome=outcome_of(con, row, d),
                     checks_shown=kept, checklist=checklist_view(version_rules(con, run)[1],
                                                                   d.checklist))


def other_of(row: sqlite3.Row, d: model.Decisions, o: outcome.Outcome, seq: int) -> checks.Other:
    """Another trade of the run as the checks read it, from its outcome. An exit always carries
    its cash (outcome._money gives one), which the record rules add up."""
    return checks.Other(id=row["id"], ticker=row["ticker"], entered=d.entry.at, seq=seq,
                        ended=o.exited_at or o.cancelled_at, exited=o.status == "exited",
                        r=o.r, cash=o.cash)


def others(con, run_row: sqlite3.Row, skip: str | None = None) -> tuple[checks.Other, ...]:
    """The run's non-void trades but `skip`, each from its outcome (the checks read them as of
    `at` from those dates). `seq` is the index in the unfiltered store.trades, so leaving voids
    out never renumbers one."""
    out = []
    for seq, t in enumerate(store.trades(con, run_row["id"])):
        if t["id"] == skip:
            continue
        d = model.decisions_of(con, t["id"])
        if not d.voided:
            out.append(other_of(t, d, outcome_of(con, t, d), seq))
    return tuple(out)


def plan_of(row: sqlite3.Row, d: model.Decisions) -> model.Plan:
    """The trade's plan as recorded: the identity row and the entry as corrected."""
    e = d.entry
    return model.Plan(row["ticker"], row["side"], row["instrument"], d.point_value,
                      e.price, e.stop, e.target, e.size)


def results_now(con, row: sqlite3.Row, d: model.Decisions, others: tuple[checks.Other, ...]
                 ) -> tuple[checks.Result, ...]:
    """The run's version's rules on the record as it stands, as of the entry's `at`. `others` never
    holds this trade."""
    run = store.run(con, row["run_id"])
    rules = version_rules(con, run)[0]
    seq = next(i for i, t in enumerate(store.trades(con, run["id"])) if t["id"] == row["id"])
    # a damaged stored value reads not supplied (integrity reports it), as a checklist does
    market = {k: v for k, v in (d.market.items() if isinstance(d.market, dict) else ())
              if isinstance(v, int | float) and not isinstance(v, bool) and finite(v)}
    return tuple(checks.run_checks(plan_of(row, d), rules, checks.Context(
        at=d.entry.at, seq=seq, currency=run["currency"], others=others, market=market)))


def differ(shown: tuple[checks.Result, ...] | None, now: tuple[checks.Result, ...]
            ) -> tuple[str, ...] | None:
    """The names whose (ok, figure) differ between the results shown and now, or are in one only;
    None when no results were shown to compare."""
    if shown is None:
        return None
    then = {r.name: (r.ok, r.figure) for r in shown}
    here = {r.name: (r.ok, r.figure) for r in now}
    return tuple(dict.fromkeys(n for n in [*here, *then]
                               if n not in then or n not in here or then[n] != here[n]))


def checklist_view(items: tuple[str, ...], answers) -> tuple[dict, ...]:
    """One answer per item of the version, in its order. An unanswered item reads "not answered",
    as does one whose stored answer trade() would not write (integrity's entered_checks reports
    that one), so a damaged record never fails a read."""
    given = {a["item"]: a for a in (answers if isinstance(answers, tuple | list) else ())
             if model.is_answer(a)}
    return tuple(given.get(i, {"item": i, "verdict": "not answered", "reason": None})
                 for i in items)
