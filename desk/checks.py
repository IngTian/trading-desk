"""The number rules, computed in code.

The owner types a planned trade and this module computes every number rule before they click.
Two rules govern the whole file:

**The AI never does a number rule's arithmetic.** Every figure below is computed here, in
Python. A rule name with no function in `_CHECKS` is reported as one that gets asked each
time -- never estimated. That is what `CHECK_NAMES` is for: `RulesFile.for_scope` is handed
this set, and any name outside it lands in `Rules.unchecked` and comes back through
`run_checks` as an `ok=None` Result carrying `ASKED`.

**A result is a fact, never a block and never a grade.** `ok` says whether the figure sits
inside the limit; nothing here can stop a click. If the owner takes the trade anyway, the
record says *taken against*. No scores, no streaks, no "process was good".

## Withholding

`ok=None` means *I could not tell*, and `says` names the input that was missing. A check that
silently passed for want of data would be the worst outcome this tool can produce, so every
guard below withholds rather than defaults: no 1:1 fallback for a missing exchange rate, no
zero for an unknown daily total, and no figure at all derived from a negative size.

## Two shapes of context the brief named but v1 has no type for yet

`Context.session_started` is the session's start time rather than a `Session` object: no
Session type or store accessor exists yet, and the only thing a check needs from a session is
when it began. `Context.stopped_closes` maps ticker -> the close time of the most recent trade
on that ticker that exited at or beyond its stop, and is built by `model.stopped_closes(con)`
rather than here, because deciding what counts as a stop is a fold over the log. `None` means
it was not supplied (so the cooldown withholds) and `{}` means the log was read and holds no
stop (so it passes).
"""
from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from desk import fmt
from desk.model import Plan, TradeState, _signed
from desk.rules import Rules

ASKED = "no automatic check for this one; I'll ask you each time"


@dataclass(frozen=True)
class Result:
    """One rule's result: the figure and the limit, stated. Never a verdict on the trader."""
    name: str
    ok: bool | None      # None = could not be computed
    figure: float | None
    limit: str           # the rule's value, verbatim from rules.md
    says: str            # the fact, e.g. "risk 140.00 CAD against a limit of 100 CAD"
    unit: str | None


def no_fx(src: str, dst: str) -> float | None:
    """The default rate source: 1.0 within a currency and nothing across one.

    Returning 1.0 for a cross-currency pair would compare a risk to a limit in another
    currency and call it a pass, so the default refuses instead.
    """
    return 1.0 if src == dst else None


@dataclass(frozen=True)
class Context:
    """Everything a check needs that is not in the plan or the rules."""
    now: dt.datetime
    open_trades: list[TradeState] = field(default_factory=list)
    session_started: dt.datetime | None = None
    closed_today_r: float | None = None
    median_candle_range: float | None = None
    volatility_ratio: float | None = None
    stopped_closes: dict[str, dt.datetime] | None = None
    # fx(src, dst) -> the rate that multiplies an amount in src to give dst, or None if unknown.
    fx: Callable[[str, str], float | None] = no_fx


# ------------------------------------------------------------------ parsing ---
# Rules.numbers values are RAW strings out of rules.md ("100 CAD", "2", "1 day, same ticker"),
# so every check parses its own. A value that cannot be read is withheld, not guessed at.
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_CURRENCY = re.compile(r"\b([A-Za-z]{3})\b")
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*([A-Za-z]+)")
_HOURS = {
    "m": 1 / 60, "min": 1 / 60, "mins": 1 / 60, "minute": 1 / 60, "minutes": 1 / 60,
    "h": 1.0, "hr": 1.0, "hrs": 1.0, "hour": 1.0, "hours": 1.0,
    "d": 24.0, "day": 24.0, "days": 24.0,
    "w": 168.0, "week": 168.0, "weeks": 168.0,
}
_YES = {"yes", "y", "true", "1", "required", "on"}
# A value in neither vocabulary is withheld, never read as a no: `stop_required: ture` must not
# be the thing that turns the stop requirement off. In this codebase a typo has twice failed
# open, and this is the check whose whole job is to notice a missing stop.
_NO = {"no", "n", "false", "0", "off", "optional"}


def _number(value: str) -> float | None:
    """The first number in a rule value: "2" -> 2.0, "100 CAD" -> 100.0, "-2R" -> -2.0."""
    found = _NUMBER.search(value)
    return float(found.group()) if found else None


