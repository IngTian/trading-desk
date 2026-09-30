"""Pure text renderers. No Textual import, so every one of these is unit-testable.

House rules copied from daylogs/tui/widgets.py, each closing a real hole:

  * Colour is emphasis, NEVER the only signal. A delta carries an arrow as well as
    a colour, so it survives a colourblind reader and a copy-paste into plain text.
  * Markup goes on AFTER width arithmetic. Bar builders return plain text; callers
    colour finished lines. Otherwise the markup tags are counted in the width.
  * GOOD/BAD/WARN are hex, not the names `green`/`red`. Bare `red` resolves to
    #ff0000 and bare `green` to #008000 — harsh and nearly unreadable respectively
    on a dark warm-earth background.
  * A dash, never a blank, for "not applicable". An inference that never landed is
    a state worth seeing.
"""
from __future__ import annotations

GOOD = "#63af7b"
BAD = "#cc5131"
WARN = "#dc9142"
FAINT = "dim"

NA = "—"          # not applicable. Never an empty cell.
FULL = "█"
EMPTY = "·"
MARKER = "┃"


def esc(text: str) -> str:
    """Escape for Textual content markup.

    rich.markup.escape is the wrong tool here: it only escapes a `[` that begins
    something tag-shaped, and `[[` is not an escape in Textual. A backslash is.
    """
    return str(text).replace("\\", "\\\\").replace("[", "\\[")


def mark(text: str, style: str) -> str:
    return f"[{style}]{text}[/{style}]" if style else str(text)


def money(value: float | None) -> str:
    return NA if value is None else f"{value:,.2f}"


def signed(value: float | None) -> str:
    return NA if value is None else f"{value:+,.2f}"


def pct(value: float | None, places: int = 2) -> str:
    """A CHANGE, so it carries a sign: +3.98%, -0.11%."""
    return NA if value is None else f"{value:+.{places}f}%"


def share(value: float | None, places: int = 1) -> str:
    """A PROPORTION, so it carries no sign: 1.4%, 89%.

    Separate from pct() because a leading `+` on a portfolio weight reads as a change --
    "+1.4%" looks like the bet grew by 1.4%, when it means the bet IS 1.4% of the book. A
    share cannot be negative, so the sign carries no information and costs a column cell.
    """
    return NA if value is None else f"{value:.{places}f}%"


def arrow(value: float | None) -> str:
    if value is None or value == 0:
        return ""
    return "▲" if value > 0 else "▼"


def trend_style(value: float | None, *, rising_is_good: bool = True) -> str:
    """Zero is neither good nor bad, and must not be coloured as either."""
    if value is None or value == 0:
        return ""
    good = value > 0 if rising_is_good else value < 0
    return GOOD if good else BAD


def bar(fraction: float, width: int) -> str:
    """A plain-text bar. Returns text only — the caller colours the finished line."""
    width = max(width, 1)
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return FULL * filled + EMPTY * (width - filled)


def rule(width: int, char: str = "─") -> str:
    return char * max(width, 1)


def kv(label: str, value: str, *, width: int = 22) -> str:
    return f"{label:<{width}}{value}"
