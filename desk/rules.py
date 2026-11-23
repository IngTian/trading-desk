"""The one ```rules block of a strategy: TOML, validated against the desk's fixed library."""
from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass

from desk import config
from desk.errors import Refused, shown

# A fence opens with 3+ backticks or tildes. The info string must follow it directly: "``` rules"
# (with a space) is a typo to refuse.
_FENCE = re.compile(r"^(`{3,}|~{3,})(.*)$")

ORDER = ("min_reward_risk", "max_risk_per_trade", "stop_required", "target_required", "sides",
         "instruments", "max_open_trades", "max_trades_per_day", "max_consecutive_losses",
         "daily_loss_limit", "cooldown_after_loss", "trading_hours", "stop_atr", "rsi", "macd",
         "ema", "sma", "facts")
INDICATORS = ("stop_atr", "rsi", "macd", "ema", "sma")
_COMPARES = ("at_least", "at_most", "between")
_SIDES = ("long", "short")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_OUTPUTS = ("histogram", "line", "signal")
_DEFAULTS = {"stop_atr": {"length": 14}, "rsi": {"length": 14},
             "macd": {"fast": 12, "slow": 26, "signal": 9, "output": "histogram"},
             "ema": {}, "sma": {}}
_TIME = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
_FACT = re.compile(r"[a-z][a-z0-9_]{0,63}")
_FACT_PREFIXES = ("rsi_", "atr_", "macd_", "ema_", "sma_", "close_", "stop_atr_")
_AT = re.compile(r"\s*\(at (?:line (\d+), column \d+|end of document)\)$")


@dataclass(frozen=True)
class Rules:
    """The rules the checks read: a version's validated table, and its timeframes."""
    values: dict                 # the version's TOML table, timeframes excluded
    timeframes: tuple[str, ...]  # finest first