def _currency(value: str) -> str | None:
    """The three-letter code in a rule value: "100 CAD" -> "CAD". None if it names none."""
    found = _CURRENCY.search(value)
    return found.group(1).upper() if found else None


def _hours(value: str) -> float | None:
    """A duration in hours: "2h" -> 2.0, "90 min" -> 1.5, "1 day, same ticker" -> 24.0."""
    found = _DURATION.search(value)
    if not found or found.group(2).lower() not in _HOURS:
        return None
    return float(found.group(1)) * _HOURS[found.group(2).lower()]


def _yes(value: str) -> bool | None:
    """True, False, or None when the value reads as neither -- which is not a no."""
    word = value.strip().lower()
    return True if word in _YES else False if word in _NO else None


def _withheld(name: str, value: str, says: str, unit: str | None = None) -> Result:
    """A figure that could not be computed, with the missing input named."""
    return Result(name, None, None, value, says, unit)


def _at(figure: float, places: int = 2) -> float:
    """The figure as `says` states it, for the comparison that sets `ok`.

    `ok` MUST NOT CONTRADICT THE DIGITS BESIDE IT. A plain 2:1 trade on a cent grid --
    entry 90.00, stop 89.96, target 90.08 -- divides to 1.9999999999996447, so a raw
    `>= 2` reports a breach under a sentence reading "reward to risk 2.00 against a
    minimum of 2". The owner would then be recorded as having taken a trade against a rule
    they did not break. A grid search over 200 entries x 90 stop distances found 4,200 such
    triples, so this is the common case rather than an exotic one.

    `Result.figure` keeps full precision; only `ok` is decided at the stated precision. Same
    argument as fmt._flat, which exists because three callers read one residue three ways.
    """
    return round(figure, places)


def _hours_since(now: dt.datetime, then: dt.datetime) -> float | None:
    """Hours between two instants, or None if they cannot be compared at all."""
    try:
        return (now - then).total_seconds() / 3600
    except TypeError:  # one carries a time zone and the other does not
        return None


def _convert(amount: float, src: str, dst: str, ctx: Context) -> float | None:
    if src == dst:
        return amount
    rate = ctx.fx(src, dst)
    return None if rate is None else amount * rate


def _side_word(a: float, b: float) -> str:
    """Where a sits relative to b, as a word, so the fact reads as one."""
    return "above" if a > b else "below" if a < b else "at"


def _risk(plan: Plan) -> tuple[float | None, str]:
    """size x stop distance x point value, in the plan's own currency, or (None, why not)."""
    if plan.entry is None:
        return None, "the plan has no entry, so the risk cannot be computed"
    if plan.stop is None:
        return None, "the plan has no stop, so the risk cannot be computed"
    if plan.size is None:
        return None, "the plan has no size, so the risk cannot be computed"
    if plan.size <= 0:
        # An over-exit is recorded rather than clamped, so a negative size can reach here.
        return None, f"the planned size is {plan.size}, so the risk cannot be computed"
    if not plan.point_value or plan.point_value <= 0:
        return None, "the plan has no point value, so the risk cannot be computed"
    distance = abs(plan.entry - plan.stop)
    if distance == 0:
        return None, "the stop is at the entry, so there is no risk distance"
    return plan.size * distance * plan.point_value, ""


def _open_sizes(ctx: Context) -> tuple[list[TradeState], str]:
    """The open trades that still hold something, or ([], why they cannot be counted).

    Counted by `size_open > 0` and NOT by `status == "open"`: exiting and closing are separate
    acts, so a trade whose shares are all gone but which has no `closed` event yet is still
    `status="open"` with `size_open=0`. Counting by status would let a finished trade block the
    owner's next trade -- a rule silently refusing a legitimate trade.
    """
    broken = [t.trade_id for t in ctx.open_trades if t.size_open is not None and t.size_open < 0]
    if broken:
        return [], (
            f"the open trades cannot be counted: a negative open size on "
            f"{', '.join(broken)} means an exit exceeded the size, which needs correcting first"
        )
    return [t for t in ctx.open_trades if (t.size_open or 0) > 0], ""


def _units(n: int, plural: str) -> str:
    """"1 trade", "2 trades": a count reads as a fact only if its noun agrees with it."""
    return f"{n} {plural[:-1] if n == 1 else plural}"


