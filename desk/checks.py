"""The 1.0 checks: one pure function of the plan, the rules and the run as it stood at `at`.

**The AI never does a rule's arithmetic.** Every figure is computed here, in Python, from the
plan, the run's other trades and the indicator values the agent supplies under `market`.
Nothing here fetches, reads a clock or writes.

**A result is a fact, never a block and never a grade.** `ok` is True (kept), False (breached) or
None (unknown, and `says` names what is missing). Nothing reads kept for want of data.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import partial

from desk.model import Plan, _signed, canonical_ticker
from desk.rules import _DAYS, INDICATORS, ORDER, Rules, _finite, indicators


@dataclass(frozen=True)
class Result:
    """One rule's result: the figure and the limit, stated. Never a verdict on the trader."""
    name: str             # the rule, or an indicator check's label
    ok: bool | None       # True kept, False breached, None unknown
    figure: float | None
    limit: str            # the setting that applied, in short words: "2", "150 USD", "at_most 30"
    says: str             # the fact; when unknown, what is missing
    unit: str | None


@dataclass(frozen=True)
class Other:
    """Another non-void trade of the run, as its outcome reads."""
    id: str
    ticker: str
    entered: dt.datetime            # the entry's `at`, as corrected
    seq: int                        # recording order in the run
    ended: dt.datetime | None       # exited_at or cancelled_at; None while not ended
    exited: bool                    # the end is an exit, not a cancellation
    r: float | None
    cash: float | None


@dataclass(frozen=True)
class Context:
    at: dt.datetime                 # the decision's time, never the clock
    seq: int | None = None          # this trade's place; None: not recorded yet (after every other)
    currency: str | None = None     # the run's
    others: tuple[Other, ...] = ()
    market: Mapping[str, float] = field(default_factory=dict)      # the agent's


# ------------------------------------------------------------ arithmetic ---
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
        # A figure that prints as zero carries no sign.
        figure = 0.0 if abs(figure) < 0.5 * 10 ** -places else figure
        return f"{figure:+.{places}f}"
    return f"{figure:,.{places}f}" if money else f"{figure:.{places}f}"


def _side_word(a: float, b: float) -> str:
    """Where a sits relative to b, as a word, so the fact reads as one."""
    return "above" if a > b else "below" if a < b else "at"


def _units(n: int, plural: str) -> str:
    """"1 trade", "2 trades": a count reads as a fact only if its noun agrees with it."""
    return f"{n} {plural[:-1] if n == 1 else plural}"


def _stop_distance(plan: Plan, what: str) -> tuple[float | None, str]:
    """How far the stop is from the entry ON THE RISK SIDE, or (None, why `what` cannot be had).

    A stop on the wrong side -- above the entry for a long, below for a short -- is not a
    stop-loss: no loss stops there. Taking abs() of the distance anyway passed
    min_reward_risk and max_risk_per_trade for a long with its stop 5 above its entry.
    """
    if plan.stop is None:
        return None, f"the plan has no stop, so {what} cannot be computed"
    if not (math.isfinite(plan.entry) and math.isfinite(plan.stop)):
        return None, f"the plan's entry or stop is not a number, so {what} cannot be computed"
    if plan.stop == plan.entry:
        return None, f"the stop is at the entry, so there is no risk distance and {what} " \
                     f"cannot be computed"
    if _signed(plan.side, plan.stop, plan.entry) < 0:
        return None, (f"the stop {plan.stop:g} is {_side_word(plan.stop, plan.entry)} the entry "
                      f"{plan.entry:g}, which is not the risk side for a {plan.side}, so {what} "
                      f"cannot be computed")
    return abs(plan.entry - plan.stop), ""


def reward_risk(plan: Plan) -> tuple[float | None, str]:
    """Planned reward ÷ risk from the entry as corrected, or (None, why): the one place it is
    computed, for min_reward_risk and for stats."""
    if plan.target is None:
        return None, "the plan has no target, so reward to risk cannot be computed"
    if not math.isfinite(plan.target):
        return None, "the plan's target is not a number, so reward to risk cannot be computed"
    risk, why = _stop_distance(plan, "reward to risk")
    if risk is None:
        return None, why
    # The reward is signed and the risk is a distance on the risk side: dividing two signed
    # figures would cancel the signs and pass an inverted trade.
    return _signed(plan.side, plan.entry, plan.target) / risk, ""


# --------------------------------------------------------- the trade's own ---
def _min_reward_risk(plan: Plan, x, ctx: Context) -> Result:
    ratio, why = reward_risk(plan)
    if ratio is None:
        return Result("min_reward_risk", None, None, str(x), why, "R")
    return Result("min_reward_risk", _within(ratio, x, at_least=True), ratio, str(x),
                  f"reward to risk {_shown(ratio, x, at_least=True)} against a minimum of {x}",
                  "R")


