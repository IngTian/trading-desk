"""The number rules, computed in code.

The owner types a planned trade and this module computes every number rule before they click.
Two rules govern the whole file:

**The AI never does a number rule's arithmetic.** Every figure below is computed here, in
Python. A strategy naming a rule with no function in `_CHECKS` is refused at save (see
`RULE_NAMES`), so nothing is ever estimated or silently skipped.

**A result is a fact, never a block and never a grade.** `ok` says whether the figure sits
inside the limit; nothing here can stop a click. No scores, no streaks.

## Withholding

`ok=None` means *I could not tell*, and `says` names the input that was missing. A check that
silently passed for want of data would be the worst outcome this tool can produce, so every
guard below withholds rather than defaults: no 1:1 fallback for a missing exchange rate, no
zero for an unknown daily total, and no figure at all derived from a negative size.

`Context.stopped_closes` maps ticker -> the close time of the most recent trade on that ticker
that exited at or beyond its stop. `None` means it was not supplied (so the cooldown withholds)
and `{}` means the log was read and holds no stop (so it passes).
"""
from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from desk.model import Plan, _signed, canonical_ticker
from desk.rules import Rules


@dataclass(frozen=True)
class Result:
    """One rule's result: the figure and the limit, stated. Never a verdict on the trader."""
    name: str
    ok: bool | None      # None = could not be computed
    figure: float | None
    limit: str           # the rule's value, verbatim from the strategy
    says: str            # the fact, e.g. "risk 140.00 CAD against a limit of 100 CAD"
    unit: str | None


def no_fx(src: str, dst: str) -> float | None:
    """The default rate source: 1.0 within a currency and nothing across one.

    Returning 1.0 for a cross-currency pair would compare a risk to a limit in another
    currency and call it a pass, so the default refuses instead.
    """
    return 1.0 if src == dst else None


@dataclass(frozen=True)
class OpenPosition:
    """A trade that may still hold shares: all a check needs to count it."""
    ticker: str
    size_open: int


@dataclass(frozen=True)
class Context:
    """Everything a check needs that is not in the plan or the rules."""
    now: dt.datetime
    open_trades: list[OpenPosition] = field(default_factory=list)
    closed_today_r: float | None = None
    median_candle_range: float | None = None
    stopped_closes: dict[str, dt.datetime] | None = None
    # fx(src, dst) -> the rate that multiplies an amount in src to give dst, or None if unknown.
    fx: Callable[[str, str], float | None] = no_fx


# ------------------------------------------------------------------ parsing ---
# Rules.numbers values are RAW strings out of the strategy ("100 CAD", "2", "1 day, same ticker"),
# so every check parses its own. A value that cannot be read is withheld, not guessed at.
# Every helper below reads a value WHOLE or not at all. Taking the first number found read
# "1:2" as a floor of 1 and "2,5" as 2, and the first duration read "2h30m" as 2h: three ways
# a typed rule came out looser than the owner wrote it. A value that does not read as one
# thing is withheld, with the value quoted so the owner can see what to fix.
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
# Thousands separators are the one comma a number may carry: "1,000" but not "2,5".
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
# Words a number may stand beside: its unit, or a currency. Anything else beside a number
# (":", "-", "or", a second number) makes the value ambiguous.
_UNIT_WORDS = re.compile(r"^[\sA-Za-z]*$")
_CURRENCIES = frozenset({"AUD", "CAD", "CHF", "EUR", "GBP", "HKD", "JPY", "USD"})
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
    """The one number in a rule value: "2" -> 2.0, "1,000 CAD" -> 1000.0, "-2R" -> -2.0.

    None unless there is exactly one number and only unit words beside it: "1:2", "2,5",
    "2 or 3", "2-3" and "about 2" are all withheld rather than read as their first number.
    """
    plain = _THOUSANDS.sub("", value)
    found = _NUMBER.findall(plain)
    if len(found) != 1:
        return None
    rest = _NUMBER.sub(" ", plain, count=1)
    words = rest.split()
    if not _UNIT_WORDS.match(rest) or (words and words[0].lower() in {"about", "around", "or"}):
        return None
    return float(found[0])


