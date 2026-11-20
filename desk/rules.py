"""Read the one ```rules block of a strategy: `name: value` lines, values verbatim."""
from __future__ import annotations

import re
from dataclasses import dataclass

from desk.errors import Refused

# A fence opens with 3+ backticks or tildes. The info string must follow it directly: "``` rules"
# (with a space) is a typo to refuse.
_FENCE = re.compile(r"^(`{3,}|~{3,})(.*)$")


@dataclass(frozen=True)
class Rules:
    """The rules the checks read: name -> value as written."""
    numbers: dict[str, str]


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance, for telling a mistyped info string from an unrelated one."""
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


def parse_rules_block(text: str, origin: str) -> dict[str, str]:
    """The rules in the text's single rules block, in file order, values verbatim.

    Refused, with `origin:line` and the fix: no block or more than one, a near-miss fence
    (```rule, ```Rules, ~~~ rules), a line that is not `name: value`, a rule named twice (the
    last one would silently win and could loosen it), a fence never closed.
    ~~~rules and ````rules are read, as Markdown reads them.
    """
    blocks: list[dict[str, str]] = []
    lines: dict[str, int] = {}
    fence: tuple[str, int, bool] | None = None   # (char, length, is a rules fence)
    number = 0
    for number, line in enumerate(text.split("\n"), 1):
        stripped = line.strip()
        if fence is not None:
            char, length, is_rules = fence
            if re.fullmatch(f"{re.escape(char)}{{{length},}}", stripped):
                fence = None
            elif is_rules and stripped:
                if ":" not in stripped:
                    raise Refused(
                        f"{origin}:{number}: malformed rule '{stripped}' (expected "
                        f"'name: value')")
                name, value = (part.strip() for part in stripped.split(":", 1))
                if name in blocks[-1]:
                    raise Refused(
                        f"{origin}:{number}: {name!r} is set twice (line {lines[name]} says "
                        f"{blocks[-1][name]!r}, this line says {value!r}); keep the one you mean")
                blocks[-1][name] = value
                lines[name] = number
            continue
        opened = _FENCE.match(stripped)
        if opened:
            marks, info = opened.groups()
            is_rules = info == "rules"
            if not is_rules and info and _edit_distance(info.strip().lower(), "rules") <= 2:
                raise Refused(
                    f"{origin}:{number}: malformed rules fence {stripped!r} (expected "
                    f"'{marks}rules', with nothing between the fence and 'rules')")
            if is_rules:
                blocks.append({})
            fence = (marks[0], len(marks), is_rules)
    if fence is not None:
        raise Refused(f"{origin}:{number}: a fenced block is never closed")
    if len(blocks) != 1:
        raise Refused(
            f"{origin}: a strategy needs exactly one rules block, and this has {len(blocks)}; "
            f"write one fenced block opened with ```rules")
    return blocks[0]