def _max_risk_per_trade(plan: Plan, table: dict, ctx: Context) -> Result:
    cur = ctx.currency
    amount = table.get(cur)
    limit = (f"{amount} {cur}" if amount is not None
             else ", ".join(f"{a} {c}" for c, a in table.items()))
    if cur is None:
        return Result("max_risk_per_trade", None, None, limit,
                      "the run's currency is not given, so its limit cannot be chosen", None)
    if amount is None:
        return Result("max_risk_per_trade", None, None, limit,
                      f"max_risk_per_trade names no {cur} limit, and the run is in {cur}", cur)
    distance, why = _stop_distance(plan, "the risk")
    if distance is None:
        return Result("max_risk_per_trade", None, None, limit, why, cur)

    def risk(size: int) -> float:   # THE ONE PLACE RISK IS COMPUTED, so the size agrees with ok
        return size * distance * plan.point_value

    # The quotient is only a first guess: float residue can put it a hair either side of a
    # whole number (100 over a 90.00/89.96 stop is 2499.9999999996094), so the answer is
    # settled by asking _within about the sizes around it.
    n = max(0, math.floor(amount / risk(1)))
    while _within(risk(n + 1), amount, at_least=False):
        n += 1
    while n > 0 and not _within(risk(n), amount, at_least=False):
        n -= 1
    unit = "contracts" if plan.instrument == "future" else "shares"
    sizes = f"a size of up to {_units(n, unit)} passes" if n else "no size passes"
    figure = risk(plan.size)
    return Result("max_risk_per_trade", _within(figure, amount, at_least=False), figure, limit,
                  f"risk {_shown(figure, amount, at_least=False, money=True)} {cur} against a "
                  f"limit of {limit}; {sizes}", cur)


def _order_required(name: str, plan: Plan, required: bool, price: float | None,
                    reward: bool) -> Result:
    """stop_required and target_required: present, and on the right side of the entry."""
    word = "target" if reward else "stop"
    limit = json.dumps(required)
    if not required:
        return Result(name, True, None, limit, f"the rules do not require a {word}", None)
    if price is None:
        return Result(name, False, None, limit, f"the plan has no {word}", None)
    # For a long the stop sits below the entry and the target above it; for a short the other
    # way round.
    right_side = (_signed(plan.side, plan.entry, price) if reward
                  else _signed(plan.side, price, plan.entry)) > 0
    side_of_entry = "the reward side" if reward else "the risk side"
    return Result(
        name, right_side, None, limit,
        f"the {word} {price:g} is {_side_word(price, plan.entry)} the entry {plan.entry:g}, "
        f"which is {'' if right_side else 'not '}{side_of_entry} for a {plan.side}", None)


def _allowed(name: str, word: str, value: str, allowed: list) -> Result:
    ok = value in allowed
    listed = ", ".join(allowed)
    return Result(name, ok, None, listed,
                  f"the plan's {word} {value} is {'' if ok else 'not '}listed: {listed}", None)


def _trading_hours(plan: Plan, v: dict, ctx: Context) -> Result:
    local = ctx.at.astimezone()
    day = _DAYS[local.weekday()]
    days = v.get("days", list(_DAYS))
    start, end = dt.time.fromisoformat(v["start"]), dt.time.fromisoformat(v["end"])
    ok = start <= local.time() < end and day in days
    limit = f"{v['start']}-{v['end']}" + ("" if set(days) == set(_DAYS) else f" {','.join(days)}")
    return Result("trading_hours", ok, None, limit,
                  f"{local:%H:%M %Z} on a {day} is {'inside' if ok else 'outside'} {limit}", None)


# ------------------------------------------------------- the run as of at ---
def _before(o: Other, ctx: Context) -> bool:
    return o.entered < ctx.at or (o.entered == ctx.at and (ctx.seq is None or o.seq < ctx.seq))


def _ended(o: Other, at: dt.datetime) -> bool:
    return o.ended is not None and o.ended <= at


def _prior(ctx: Context) -> list[Other]:
    return [o for o in ctx.others if _before(o, ctx)]


def _day_exits(ctx: Context) -> list[Other]:
    """The exits that ended on `at`'s day, in the system timezone, by `at`."""
    day = ctx.at.astimezone().date()
    return [o for o in _prior(ctx)
            if o.exited and _ended(o, ctx.at) and o.ended.astimezone().date() == day]


def _max_open_trades(plan: Plan, limit: int, ctx: Context) -> Result:
    # The figure the limit is compared against is the book WITH this trade on it, or "1 open
    # against a limit of 1" would read as a pass beside ok=False.
    n = sum(not _ended(o, ctx.at) for o in _prior(ctx))
    return Result("max_open_trades", n + 1 <= limit, float(n), str(limit),
                  f"{_units(n, 'trades')} open; this would make {n + 1} against a limit of "
                  f"{limit}", "trades")