def _currency(value: str) -> str | None:
    """The one currency code in a rule value: "100 CAD" -> "CAD", "100 usd" -> "USD".

    None if it names none or more than one. Only known codes count, so a three-letter word
    such as "per" is never taken for a currency.
    """
    named = {w.upper() for w in re.findall(r"(?<![A-Za-z])[A-Za-z]{3}(?![A-Za-z])", value)}
    named &= _CURRENCIES
    return named.pop() if len(named) == 1 else None


def _hours(value: str) -> float | None:
    """A duration in hours, every part of it: "2h" -> 2.0, "2h30m" -> 2.5, "1 day, same
    ticker" -> 24.0. None if a part's unit is unknown or a number stands without a unit."""
    parts = _DURATION.findall(value)
    if not parts or len(_NUMBER.findall(value)) != len(parts):
        return None
    if any(unit.lower() not in _HOURS for _, unit in parts):
        return None
    return sum(float(n) * _HOURS[unit.lower()] for n, unit in parts)


def _unreadable(name: str, value: str, wanted: str, unit: str | None = None) -> Result:
    """A rule value that does not read as one thing, withheld with the value quoted."""
    return _withheld(
        name, value,
        f"I could not read {value!r} as {wanted}; write it as one, e.g. {_EXAMPLES[wanted]}",
        unit)


_EXAMPLES = {
    "a number": "2",
    "an amount and a currency": "100 USD",
    "a duration": "2h or 1 day",
}


def _yes(value: str) -> bool | None:
    """True, False, or None when the value reads as neither -- which is not a no."""
    word = value.strip().lower()
    return True if word in _YES else False if word in _NO else None


def _withheld(name: str, value: str, says: str, unit: str | None = None) -> Result:
    """A figure that could not be computed, with the missing input named."""
    return Result(name, None, None, value, says, unit)


def _within(figure: float, limit: float, at_least: bool) -> bool:
    """figure >= limit (at_least) or figure <= limit, forgiving float residue and nothing else.

    A plain 2:1 trade on a cent grid -- entry 90.00, stop 89.96, target 90.08 -- divides to
    1.9999999999996447, and a raw `>= 2` would record the owner as having taken a trade
    against a rule they did not break; a grid search found 4,200 such triples. That residue
    is around 1e-13. The tolerance is a relative 1e-9: far above any residue, far below any
    difference a price can make.

    It used to be rounding to the printed precision instead, which forgave far more than
    residue: a 1.996 reward-to-risk passed a floor of 2, and a session 3 minutes over a
    2-hour limit passed it.
    """
    slack = 1e-9 * max(1.0, abs(limit))
    return figure >= limit - slack if at_least else figure <= limit + slack


def _shown(figure: float, limit: float, at_least: bool, places: int = 2,
           money: bool = False, signed: bool = False) -> str:
    """The figure with as many decimals as it takes for the digits to agree with `ok`.

    `ok` MUST NOT CONTRADICT THE DIGITS BESIDE IT, in either direction: residue must not
    print as a breach, and a breach must not print as the limit. 1.996 against a floor of 2
    is shown as 1.996, never 2.00. Two decimals are enough almost always.
    """
    ok = _within(figure, limit, at_least)
    for p in range(places, 7):
        if _within(round(figure, p), limit, at_least) == ok:
            places = p
            break
    else:
        places = 6
    if signed:
        # fmt._flat's rule: a figure that prints as zero carries no sign.
        figure = 0.0 if abs(figure) < 0.5 * 10 ** -places else figure
        return f"{figure:+.{places}f}"
    return f"{figure:,.{places}f}" if money else f"{figure:.{places}f}"


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


def _finite(*prices: float | None) -> bool:
    return all(p is None or math.isfinite(p) for p in prices)


def _stop_distance(plan: Plan, what: str) -> tuple[float | None, str]:
    """How far the stop is from the entry ON THE RISK SIDE, or (None, why `what` cannot be had).

    A stop on the wrong side -- above the entry for a long, below for a short -- is not a
    stop-loss: no loss stops there. Taking abs() of the distance anyway passed
    min_reward_risk and max_risk_per_trade for a long with its stop 5 above its entry.
    """
    if plan.entry is None:
        return None, f"the plan has no entry, so {what} cannot be computed"
    if plan.stop is None:
        return None, f"the plan has no stop, so {what} cannot be computed"
    if not _finite(plan.entry, plan.stop):
        return None, f"the plan's entry or stop is not a number, so {what} cannot be computed"
    if plan.stop == plan.entry:
        return None, f"the stop is at the entry, so there is no risk distance and {what} " \
                     f"cannot be computed"
    if _signed(plan.side, plan.stop, plan.entry) < 0:
        return None, (f"the stop {plan.stop:g} is {_side_word(plan.stop, plan.entry)} the entry "
                      f"{plan.entry:g}, which is not the risk side for a {plan.side}, so {what} "
                      f"cannot be computed")
    return abs(plan.entry - plan.stop), ""


