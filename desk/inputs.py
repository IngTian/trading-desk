"""The checks an agent's argument passes before it reaches the record, and the words of
each refusal. Nothing here reads a connection or writes.

The MCP server passes the agent's JSON straight in, so an argument can arrive as any JSON type;
each check refuses it in words that name the fix. Pure: desk/api.py calls these first.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from desk import config
from desk.errors import Refused, shown
from desk.rules import Rules, finite, market_keys


def aware(when, name: str = "now") -> datetime:
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


def at(now: datetime | None) -> str:
    return aware(now).astimezone(UTC).isoformat()


def text(name: str, value) -> str:
    if not isinstance(value, str):
        raise Refused(f"{name} must be text, a string; got {shown(value)}.")
    return value


def version_number(v) -> int | None:
    if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
        raise Refused("version must be a whole number from 1, e.g. 2, or left out; got "
                      f"{shown(v)}.")
    return v


def strings(name: str, value) -> tuple[str, ...]:
    """A list of strings; None is none. A bare string is refused, not read letter by letter."""
    if value is None:
        return ()
    if not isinstance(value, list | tuple) or not all(isinstance(v, str) for v in value):
        raise Refused(f'{name} must be a list of strings, e.g. ["a", "b"], or left out; '
                      f"got {shown(value)}.")
    return tuple(value)


# The two tails of the after-now refusal: a decision is dated when it happened, a report states
# a state the trade was already in.
WHEN_IT_HAPPENED = "is dated when it happened, so give that moment, in UTC."
REPORT_STATE = ("a report states a state the trade was already in, so give the moment it was in "
                "it, in UTC.")


def not_future(when: datetime, now: datetime, says: str) -> None:
    """Refuse an `at` after `now`; `says` completes the reason in words."""
    if when > now:
        raise Refused(f"`at` {when.isoformat()} is after now ({now.isoformat()}); {says}")


def decision_time(mode: str, at: datetime | None, now: datetime, what: str) -> datetime:
    """A live decision defaults to `now` (already an aware time); a replay one never defaults, or
    it would be dated after every bar in the history being replayed."""
    if at is not None:
        when = aware(at, "at")
        noun = {"enter a trade": "an entry", "move an order": "a move"}.get(what, f"a {what}")
        not_future(when, now, f"{noun} {WHEN_IT_HAPPENED}")
        return when
    if mode == "replay":
        raise Refused(f"A replay run needs `at` to {what}: give the moment in the replayed "
                      "history, not the wall clock.")
    return now


def right_side(side: str, entry, stop, target) -> None:
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


def price(name: str, v, *, required: bool = True) -> None:
    if v is None and not required:
        return
    if isinstance(v, bool) or not isinstance(v, int | float) or not finite(v) or v <= 0:
        raise Refused(f"{name} must be a number above zero, e.g. 101.5; got {shown(v)}.")


def size(v) -> None:
    # a size past a float's range would overflow the checks' arithmetic
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0 or not finite(v):
        raise Refused(f"size must be a whole number above zero, e.g. 10; got {shown(v)}.")


def currency(v) -> None:
    if v not in config.CURRENCIES:
        raise Refused(f"currency {shown(v)} is not known; use one of "
                      f"{', '.join(config.CURRENCIES)}.")


def screenshot(text: str) -> tuple[dict, bytes | None, str | None]:
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


def market(rules: Rules, market) -> dict[str, float]:
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


def answers(items: tuple[str, ...], checklist) -> tuple[dict, ...]:
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