def _max_trades_per_day(plan: Plan, limit: int, ctx: Context) -> Result:
    day = ctx.at.astimezone().date()
    n = 1 + sum(o.entered.astimezone().date() == day for o in _prior(ctx))
    return Result("max_trades_per_day", n <= limit, float(n), str(limit),
                  f"this is trade {n} of {day.isoformat()} against a limit of {limit}", "trades")


def _max_consecutive_losses(plan: Plan, limit: int, ctx: Context) -> Result:
    streak = 0
    for o in sorted(_day_exits(ctx), key=lambda o: (o.ended, o.seq), reverse=True):
        if o.cash > 0:
            break
        streak += o.cash < 0   # a breakeven is skipped
    day = ctx.at.astimezone().date().isoformat()
    return Result("max_consecutive_losses", streak < limit, float(streak), str(limit),
                  f"{_units(streak, 'losing exits')} in a row on {day} against a limit of {limit}",
                  "losses")


def _daily_loss_limit(plan: Plan, v: dict, ctx: Context) -> Result:
    x = v["R"]
    exits = _day_exits(ctx)
    known = math.fsum(o.r for o in exits if o.r is not None)
    no_r = [o for o in exits if o.r is None]
    kept = _within(known, -x, at_least=True)   # exactly -x R is kept
    # An exit without R could only move the total its cash's way.
    could = [o for o in no_r if (o.cash < 0 if kept else o.cash > 0)]
    ok = None if could else kept
    day = ctx.at.astimezone().date().isoformat()
    shown = _shown(known, -x, at_least=True, signed=True)
    fact = f"the realised R of {day} is {shown} against a floor of -{x} R"
    if no_r:
        lower, higher = any(o.cash < 0 for o in no_r), any(o.cash > 0 for o in no_r)
        total = ("not known" if lower and higher else f"at most {shown}" if lower
                 else f"at least {shown}" if higher else shown)
        left_out = [f"{o.id} exited with no R and {o.cash:+g} in cash" for o in no_r]
        fact = (f"the known realised R of {day} is {shown}; {'; '.join(left_out)}; so the "
                f"day's total is {total}, against a floor of -{x} R")
    return Result("daily_loss_limit", ok, known, f"{x} R", fact, "R")


def _cooldown_after_loss(plan: Plan, v: dict, ctx: Context) -> Result:
    minutes, same = v["minutes"], v.get("same_ticker", False)
    mine = canonical_ticker(plan.ticker)[0]

    def in_scope(o: Other) -> bool:
        return not same or canonical_ticker(o.ticker)[0] == mine

    losses = [o for o in _prior(ctx)
              if o.exited and _ended(o, ctx.at) and o.cash < 0 and in_scope(o)]
    scope = f" on {mine}" if same else ""
    limit = f"{minutes} min" + (" on the same ticker" if same else "")
    figure = None
    fact = f"no loss{scope} before this trade"
    if losses:
        last = max(losses, key=lambda o: (o.ended, o.seq))
        figure = (ctx.at - last.ended).total_seconds() / 60
        fact = (f"the last loss{scope}, {last.id}, exited "
                f"{_shown(figure, minutes, at_least=True, places=1)} min before, against a "
                f"cooldown of {minutes} min")
        if not _within(figure, minutes, at_least=True):
            return Result("cooldown_after_loss", False, figure, limit, fact, "min")
    return Result("cooldown_after_loss", True, figure, limit, fact, "min")


# --------------------------------------------------- indicators and facts ---
def _read(key: str, ctx: Context) -> tuple[float | None, str]:
    """The value a check uses: the agent's, else (None, what is missing)."""
    if key in ctx.market:
        value, src = ctx.market[key], "supplied"
    else:
        return None, f"supply {key}, read off the chart at the bar acted on"
    if not math.isfinite(value):
        return None, f"{key} is {value}, not a finite number: supply {key}"
    return value, src


def _limit(cmp: dict) -> str:
    ((how, x),) = cmp.items()
    return f"between {x[0]} and {x[1]}" if how == "between" else f"{how} {x}"


def _compared(figure: float, cmp: dict) -> tuple[bool, str]:
    """(kept, the figure with the digits that decide it)."""
    ((how, x),) = cmp.items()
    if how == "between":
        lo, hi = x
        nearer = (lo, True) if figure < (lo + hi) / 2 else (hi, False)
        return (_within(figure, lo, at_least=True) and _within(figure, hi, at_least=False),
                _shown(figure, *nearer))
    least = how == "at_least"
    return _within(figure, x, least), _shown(figure, x, least)


