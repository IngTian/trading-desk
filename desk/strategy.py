"""A strategy is Markdown: front matter with `name`, prose, and exactly one rules block."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from desk import checks, rules
from desk.errors import Refused

_TIMEFRAME = re.compile(r"[1-9]\d*[mhdw]")
_MINUTES = {"m": 1, "h": 60, "d": 1440, "w": 10080}


@dataclass(frozen=True)
class ParsedStrategy:
    name: str                       # front matter `name`, required
    text: str                       # as given
    normalised: str                 # whitespace runs collapsed, trailing space stripped, \n only
    text_hash: str                  # sha256 of `normalised`
    rules: dict[str, str]           # every rule except timeframes, values verbatim
    timeframes: tuple[str, ...]     # finest first, e.g. ("15m", "1h")


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


def parse(text: str) -> ParsedStrategy:
    """Read a strategy, or raise Refused naming the line the checks could not apply."""
    name = name_of(text)
    found = rules.parse_rules_block(text, "strategy")
    for rule, value in found.items():
        if rule in checks.RETIRED:
            raise Refused(
                f"strategy: {rule!r} is not a rule in this version of the desk; delete the line")
        if rule not in checks.RULE_NAMES:
            raise Refused(
                f"strategy: {rule!r} is not a rule the desk can check; the rules are "
                f"{', '.join(sorted(checks.RULE_NAMES))}")
        if rule != "timeframes" and (why := checks.read_value(rule, value)):
            raise Refused(f"strategy: {rule}: {why}")
    if "timeframes" not in found:
        raise Refused(
            "strategy: the rules block needs 'timeframes', the candle sizes this strategy "
            "reads, for example 'timeframes: 1h, 15m'")
    frames = [f.strip() for f in found["timeframes"].split(",")]
    for f in frames:
        if not _TIMEFRAME.fullmatch(f):
            raise Refused(
                f"strategy: timeframes: {f!r} is not a candle size; write a whole number and "
                f"m, h, d or w, such as 15m, 4h, 1d")
    chosen = tuple(sorted(set(frames), key=lambda f: int(f[:-1]) * _MINUTES[f[-1]]))
    norm = normalise(text)
    return ParsedStrategy(
        name=name, text=text, normalised=norm,
        text_hash=hashlib.sha256(norm.encode()).hexdigest(),
        rules={k: v for k, v in found.items() if k != "timeframes"}, timeframes=chosen)