# ------------------------------------------------------------- the checks ---
# One function per rule name in the spec's §5 table, each (plan, value, ctx, rules) -> Result.
# `rules` is passed because two of them read it: `size` needs the risk budget and
# `no_open_trade_in_group` needs the file's groups.
def min_reward_risk(plan: Plan, value: str, ctx: Context, rules: Rules) -> Result:
    limit = _number(value)
    if limit is None:
        return _withheld("min_reward_risk", value, f"I could not read a number in {value!r}", "R")
    if plan.entry is None:
        return _withheld(
            "min_reward_risk", value,
            "the plan has no entry, so reward to risk cannot be computed", "R")
    if plan.target is None:
        return _withheld(
            "min_reward_risk", value,
            "the plan has no target, so reward to risk cannot be computed", "R")
    if plan.stop is None:
        return _withheld(
            "min_reward_risk", value,
            "the plan has no stop, so reward to risk cannot be computed", "R")
    risk = abs(plan.entry - plan.stop)
    if risk == 0:
        return _withheld(
            "min_reward_risk", value,
            "the stop is at the entry, so there is no risk to divide the reward by", "R")
    # The reward is signed and the risk is a distance: dividing two signed figures would
    # cancel the signs and pass an inverted trade. _signed is model's, so the side flips a
    # figure in exactly one place in this codebase.
    ratio = _signed(plan.side, plan.entry, plan.target) / risk
    return Result(
        "min_reward_risk", _at(ratio) >= limit, ratio, value,
        f"reward to risk {ratio:.2f} against a minimum of {value}", "R")


def max_risk_per_trade(plan: Plan, value: str, ctx: Context, rules: Rules) -> Result:
    limit = _number(value)
    currency = _currency(value)
    if limit is None:
        return _withheld("max_risk_per_trade", value, f"I could not read an amount in {value!r}")
    if currency is None:
        return _withheld(
            "max_risk_per_trade", value,
            f"the limit {value!r} names no currency, so I cannot compare it to a risk in "
            f"{plan.currency}")
    risk, why = _risk(plan)
    if risk is None:
        return _withheld("max_risk_per_trade", value, why, currency)
    converted = _convert(risk, plan.currency, currency, ctx)
    if converted is None:
        return _withheld(
            "max_risk_per_trade", value,
            f"I have no {plan.currency} to {currency} rate, so the risk cannot be stated in "
            f"the limit's currency", currency)
    return Result(
        "max_risk_per_trade", _at(converted) <= limit, converted, value,
        f"risk {fmt.money(converted)} {currency} against a limit of {value}", currency)


def _suggest(rules: Rules, plan: Plan, ctx: Context) -> tuple[int | None, str]:
    """max risk / (stop distance x point value), rounded DOWN, or (None, why not)."""
    budget_rule = rules.numbers.get("max_risk_per_trade")
    if budget_rule is None:
        return None, "there is no max_risk_per_trade rule, so there is no budget to size against"
    budget = _number(budget_rule)
    currency = _currency(budget_rule)
    if budget is None or currency is None:
        return None, (f"I could not read an amount and a currency in the max_risk_per_trade "
                      f"rule {budget_rule!r}")
    if plan.entry is None:
        return None, "the plan has no entry, so I cannot suggest a size"
    if plan.stop is None:
        return None, "the plan has no stop, so I cannot suggest a size"
    if not plan.point_value or plan.point_value <= 0:
        return None, "the plan has no point value, so I cannot suggest a size"
    distance = abs(plan.entry - plan.stop)
    if distance == 0:
        return None, "the stop is at the entry, so no size follows from the risk budget"
    # The risk of ONE unit, converted into the budget's currency rather than the budget into
    # the plan's: that is the same rate direction max_risk_per_trade needs, so one rate source
    # serves both and a one-way fx function is enough.
    each = _convert(distance * plan.point_value, plan.currency, currency, ctx)
    if each is None:
        return None, (f"I have no {plan.currency} to {currency} rate, so the budget cannot be "
                      f"applied to this trade's prices")
    # Rounded down, because rounding up would exceed the limit by part of one unit -- but
    # through _at first, or the floor turns float residue into a false breach: 100 over a
    # 90.00/89.96 stop divides to 2499.9999999996094, and flooring that raw reports 2499
    # allowed on a trade whose 2500 shares risk exactly the limit.
    return math.floor(_at(budget / each)), ""