def _indicator(plan: Plan, rule: str, label: str, keys: tuple[str, ...], compare: dict,
               ctx: Context) -> Result:
    """An indicator check, or a fact (rule = label = its name, keys = (its name,))."""
    cmp = compare.get(plan.side) if any(s in compare for s in ("long", "short")) else compare
    if cmp is None:   # kept, and no market key is read
        return Result(label, True, None, "none", f"{rule} sets no comparison for a {plan.side}",
                      None)
    if rule in ("ema", "sma"):
        (line, line_src), (close, close_src) = _read(keys[0], ctx), _read(keys[1], ctx)
        if line is None or close is None:
            why = "; ".join(src for v, src in ((line, line_src), (close, close_src)) if v is None)
            return Result(label, None, None, cmp, why, None)
        return Result(label, close > line if cmp == "above" else close < line, close - line, cmp,
                      f"{keys[1]} {close} ({close_src}) is {_side_word(close, line)} {keys[0]} "
                      f"{line} ({line_src})", None)
    limit, unit = _limit(cmp), "ATR" if rule == "stop_atr" else None

    def unknown(why: str) -> Result:
        return Result(label, None, None, limit, why, unit)

    value, src = _read(keys[0], ctx)
    if rule != "stop_atr":
        if value is None:
            return unknown(src)
        ok, shown = _compared(value, cmp)
        return Result(label, ok, value, limit, f"{keys[0]} is {shown} ({src}), against {limit}",
                      unit)
    distance, why = _stop_distance(plan, "the stop in ATR")
    if distance is None:   # the plan's half first: it is the half the owner can fix
        return unknown(why)
    if value is None:
        return unknown(src)
    if value <= 0:
        return unknown(f"{keys[0]} is {value:g}, so the stop cannot be measured in ATR")
    ok, shown = _compared(distance / value, cmp)
    return Result(label, ok, distance / value, limit,
                  f"the stop is {shown} ATR from the entry, on {keys[0]} {value} ({src}), "
                  f"against {limit}", unit)


_RULES = {
    "min_reward_risk": _min_reward_risk,
    "max_risk_per_trade": _max_risk_per_trade,
    "stop_required": lambda p, v, c: _order_required("stop_required", p, v, p.stop, False),
    "target_required": lambda p, v, c: _order_required("target_required", p, v, p.target, True),
    "sides": lambda p, v, c: _allowed("sides", "side", p.side, v),
    "instruments": lambda p, v, c: _allowed("instruments", "instrument", p.instrument, v),
    "max_open_trades": _max_open_trades,
    "max_trades_per_day": _max_trades_per_day,
    "max_consecutive_losses": _max_consecutive_losses,
    "daily_loss_limit": _daily_loss_limit,
    "cooldown_after_loss": _cooldown_after_loss,
    "trading_hours": _trading_hours,
}
# the rules that read ctx.others: a version naming none of them needs no other trade read
RECORD = ("max_open_trades", "max_trades_per_day", "max_consecutive_losses", "daily_loss_limit",
          "cooldown_after_loss")


def _stated(name: str, limit: str, compute: Callable[[], Result]) -> Result:
    """compute()'s result, read unknown when its figure is not a finite number or overflows on the
    way (a risk past a float's range, the size that passes on a vanishing one): every figure kept
    is finite, and a stored one is JSON, which holds no inf."""
    try:
        r = compute()
    except OverflowError:
        r = Result(name, None, math.inf, limit, "", None)
    if r.figure is None or _finite(r.figure):
        return r
    return replace(r, ok=None, figure=None,
                   says=f"its figure is too large to state, so {name} cannot be checked")


def run_checks(plan: Plan, rules: Rules, ctx: Context) -> list[Result]:
    """Every rule the version names, in the library's ORDER (an array in file order, facts in file
    order), then any stored key the library does not know, which reads unknown: never skipped."""
    values = rules.values
    found = indicators(values, rules.timeframes)
    results = []
    for rule in ORDER:
        if rule not in values:
            continue
        if rule in INDICATORS:
            results += [_stated(i.label, json.dumps(i.compare), partial(
                _indicator, plan, i.rule, i.label, i.keys, i.compare, ctx))
                for i in found if i.rule == rule]
        elif rule == "facts":
            results += [_stated(name, json.dumps(cmp), partial(
                _indicator, plan, name, name, (name,), cmp, ctx))
                for name, cmp in values["facts"].items()]
        else:
            results.append(_stated(rule, json.dumps(values[rule]),
                                   partial(_RULES[rule], plan, values[rule], ctx)))
    return results + [Result(name, None, None, json.dumps(v),
                             f"this desk checks no rule named {name}", None)
                      for name, v in values.items() if name not in ORDER]
