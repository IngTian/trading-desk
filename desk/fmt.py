"""Pure text formatters for money, percentages, and shares.

No colour, markup or Rich imports — usable from the CLI, tests, and reports.
"""
from __future__ import annotations

NA = "—"          # not applicable. Never an empty cell.


def money(value: float | None) -> str:
    return NA if value is None else f"{value:,.2f}"


def _flat(value: float, places: int = 2) -> float:
    """Snap to exactly zero anything that cannot be distinguished from it at `places`.

    A FIGURE MUST NOT CLAIM A SIGN IT CANNOT SHOW A VALUE FOR. A flat money-market leg
    rendered `▼ -0.00` -- a red down-arrow, a loss -- on a P&L of -0.00017. The cell shows
    two decimals, so every digit it could print was zero while the arrow and the colour both
    said "down". Three callers below read the same number and all three got it wrong, which
    is why the snap lives here rather than in each of them.

    THE NUMBER IS REAL, not float noise, and no arithmetic here produced it. The price feed
    returns SINGLE-PRECISION closes, so a hand-typed two-decimal entry and a fetched mark of
    the very same price are not the same double: float32(25.07) is 25.06999969482422, and 100
    shares of it against an entry of 25.07 is a P&L of -0.0000305. Every genuinely flat row
    carries a residue like that, and which way it points is arbitrary -- so the colour and
    the arrow were being decided by the last bits of a float.
    """
    return 0.0 if abs(value) < 0.5 * 10 ** -places else value


def signed(value: float | None) -> str:
    return NA if value is None else f"{_flat(value):+,.2f}"


def pct(value: float | None, places: int = 2) -> str:
    """A CHANGE, so it carries a sign: +3.98%, -0.11%."""
    return NA if value is None else f"{_flat(value, places):+.{places}f}%"


def share(value: float | None, places: int = 1) -> str:
    """A PROPORTION (percentage), so it carries no sign: 1.4%, 89%.

    NOT a share count (units of stock). This is for portfolio allocation: what percentage
    of total capital is in this position. For share counts, print the integer plainly.

    Separate from pct() because a leading `+` on a portfolio weight reads as a change --
    "+1.4%" looks like the bet grew by 1.4%, when it means the bet IS 1.4% of the book. A
    share cannot be negative, so the sign carries no information and costs a column cell.
    """
    return NA if value is None else f"{value:.{places}f}%"