def suggest_size(rules: Rules, plan: Plan, ctx: Context) -> int | None:
    """The size the risk limit allows: max risk / (stop distance x point value), rounded down."""
    return _suggest(rules, plan, ctx)[0]


def size(plan: Plan, value: str, ctx: Context, rules: Rules) -> Result:
    # The rule's value is not a limit (the file says `size: computed`); the budget comes from
    # max_risk_per_trade, so the value is only carried through for the record.
    unit = "contracts" if plan.instrument == "future" else "shares"
    suggested, why = _suggest(rules, plan, ctx)
    if suggested is None:
        return _withheld("size", value, why, unit)
    if plan.size is None:
        return Result(
            "size", None, float(suggested), value,
            f"the risk limit allows {_units(suggested, unit)}; the plan names no size", unit)
    if plan.size <= 0:
        # Never size off a figure that is not a position. An over-exit is recorded rather
        # than clamped, so a negative size can reach here.
        return _withheld(
            "size", value,
            f"the planned size is {plan.size}, so it cannot be compared to the "
            f"{_units(suggested, unit)} the risk limit allows", unit)
    return Result(
        "size", plan.size <= suggested, float(suggested), value,
        f"the plan is {_units(plan.size, unit)} against the {_units(suggested, unit)} "
        f"the risk limit allows", unit)


def _order_required(name: str, plan: Plan, value: str, price: float | None, reward: bool) -> Result:
    """stop_required and target_required: present, and on the right side of the entry."""
    word = "target" if reward else "stop"
    required = _yes(value)
    if required is None:
        return _withheld(
            name, value,
            f"I could not read {value!r} as a yes or a no, so I cannot tell whether a {word} "
            f"is required")
    if not required:
        return Result(name, True, None, value, f"the rules do not require a {word} ({value})", None)
    if price is None:
        return Result(name, False, None, value, f"the plan has no {word}", None)
    if plan.entry is None:
        return _withheld(
            name, value, f"the plan has no entry, so I cannot tell which side the {word} is on")
    # _signed again: for a long the stop sits below the entry and the target above it, and for
    # a short the other way round.
    right_side = (_signed(plan.side, plan.entry, price) if reward
                  else _signed(plan.side, price, plan.entry)) > 0
    side_of_entry = "the reward side" if reward else "the risk side"
    return Result(
        name, right_side, None, value,
        f"the {word} {price:g} is {_side_word(price, plan.entry)} the entry {plan.entry:g}, "
        f"which is {'' if right_side else 'not '}{side_of_entry} for a {plan.side}", None)


def stop_required(plan: Plan, value: str, ctx: Context, rules: Rules) -> Result:
    return _order_required("stop_required", plan, value, plan.stop, reward=False)


def target_required(plan: Plan, value: str, ctx: Context, rules: Rules) -> Result:
    return _order_required("target_required", plan, value, plan.target, reward=True)


def stop_headroom_candles(plan: Plan, value: str, ctx: Context, rules: Rules) -> Result:
    limit = _number(value)
    if limit is None:
        return _withheld(
            "stop_headroom_candles", value, f"I could not read a number in {value!r}", "candles")
    if plan.entry is None or plan.stop is None:
        # Named before the bars, because this is the half the owner can do something about.
        missing = "entry" if plan.entry is None else "stop"
        return _withheld(
            "stop_headroom_candles", value,
            f"the plan has no {missing}, so the stop's headroom cannot be measured", "candles")
    if ctx.median_candle_range is None:
        return _withheld(
            "stop_headroom_candles", value,
            "price bars are not in the record yet, so I cannot measure the stop against the "
            "median candle range", "candles")
    if ctx.median_candle_range <= 0:
        return _withheld(
            "stop_headroom_candles", value,
            "the median candle range is zero, so the stop cannot be measured in candles",
            "candles")
    candles = abs(plan.entry - plan.stop) / ctx.median_candle_range
    return Result(
        "stop_headroom_candles", _at(candles) >= limit, candles, value,
        f"the stop is {candles:.2f} candles from the entry against a minimum of {value}",
        "candles")