def _risk_in(plan: Plan, size: int, currency: str, ctx: Context) -> tuple[float | None, str]:
    """The risk of `size` units of this plan, in `currency`: size x stop distance x point value.

    THE ONE PLACE RISK IS COMPUTED. max_risk_per_trade states it and size searches it, so the
    size the risk limit allows is by construction the largest one max_risk_per_trade passes.
    They used to be two formulas, and size could allow 5 shares that max_risk then called a
    breach.
    """
    distance, why = _stop_distance(plan, "the risk")
    if distance is None:
        return None, why
    if not plan.point_value or plan.point_value <= 0:
        return None, "the plan has no point value, so the risk cannot be computed"
    converted = _convert(size * distance * plan.point_value, plan.currency, currency, ctx)
    if converted is None:
        return None, (f"I have no {plan.currency} to {currency} rate, so the risk cannot be "
                      f"stated in the limit's currency")
    return converted, ""


def _budget(value: str) -> tuple[float | None, str | None]:
    """A max_risk_per_trade value read whole: (amount, currency), either None if unreadable."""
    return _number(value), _currency(value)


def _open_sizes(ctx: Context) -> tuple[list[OpenPosition], str]:
    """The open trades that still hold something, or ([], why they cannot be counted).

    Counted by `size_open > 0` and NOT by `status == "open"`: exiting and closing are separate
    acts, so a trade whose shares are all gone but which has no `closed` event yet is still
    `status="open"` with `size_open=0`. Counting by status would let a finished trade block the
    owner's next trade -- a rule silently refusing a legitimate trade.
    """
    broken = [t.ticker for t in ctx.open_trades if t.size_open < 0]
    if broken:
        return [], (
            f"the open trades cannot be counted: a negative open size on "
            f"{', '.join(broken)} means an exit exceeded the size, which needs correcting first"
        )
    return [t for t in ctx.open_trades if t.size_open > 0], ""


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
        return _unreadable("min_reward_risk", value, "a number", "R")
    if plan.target is None:
        return _withheld(
            "min_reward_risk", value,
            "the plan has no target, so reward to risk cannot be computed", "R")
    if not _finite(plan.target):
        return _withheld(
            "min_reward_risk", value,
            "the plan's target is not a number, so reward to risk cannot be computed", "R")
    risk, why = _stop_distance(plan, "reward to risk")
    if risk is None:
        return _withheld("min_reward_risk", value, why, "R")
    # The reward is signed and the risk is a distance on the risk side: dividing two signed
    # figures would cancel the signs and pass an inverted trade. _signed is model's, so the
    # side flips a figure in exactly one place in this codebase.
    ratio = _signed(plan.side, plan.entry, plan.target) / risk
    return Result(
        "min_reward_risk", _within(ratio, limit, at_least=True), ratio, value,
        f"reward to risk {_shown(ratio, limit, at_least=True)} against a minimum of {value}",
        "R")


def max_risk_per_trade(plan: Plan, value: str, ctx: Context, rules: Rules) -> Result:
    limit, currency = _budget(value)
    if limit is None:
        return _unreadable("max_risk_per_trade", value, "an amount and a currency")
    if currency is None:
        return _withheld(
            "max_risk_per_trade", value,
            f"the limit {value!r} names no single currency, so I cannot compare it to a risk "
            f"in {plan.currency}")
    if plan.size is None:
        return _withheld("max_risk_per_trade", value,
                         "the plan has no size, so the risk cannot be computed", currency)
    if plan.size <= 0:
        # An over-exit is recorded rather than clamped, so a negative size can reach here.
        return _withheld("max_risk_per_trade", value,
                         f"the planned size is {plan.size}, so the risk cannot be computed",
                         currency)
    risk, why = _risk_in(plan, plan.size, currency, ctx)
    if risk is None:
        return _withheld("max_risk_per_trade", value, why, currency)
    return Result(
        "max_risk_per_trade", _within(risk, limit, at_least=False), risk, value,
        f"risk {_shown(risk, limit, at_least=False, money=True)} {currency} against a limit "
        f"of {value}", currency)


