"""A strategy is Markdown: front matter with `name`, prose, and exactly one rules block (TOML)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from desk import rules
from desk.errors import Refused

_TIMEFRAME = re.compile(r"[1-9]\d*[mhdw]")
_MINUTES = {"m": 1, "h": 60, "d": 1440, "w": 10080}


@dataclass(frozen=True)
class ParsedStrategy:
    name: str                       # front matter `name`, required
    text: str                       # as given
    normalised: str                 # whitespace runs collapsed, trailing space stripped, \n only
    text_hash: str                  # sha256 of `normalised`
    rules: dict                     # the TOML table without timeframes
    timeframes: tuple[str, ...]     # finest first, e.g. ("15m", "1h")
    checklist: tuple[str, ...]      # the items under `## Checklist`, in order


def minutes(timeframe: str) -> int:
    """A timeframe in whole minutes: "15m" -> 15, "1h" -> 60, "1d" -> 1440, "1w" -> 10080."""
    if not _TIMEFRAME.fullmatch(timeframe):
        raise ValueError(f"{timeframe!r} is not a timeframe such as 15m, 4h or 1d")
    return int(timeframe[:-1]) * _MINUTES[timeframe[-1]]


def normalise(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", ln).rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def name_of(text: str) -> str:
    """The front matter's `name`, read alone: a stored version is read by this, never re-parsed,
    so a parser tightened later cannot make an old version unreadable."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[0].strip() == "---":
        for ln in lines[1:]:
            if ln.strip() == "---":
                break
            key, _, value = ln.partition(":")
            if key.strip() == "name" and value.strip():
                return value.strip().strip("\"'")
    raise Refused(
        "a strategy starts with front matter naming it: a first line '---', then "
        "'name: Your strategy name', then '---'")


def _checklist(text: str) -> tuple[str, ...]:
    """The top-level bullets under `## Checklist`, up to the next heading outside a fence, each
    without a task box ("[ ] "). A Checklist heading written another way, or a section with text
    but no bullets, is refused: either would save with its items dropped."""
    items: list[str] = []
    fence: tuple[str, int] | None = None
    section = 0     # sections seen
    inside = prose = False
    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if fence is not None:
            if re.fullmatch(f"{re.escape(fence[0])}{{{fence[1]},}}", stripped):
                fence = None
            continue
        heading = re.match(r" {0,3}#{1,6} +(.*)", line)
        if opened := re.match(r"(`{3,}|~{3,})", stripped):
            fence = (opened.group(1)[0], len(opened.group(1)))
        elif line.rstrip().lower() == "## checklist":
            section += 1
            if section > 1:
                raise Refused("strategy: there are two '## Checklist' sections; keep one")
            inside = True
        elif heading and heading.group(1).rstrip(" \t:#").lower() == "checklist":
            raise Refused(f"strategy: the heading {stripped!r} is not read as the checklist; "
                          "write it as '## Checklist'")
        elif inside and line.startswith("#") and line[1:].lstrip("#").startswith(" "):
            inside = False
        elif inside and (m := re.match(r"[-*+](?: +(.*))?$", line)):
            item = re.sub(r"^\[[ xX]\](?: +|$)", "", (m.group(1) or "").strip())
            if not item:
                raise Refused("strategy: a checklist item is empty; write text after the bullet "
                              "or delete it")
            if item in items:
                raise Refused(f"strategy: the checklist item {item!r} is written twice; the "
                              "answers are matched by text, so keep one")
            items.append(item)
        elif inside and stripped:
            prose = True
    if prose and not items:
        raise Refused("strategy: the '## Checklist' section has text but no bullet items; write "
                      "each item on a line starting '- '")
    return tuple(items)


def parse(text: str) -> ParsedStrategy:
    """Read a strategy, or raise Refused naming the line the checks could not apply."""
    name = name_of(text)
    found = rules.read_block(text, "strategy")
    if "timeframes" not in found:
        raise Refused(
            'strategy: the rules block needs timeframes, the candle sizes this strategy '
            'reads, for example timeframes = ["1h", "15m"]')
    given = found.pop("timeframes")
    if not isinstance(given, list):
        raise Refused(f'strategy: timeframes: {given!r} is not a list; write it as '
                      'timeframes = ["1h", "15m"]')
    if not given:
        raise Refused('strategy: timeframes: needs at least one candle size, for example '
                      'timeframes = ["1h"]')
    named: dict[int, str] = {}  # minutes -> the one name kept for that candle size
    for f in given:
        if not isinstance(f, str) or not _TIMEFRAME.fullmatch(f):
            raise Refused(
                f"strategy: timeframes: {f!r} is not a timeframe; write a whole number and "
                f'm, h, d or w in quotes, such as "15m", "4h", "1d"')
        # Two names for one size would tie, and "finest first" must not depend on which.
        if (kept := named.setdefault(minutes(f), f)) != f:
            raise Refused(f"strategy: timeframes: {kept} and {f} are the same timeframe; keep one")
    chosen = tuple(named[n] for n in sorted(named))
    rules.validate(found, chosen)
    checklist = _checklist(text)
    norm = normalise(text)
    return ParsedStrategy(
        name=name, text=text, normalised=norm,
        text_hash=hashlib.sha256(norm.encode()).hexdigest(),
        rules=found, timeframes=chosen, checklist=checklist)