def max_open_trades(plan: Plan, value: str, ctx: Context, rules: Rules) -> Result:
    limit = _number(value)
    if limit is None:
        return _withheld("max_open_trades", value, f"I could not read a number in {value!r}",
                         "trades")
    holding, why = _open_sizes(ctx)
    if why:
        return _withheld("max_open_trades", value, why, "trades")
    count = len(holding)
    # The sentence states the figure the limit is compared against -- the book WITH this trade
    # on it -- or "1 open against a limit of 1" would read as a pass beside ok=False.
    return Result(
        "max_open_trades", count + 1 <= limit, float(count), value,
        f"{_units(count, 'trades')} already open; this would make {count + 1} against a limit "
        f"of {value}", "trades")


def no_open_trade_in_group(plan: Plan, value: str, ctx: Context, rules: Rules) -> Result:
    guarded = [g.strip() for g in value.split(",") if g.strip()]
    undefined = [g for g in guarded if g not in rules.groups]
    if undefined:
        return _withheld(
            "no_open_trade_in_group", value,
            f"the rules file defines no group called {', '.join(undefined)}, so I cannot tell "
            f"which tickers it holds", "trades")
    holding, why = _open_sizes(ctx)
    if why:
        return _withheld("no_open_trade_in_group", value, why, "trades")
    mine = [g for g in guarded if plan.ticker in rules.groups[g]]
    if not mine:
        return Result(
            "no_open_trade_in_group", True, 0.0, value,
            f"{plan.ticker} is in none of: {value}", "trades")
    tickers = sorted({t.ticker for t in holding for g in mine if t.ticker in rules.groups[g]})
    group_names = ", ".join(mine)
    if not tickers:
        return Result(
            "no_open_trade_in_group", True, 0.0, value,
            f"no open trade in {group_names}, which {plan.ticker} is in", "trades")
    return Result(
        "no_open_trade_in_group", False, float(len(tickers)), value,
        f"{_units(len(tickers), 'trades')} open in {group_names}, which {plan.ticker} is in: "
        f"{', '.join(tickers)}", "trades")


def cooldown_after_stop(plan: Plan, value: str, ctx: Context, rules: Rules) -> Result:
    hours = _hours(value)
    if hours is None:
        return _withheld("cooldown_after_stop", value, f"I could not read a duration in {value!r}",
                         "h")
    # Read from the whole value, so "same ticker, 1 day" scopes the same as "1 day, same ticker".
    same_ticker = "same ticker" in value.lower().replace("_", " ")
    if ctx.stopped_closes is None:
        return _withheld(
            "cooldown_after_stop", value,
            "the closes of stopped trades were not supplied, so the cooldown cannot be "
            "measured", "h")
    scope = f" on {plan.ticker}" if same_ticker else ""
    if same_ticker:
        closes = [t for ticker, t in ctx.stopped_closes.items() if ticker == plan.ticker]
    else:
        closes = list(ctx.stopped_closes.values())
    if not closes:
        return Result(
            "cooldown_after_stop", True, None, value, f"no stopped trade{scope} on record", "h")
    elapsed = _hours_since(ctx.now, max(closes))
    if elapsed is None:
        return _withheld(
            "cooldown_after_stop", value,
            "the time now and the last stop's close cannot be compared: one carries a time "
            "zone and the other does not", "h")
    if elapsed < 0:
        # Nonsense in either direction: a breach would be as wrong as a pass.
        return _withheld(
            "cooldown_after_stop", value,
            f"the last stop{scope} closed {abs(elapsed):.1f} h in the future, so the clocks "
            f"disagree and the cooldown cannot be measured", "h")
    return Result(
        "cooldown_after_stop", _at(elapsed, 1) >= hours, elapsed, value,
        f"the last stop{scope} closed {elapsed:.1f} h ago against a cooldown of {value}", "h")


def max_session_length(plan: Plan, value: str, ctx: Context, rules: Rules) -> Result:
    hours = _hours(value)
    if hours is None:
        return _withheld("max_session_length", value, f"I could not read a duration in {value!r}",
                         "h")
    if ctx.session_started is None:
        return _withheld(
            "max_session_length", value,
            "no session start was given, so the session's length cannot be measured", "h")
    elapsed = _hours_since(ctx.now, ctx.session_started)
    if elapsed is None:
        return _withheld(
            "max_session_length", value,
            "the time now and the session's start cannot be compared: one carries a time zone "
            "and the other does not", "h")
    if elapsed < 0:
        # A session that has not started yet has no length; "-1.0 h against a limit of 2h" was
        # passing, which is a figure no clock can produce.
        return _withheld(
            "max_session_length", value,
            f"the session's start is {abs(elapsed):.1f} h in the future, so the clocks "
            f"disagree and the session's length cannot be measured", "h")
    return Result(
        "max_session_length", _at(elapsed, 1) <= hours, elapsed, value,
        f"the session has run {elapsed:.1f} h against a limit of {value}", "h")