def _suggest(rules: Rules, plan: Plan, ctx: Context) -> tuple[int | None, str]:
    """The largest size whose risk max_risk_per_trade passes, or (None, why not)."""
    budget_rule = rules.numbers.get("max_risk_per_trade")
    if budget_rule is None:
        return None, "there is no max_risk_per_trade rule, so there is no budget to size against"
    budget, currency = _budget(budget_rule)
    if budget is None or currency is None:
        return None, (f"I could not read one amount and one currency in the max_risk_per_trade "
                      f"rule {budget_rule!r}")
    each, why = _risk_in(plan, 1, currency, ctx)
    if each is None:
        return None, why.replace("the risk cannot be computed", "I cannot suggest a size")
    # The quotient is only a first guess: float residue can put it a hair either side of a
    # whole number (100 over a 90.00/89.96 stop is 2499.9999999996094). The answer is then
    # settled by asking _risk_in -- the function max_risk_per_trade asks -- about the sizes
    # around the guess, so the two checks cannot disagree.
    n = max(0, math.floor(budget / each))
    while _within(_risk_in(plan, n + 1, currency, ctx)[0], budget, at_least=False):
        n += 1
    while n > 0 and not _within(_risk_in(plan, n, currency, ctx)[0], budget, at_least=False):
        n -= 1
    return n, ""


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
        return _unreadable("stop_headroom_candles", value, "a number", "candles")
    # The plan's half first, because it is the half the owner can do something about.
    distance, why = _stop_distance(plan, "the stop's headroom")
    if distance is None:
        return _withheld("stop_headroom_candles", value, why, "candles")
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
    candles = distance / ctx.median_candle_range
    return Result(
        "stop_headroom_candles", _within(candles, limit, at_least=True), candles, value,
        f"the stop is {_shown(candles, limit, at_least=True)} candles from the entry against a "
        f"minimum of {value}", "candles")


def max_open_trades(plan: Plan, value: str, ctx: Context, rules: Rules) -> Result:
    limit = _number(value)
    if limit is None:
        return _unreadable("max_open_trades", value, "a number", "trades")
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


def _ticker(typed: str) -> str:
    """A ticker as every comparison here sees it: upper case, a continuous future as its root."""
    return canonical_ticker(typed)[0]


def cooldown_after_stop(plan: Plan, value: str, ctx: Context, rules: Rules) -> Result:
    # "same ticker" is taken out before the duration is read, so its words are not units.
    same_ticker = "same ticker" in value.lower().replace("_", " ")
    hours = _hours(re.sub(r"same[\s_]ticker", "", value, flags=re.IGNORECASE))
    if hours is None:
        return _unreadable("cooldown_after_stop", value, "a duration", "h")
    if ctx.stopped_closes is None:
        return _withheld(
            "cooldown_after_stop", value,
            "the closes of stopped trades were not supplied, so the cooldown cannot be "
            "measured", "h")
    scope = f" on {plan.ticker}" if same_ticker else ""
    if same_ticker:
        closes = [t for ticker, t in ctx.stopped_closes.items()
                  if _ticker(ticker) == _ticker(plan.ticker)]
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
        "cooldown_after_stop", _within(elapsed, hours, at_least=True), elapsed, value,
        f"the last stop{scope} closed {_shown(elapsed, hours, at_least=True, places=1)} h ago "
        f"against a cooldown of {value}", "h")


def daily_loss_limit(plan: Plan, value: str, ctx: Context, rules: Rules) -> Result:
    limit = _number(value)
    if limit is None:
        return _unreadable("daily_loss_limit", value, "a number", "R")
    # The limit is a loss floor however it is written: "-2R" and "2R" both mean 2R down.
    floor = -abs(limit)
    if ctx.closed_today_r is None:
        return _withheld(
            "daily_loss_limit", value,
            "today's closed R was not supplied, so it cannot be totalled; an unknown total is "
            "not zero", "R")
    closed = ctx.closed_today_r
    return Result(
        "daily_loss_limit", _within(closed, floor, at_least=True), closed, value,
        f"today's closed R is {_shown(closed, floor, at_least=True, signed=True)} R against a "
        f"daily limit of {value}", "R")