@dataclass(frozen=True)
class Indicator:
    rule: str
    label: str                  # the result's name
    keys: tuple[str, ...]       # the market keys it reads
    timeframe: str
    settings: dict              # defaults filled
    compare: dict               # as written; for ema/sma {"long": "above", ...}


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance, for telling a mistyped info string from an unrelated one."""
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


def read_block(text: str, origin: str) -> dict:
    """The text's single rules block, parsed as TOML, or Refused with `origin:line` and the fix.

    Refused: no block or more than one, a near-miss fence (```rule, ```Rules, ~~~ rules), a fence
    never closed, text that is not TOML (a key or table set twice included).
    ~~~rules and ````rules are read, as Markdown reads them.
    """
    bodies: list[tuple[int, list[str], int]] = []   # (fence line, body lines, end line)
    fence: tuple[str, int, bool] | None = None      # (char, length, is a rules fence)
    number = 0
    for number, line in enumerate(text.split("\n"), 1):
        stripped = line.strip()
        if fence is not None:
            char, length, is_rules = fence
            if re.fullmatch(f"{re.escape(char)}{{{length},}}", stripped):
                fence = None
                if is_rules:
                    bodies[-1] = (bodies[-1][0], bodies[-1][1], number)
            elif is_rules:
                bodies[-1][1].append(line)
            continue
        opened = _FENCE.match(stripped)
        if opened:
            marks, info = opened.groups()
            is_rules = info == "rules"
            if not is_rules and info and _edit_distance(info.strip().lower(), "rules") <= 2:
                raise Refused(
                    f"{origin}:{number}: malformed rules fence {shown(stripped)} (expected "
                    f"'{marks}rules', with nothing between the fence and 'rules')")
            if is_rules:
                bodies.append((number, [], number))
            fence = (marks[0], len(marks), is_rules)
    if fence is not None:
        raise Refused(f"{origin}:{number}: a fenced block is never closed")
    if len(bodies) != 1:
        raise Refused(
            f"{origin}: a strategy needs exactly one rules block, and this has {len(bodies)}; "
            f"write one fenced block opened with ```rules")
    start, lines, end = bodies[0]
    try:
        return tomllib.loads("\n".join(lines) + "\n")
    except tomllib.TOMLDecodeError as e:
        found = _AT.search(str(e))
        at = start + int(found.group(1)) if found and found.group(1) else end
        raise Refused(f"{origin}:{at}: the rules block is not valid TOML: "
                      f"{_AT.sub('', str(e))}") from None


def _no(path: str, why: str, fix: str = "") -> Refused:
    return Refused(f"strategy: {path}: {why}" + (f"; write it as {fix}" if fix else ""))


def _is_number(v) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def finite(v) -> bool:
    try:
        return math.isfinite(v)
    except OverflowError:   # an int too large for a float
        return False


def _number(path: str, v, *, positive: bool = False) -> None:
    if not _is_number(v):
        raise _no(path, f"{shown(v)} is not a number", f"{path.rsplit('.', 1)[-1]} = 2")
    if not finite(v):
        raise _no(path, "must be finite, and not too large to read")
    if positive and v <= 0:
        raise _no(path, f"{shown(v)} must be above zero")


MAX_SETTING = 1000


def _whole(path: str, v, example: int = 1, most: int | None = None) -> None:
    key = path.rsplit(".", 1)[-1]
    if not isinstance(v, int) or isinstance(v, bool) or v < 1:
        raise _no(path, f"{shown(v)} is not a whole number of 1 or more", f"{key} = {example}")
    if most is not None and v > most:
        raise _no(path, f"{shown(v)} is more than {most}, the most an indicator setting may be",
                  f"{key} = {example}")


def _table(path: str, v, example: str) -> dict:
    if not isinstance(v, dict):
        raise _no(path, f"{shown(v)} is not a table", example)
    return v


def _only(path: str, table: dict, settings: tuple[str, ...]) -> None:
    for key in table:
        if key not in settings:
            raise _no(path, f"{shown(key)} is not a setting of {path.split('[')[0]}; its settings "
                            f"are {', '.join(settings)}")


def _one(path: str, table: dict, *, fact: bool = False) -> None:
    """Exactly one of at_least, at_most or between, alone in the table, with a good value."""
    given = [k for k in table if k in _COMPARES]
    if len(given) != 1 or len(table) != 1:
        raise _no(path, "needs exactly one of at_least, at_most or between")
    key = given[0]
    v = table[key]
    where = f"{path}.{key}"
    if key != "between":
        if fact and isinstance(v, bool | str):
            raise _no(where, f"{shown(v)} is a yes/no or text; a yes/no belongs in the checklist")
        _number(where, v)
    elif (not isinstance(v, list) or len(v) != 2 or not all(_is_number(x) for x in v)
          or not all(finite(x) for x in v)):
        raise _no(where, f"{shown(v)} is not two numbers", "between = [1, 3]")
    elif v[0] > v[1]:
        raise _no(where, f"{shown(v)} has lo above hi", f"between = [{v[1]}, {v[0]}]")


def _compare(path: str, table: dict, *, fact: bool = False) -> None:
    top = {k: table[k] for k in _COMPARES if k in table}
    sides = [k for k in _SIDES if k in table]
    if top and sides:
        raise _no(path, "writes a comparison at the top and under long or short, not both")
    if sides:
        for side in sides:
            sub = _table(f"{path}.{side}", table[side], f"{side} = {{ at_most = 30 }}")
            _one(f"{path}.{side}", sub, fact=fact)
    elif top:
        _one(path, top, fact=fact)
    else:
        raise _no(path, "needs exactly one of at_least, at_most or between, at the top or "
                        "under long and short")


def _indicator(rule: str, path: str, table, timeframes: tuple[str, ...]) -> None:
    table = _table(path, table, f"[{rule}]")
    settings = tuple(_DEFAULTS[rule])
    if rule in ("ema", "sma"):
        _only(path, table, ("length", "timeframe", *_SIDES))
        if "length" not in table:
            raise _no(f"{path}.length", "is required", "length = 50")
        _whole(f"{path}.length", table["length"], 50, MAX_SETTING)
    else:
        _only(path, table, (*settings, "timeframe", *_COMPARES, *_SIDES))
        for key in settings:
            if key in table and key != "output":
                _whole(f"{path}.{key}", table[key], _DEFAULTS[rule][key], MAX_SETTING)
    if "output" in table and table["output"] not in _OUTPUTS:
        raise _no(f"{path}.output", f"{shown(table['output'])} is not an output; use histogram, "
                                    f"line "
                  "or signal", 'output = "histogram"')
    if table.get("timeframe", timeframes[0]) not in timeframes:
        raise _no(f"{path}.timeframe", f"{table['timeframe']} is not one of this strategy's "
                  f"timeframes ({', '.join(timeframes)})", f'timeframe = "{timeframes[0]}"')
    if rule not in ("ema", "sma"):
        _compare(path, table)
    elif not any(k in table for k in _SIDES):
        raise _no(path, "needs long or short, or both", 'long = "above"')
    else:
        for side in _SIDES:
            if side in table and table[side] not in ("above", "below"):
                raise _no(f"{path}.{side}", f"{shown(table[side])} is not above or below",
                          f'{side} = "above"')


def _hours(path: str, table: dict) -> None:
    _only(path, table, ("start", "end", "days"))
    for key in ("start", "end"):
        if key not in table:
            raise _no(f"{path}.{key}", "is required", f'{key} = "09:45"')
        v = table[key]
        if not isinstance(v, str):
            raise _no(f"{path}.{key}", f"{shown(v)} is not a string", f'{key} = "09:45"')
        if not _TIME.fullmatch(v):
            raise _no(f"{path}.{key}", f"{shown(v)} is not a time of day in HH:MM",
                      f'{key} = "09:45"')
    if table["start"] >= table["end"]:
        raise _no(path, "start must be before end; a window that crosses midnight is not "
                        "supported")
    days = table.get("days", list(DAYS))
    if not isinstance(days, list) or not days or any(d not in DAYS for d in days):
        raise _no(f"{path}.days", f"{shown(days)} is not a list of days; use {', '.join(DAYS)}",
                  'days = ["mon", "tue"]')


def _facts(path: str, v) -> None:
    table = _table(path, v, "[facts]\nrvol = { at_least = 2 }")
    if not table:
        raise _no(path, "an empty table holds no fact; write one, such as rvol = { at_least = 2 }, "
                        "or delete the line")
    for name, cmp in table.items():
        where = f"{path}.{name}"
        if not _FACT.fullmatch(name):
            raise _no(where, "a fact's name is lowercase letters, digits and underscores, "
                             "starting with a letter, 64 at most")
        if name in ORDER or name == "timeframes":
            raise _no(where, "that is a rule's name; a fact may not be named like a rule")
        if name.startswith(_FACT_PREFIXES):
            raise _no(where, "a fact may not start with rsi_, atr_, macd_, ema_, sma_, close_ or "
                             "stop_atr_: those are built-in figures")
        if not isinstance(cmp, dict):
            raise _no(where, f"{shown(cmp)} is not a comparison", f"{name} = {{ at_least = 2 }}")
        _only(where, cmp, (*_COMPARES, *_SIDES))
        _compare(where, cmp, fact=True)


def validate(values: dict, timeframes: tuple[str, ...]) -> None:
    """Refuse a rules table (without timeframes) the library cannot check, naming key and fix.

    `timeframes` is the strategy's, finest first."""
    for rule, v in values.items():
        if rule not in ORDER:
            raise Refused(f"strategy: {shown(rule)} is not a rule the desk knows; the rules are "
                          f"{', '.join(ORDER)}")
        if rule == "min_reward_risk":
            _number(rule, v, positive=True)
        elif rule == "max_risk_per_trade":
            table = _table(rule, v, "max_risk_per_trade = { USD = 150, CAD = 200 }")
            if not table:
                raise _no(rule, "needs at least one currency", "max_risk_per_trade = { USD = 150 }")
            for cur, amount in table.items():
                if cur not in config.CURRENCIES:
                    raise _no(f"{rule}.{cur}", "only CAD or USD: the desk converts no currency")
                _number(f"{rule}.{cur}", amount, positive=True)
        elif rule in ("stop_required", "target_required"):
            if not isinstance(v, bool):
                raise _no(rule, f"{shown(v)} is not true or false", f"{rule} = true")
        elif rule in ("sides", "instruments"):
            allowed = _SIDES if rule == "sides" else ("stock", "etf", "future")
            if not isinstance(v, list):
                raise _no(rule, f"{shown(v)} is not a list", f'{rule} = ["{allowed[0]}"]')
            if not v:
                raise _no(rule, "needs at least one entry", f'{rule} = ["{allowed[0]}"]')
            for item in v:
                if item not in allowed:
                    raise _no(rule, f"{shown(item)} is not allowed; use "
                                    f"{', '.join(allowed[:-1])} or {allowed[-1]}")
        elif rule in ("max_open_trades", "max_trades_per_day", "max_consecutive_losses"):
            _whole(rule, v)
        elif rule == "daily_loss_limit":
            table = _table(rule, v, "daily_loss_limit = { R = 2 }")
            if set(table) != {"R"}:
                raise _no(rule, "only R: the limit is in R, the risk of one trade",
                          "daily_loss_limit = { R = 2 }")
            _number(f"{rule}.R", table["R"], positive=True)
        elif rule == "cooldown_after_loss":
            table = _table(rule, v, "cooldown_after_loss = { minutes = 30 }")
            _only(rule, table, ("minutes", "same_ticker"))
            if "minutes" not in table:
                raise _no(f"{rule}.minutes", "is required", "minutes = 30")
            _whole(f"{rule}.minutes", table["minutes"], 30)
            if not isinstance(table.get("same_ticker", False), bool):
                raise _no(f"{rule}.same_ticker", f"{shown(table['same_ticker'])} is not true or "
                                                 f"false",
                          "same_ticker = true")
        elif rule == "trading_hours":
            _hours(rule, _table(rule, v, 'trading_hours = { start = "09:45", end = "11:30" }'))
        elif rule in INDICATORS:
            if v == []:
                raise _no(rule, "an empty list holds no check; write one [[" + rule +
                          "]] table, or delete the line")
            for n, table in enumerate(v if isinstance(v, list) else [v]):
                _indicator(rule, f"{rule}[{n}]" if isinstance(v, list) else rule, table,
                           timeframes)
        else:
            _facts(rule, v)
    seen = set()
    for ind in indicators(values, timeframes):
        if ind.label in seen:
            raise _no(ind.rule, f"two {ind.rule} checks read {ind.label}; write one, with long "
                                "and short")
        seen.add(ind.label)