def daily_loss_limit(plan: Plan, value: str, ctx: Context, rules: Rules) -> Result:
    limit = _number(value)
    if limit is None:
        return _withheld("daily_loss_limit", value, f"I could not read a number in {value!r}", "R")
    # The limit is a loss floor however it is written: "-2R" and "2R" both mean 2R down.
    floor = -abs(limit)
    if ctx.closed_today_r is None:
        return _withheld(
            "daily_loss_limit", value,
            "today's closed R was not supplied, so it cannot be totalled; an unknown total is "
            "not zero", "R")
    return Result(
        "daily_loss_limit", _at(ctx.closed_today_r) >= floor, ctx.closed_today_r, value,
        f"today's closed R is {fmt.signed(ctx.closed_today_r)} R against a daily limit of "
        f"{value}", "R")


def _singular(word: str) -> str:
    return word[:-1] if word.endswith("s") else word


def banned(plan: Plan, value: str, ctx: Context, rules: Rules) -> Result:
    # Only the side and the instrument are visible to this check: leverage and options are on
    # the owner's list but v1's record has no field for either, so this never claims to have
    # checked them.
    listed = {_singular(t.strip().lower()) for t in value.split(",") if t.strip()}
    hits = [w for w in (plan.side, plan.instrument) if w and _singular(w.lower()) in listed]
    if hits:
        return Result(
            "banned", False, None, value,
            f"this is a {plan.side} {plan.instrument}, and the rules ban "
            f"{' and '.join(hits)}: {value}", None)
    return Result(
        "banned", True, None, value,
        f"a {plan.side} {plan.instrument} is not on the banned list: {value}", None)


def max_volatility_ratio(plan: Plan, value: str, ctx: Context, rules: Rules) -> Result:
    limit = _number(value)
    if limit is None:
        return _withheld("max_volatility_ratio", value, f"I could not read a number in {value!r}")
    if ctx.volatility_ratio is None:
        return _withheld(
            "max_volatility_ratio", value,
            "price bars are not in the record yet, so 20-day volatility cannot be compared to "
            "its 1-year median")
    return Result(
        "max_volatility_ratio", _at(ctx.volatility_ratio) <= limit, ctx.volatility_ratio, value,
        f"20-day volatility is {ctx.volatility_ratio:.2f} times its 1-year median against a "
        f"limit of {value}", None)


# The registry, in the order of the spec's §5 table: run_checks walks it, and CHECK_NAMES is
# derived from it so a new check cannot be implemented without becoming known to rules.py.
_CHECKS: dict[str, Callable[[Plan, str, Context, Rules], Result]] = {
    fn.__name__: fn for fn in (
        min_reward_risk,
        max_risk_per_trade,
        size,
        stop_required,
        target_required,
        stop_headroom_candles,
        max_open_trades,
        no_open_trade_in_group,
        cooldown_after_stop,
        max_session_length,
        daily_loss_limit,
        banned,
        max_volatility_ratio,
    )
}

CHECK_NAMES: frozenset[str] = frozenset(_CHECKS)


def run_checks(plan: Plan, rules: Rules, ctx: Context) -> list[Result]:
    """Every rule in the scope, in the §5 table's order, then the ones with no code check."""
    results = [
        fn(plan, rules.numbers[name], ctx, rules)
        for name, fn in _CHECKS.items()
        if name in rules.numbers
    ]
    # A name in the file that this module does not implement is asked, never dropped silently,
    # and its Result carries the value as written so the desk can quote the rule back. The says
    # field includes both the rule name and value so a misspelling is visible on the line.
    # rules.unchecked already holds them when for_scope was given CHECK_NAMES; the second half
    # catches a caller that passed a wider `known` set.
    asked = {**rules.unchecked,
             **{n: v for n, v in rules.numbers.items() if n not in _CHECKS}}
    results.extend(Result(name, None, None, value, f"{name}: {value} — {ASKED}", None)
                   for name, value in asked.items())
    return results