# The words a banned list is written in, each mapped to what it bans. Only the side and the
# instrument are visible to this check: options and leverage are words it can READ, so their
# presence never stops a pass, but v1's record has no field for either, so it never claims to
# have checked them.
_BAN_TERMS = {
    "short": "short", "shorts": "short", "shorting": "short",
    "long": "long", "longs": "long",
    "stock": "stock", "stocks": "stock", "etf": "etf", "etfs": "etf",
    "future": "future", "futures": "future",
    "option": "option", "options": "option",
    "leverage": "leverage", "leveraged": "leverage", "margin": "leverage",
}
_BAN_FILLER = {"no", "and", "or", "nor", "selling", "sales", "trading", "trades", "any", "all"}


def banned(plan: Plan, value: str, ctx: Context, rules: Rules) -> Result:
    # Read word by word, not as exact comma-separated entries: "no shorts", "short selling" and
    # "shorts and futures" each let a short through when only "short" or "shorts" between
    # commas counted.
    terms, unread = set(), []
    for word in re.findall(r"[a-z]+", value.lower()):
        if word in _BAN_TERMS:
            terms.add(_BAN_TERMS[word])
        elif word not in _BAN_FILLER:
            unread.append(word)
    hits = [w for w in (plan.side, plan.instrument) if w in terms]
    if hits:
        # A ban it can see is a breach whatever else the list says.
        return Result(
            "banned", False, None, value,
            f"this is a {plan.side} {plan.instrument}, and the rules ban "
            f"{' and '.join(hits)}: {value}", None)
    if unread:
        # It cannot see everything the list bans, so it cannot say this trade is clear of it.
        return _withheld(
            "banned", value,
            f"I could not read {', '.join(repr(w) for w in unread)} in the banned list "
            f"{value!r}, so I cannot tell everything it bans; a {plan.side} {plan.instrument} "
            f"is not among the words I could read")
    return Result(
        "banned", True, None, value,
        f"a {plan.side} {plan.instrument} is not on the banned list: {value}", None)


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
        cooldown_after_stop,
        daily_loss_limit,
        banned,
    )
}

CHECK_NAMES: frozenset[str] = frozenset(_CHECKS)


def run_checks(plan: Plan, rules: Rules, ctx: Context) -> list[Result]:
    """Every rule in the strategy, in the table's order. Unknown names never get here: a save
    refuses them, so no result is ever "asked"."""
    return [
        fn(plan, rules.numbers[name], ctx, rules)
        for name, fn in _CHECKS.items()
        if name in rules.numbers
    ]


# Rule names the 1.0 desk checks, besides `timeframes`, which only names the candle sizes.
RULE_NAMES: frozenset[str] = CHECK_NAMES | {"timeframes"}
# Names plan 1 checked and 1.0 does not: told apart from a typo so the refusal can say so.
RETIRED: frozenset[str] = frozenset(
    {"no_open_trade_in_group", "max_session_length", "max_volatility_ratio"})


def read_value(name: str, value: str) -> str | None:
    """None if the rule's value reads the way its check needs; else why it does not.

    The same readers the checks use, so a value that saves is a value that checks.
    """
    if name == "size":
        ok = value.strip().lower() == "computed"
        wanted = "computed"
    elif name == "stop_required" or name == "target_required":
        ok = _yes(value) is not None
        wanted = "yes or no"
    elif name == "max_risk_per_trade":
        ok = _number(value) is not None and _currency(value) is not None
        wanted = _EXAMPLES["an amount and a currency"]
    elif name == "cooldown_after_stop":
        ok = _hours(re.sub(r"same[\s_]ticker", "", value, flags=re.IGNORECASE)) is not None
        wanted = "a duration such as 1 day, same ticker"
    elif name == "banned":
        ok = bool(value.strip())
        wanted = "a list such as shorts, options"
    else:
        ok = _number(value) is not None
        wanted = _EXAMPLES["a number"]
    return None if ok else f"I could not read {value!r}; write it as {wanted}"