def indicators(values: dict, timeframes: tuple[str, ...]) -> tuple[Indicator, ...]:
    """The indicator checks in ORDER (a [[rule]] array in file order); `values` is validated."""
    found = []
    for rule in INDICATORS:
        got = values.get(rule, [])
        for table in got if isinstance(got, list) else [got]:
            tf = table.get("timeframe", timeframes[0])
            settings = {k: table.get(k, d) for k, d in _DEFAULTS[rule].items()}
            if rule in ("ema", "sma"):
                settings["length"] = table["length"]
                compare = {k: table[k] for k in _SIDES if k in table}
            else:
                compare = {k: table[k] for k in (*_COMPARES, *_SIDES) if k in table}
            n = settings.get("length")
            if rule == "macd":
                out = {"histogram": "hist"}.get(settings["output"], settings["output"])
                keys = (f"macd_{out}_{settings['fast']}_{settings['slow']}_"
                        f"{settings['signal']}_{tf}",)
            elif rule in ("ema", "sma"):
                keys = (f"{rule}_{n}_{tf}", f"close_{tf}")
            else:
                keys = (f"{'atr' if rule == 'stop_atr' else rule}_{n}_{tf}",)
            label = f"stop_atr_{n}_{tf}" if rule == "stop_atr" else keys[0]
            found.append(Indicator(rule, label, keys, tf, settings, compare))
    return tuple(found)


def market_keys(values: dict, timeframes: tuple[str, ...]) -> tuple[str, ...]:
    """Every indicator key in indicators() order, then the fact names; each listed once."""
    keys = [k for i in indicators(values, timeframes) for k in i.keys]
    return tuple(dict.fromkeys([*keys, *values.get("facts", {})]))
