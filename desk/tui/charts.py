"""Charts, in characters. Pure: they take numbers and return rich.Text.

No Textual, no database, no state -- which means every one of these is testable by asserting
on a string, the same way desk/book.py is testable by asserting on a float.

THERE IS NO PIE CHART HERE, AND THERE WAS. The author: "i think you shouldnt use a pie chart. look
into daylogs, for such pie chart they just stack each on top of each other. i believe it's in
catogery costs." They are right, and daylogs' widgets.py had already written down why -- in
ranked_bars, which does exactly this job for category spend:

    "A pie needs one distinguishable fill per category and a terminal has about eight, so a
     ninth category becomes ambiguous -- and you still need a legend to read any amount.
     These lines carry label, share, amount and rank at once."

That is the whole argument. The pie built here first was aspect-corrected and round and cost
12 rows and 25 columns to say less than four lines of text; worse, it needed an eight-colour
palette and a legend beside it, and the legend was doing all the work. One bar per row needs
NO palette at all, because each row carries its own label. So the palette went with the pie.

What survives is signed: diverging() puts losses and gains either side of one zero column,
which is a different question from part-to-whole and has no ranked-bar equivalent.
"""
from __future__ import annotations

from rich.text import Text

_FULL = "█"
# " " + pct + " " + right-aligned amount. The pct field is 6 rather than 5 because "43.7%" is
# five characters and "100.0%" is six, and the bar must be one width on every row or the
# columns stop lining up -- daylogs' note, and it is right.
_RANKED_TAIL = 1 + 6 + 2 + 13
_NO_SHARE = "—"


def _fit_bar(width: int, label_width: int, tail: int, wanted: int) -> int:
    """How many bar cells fit once the label and the numbers are paid for.

    Returns 0 rather than a stub when there is no room: a two-cell bar carries no information
    and just steals columns from the figures. THE BAR YIELDS TO THE NUMBERS, which is the
    right way round -- a share and a bar are both estimable by eye, and the amount is the one
    thing that is not.
    """
    room = width - label_width - tail
    return 0 if room < 4 else min(wanted, room)


_EIGHTHS = " ▏▎▍▌▋▊▉█"


def cell_bar(pct: float | None, width: int) -> str:
    """A bar for ONE table cell, resolved to an eighth of a column.

    The author, 2026-09-24, on the dashboard's portfolio as plain indented text: "cant we have a
    table or something? some charts? this looks ugly as hell." A table fixes the alignment;
    this is what makes a column of percentages readable at a glance instead of eight numbers
    to compare by eye.

    EIGHTHS RATHER THAN WHOLE CELLS, because these shares are small: at 10 columns for 100%,
    a 1.4% holding and a 6.0% one both round to nothing and one whole cell respectively --
    the same rounding that made `ranked_bars` bump anything real up to one full cell. Partial
    blocks give roughly eighty steps in ten columns, so small weights stay distinguishable
    from each other and from zero.

    None is EMPTY, not a zero-width bar: a weight that is not recorded must not draw as a
    weight of nothing.
    """
    if pct is None or width <= 0:
        return ""
    eighths = max(0, min(round(max(pct, 0.0) / 100.0 * width * 8), width * 8))
    full, rest = divmod(eighths, 8)
    out = _EIGHTHS[-1] * full + (_EIGHTHS[rest] if rest else "")
    return out[:width].ljust(width)


def ranked_bars(items: list[tuple[str, float]], *, width: int,
                label_width: int = 22, bar_width: int = 18,
                unit: str = "", style: str = "") -> list[Text]:
    """`label ████████ 43.7%   11,250.00`, one row per item, in the order given.

    THE PART-TO-WHOLE FORM FOR A TERMINAL, and the replacement for the pie. Each row is
    self-describing, so there is no legend and no colour vocabulary to learn.

    SHARES ARE OF THE POSITIVE TOTAL, never of a signed sum. If one item can go negative --
    and on this desk a bet's P&L can -- dividing by the signed total inflates every other
    share past 100%, because the denominator shrank by the loss. Such a row keeps its amount
    and shows NO share, because a part-to-whole has no meaningful negative slice.
    """
    total = sum(v for _, v in items if v > 0)
    bars = _fit_bar(width, label_width, _RANKED_TAIL + len(unit), bar_width)
    out: list[Text] = []
    for name, value in items:
        row = Text()
        row.append(f"{name[:label_width - 1]:<{label_width}}")
        if value > 0 and total > 0:
            share = value / total
            # AT LEAST ONE CELL for anything real. A holding rounded to nothing reads as a
            # holding the book does not have.
            filled = min(max(1, round(share * bars)), bars)
            tail = f"{share * 100:5.1f}%"
        else:
            filled = 0
            tail = f"{_NO_SHARE:>6}"
        if bars:
            row.append(_FULL * filled, style=style)
            row.append(" " * (bars - filled))
        row.append(f" {tail}  {value:>12,.2f}{unit}")
        out.append(row)
    return out


# spark() WAS HERE, for the per-holding price sparklines on the positions tab. Both went
# together: the author, "you dont need the recent closes in the positions tab. i can see prices
# in tradingview."
#
# Its one rule is worth keeping in mind if a series ever comes back: a MISSING value was
# drawn as a space and never at the baseline, because a gap in the data is not a crash.

# diverging() WAS HERE, for P&L per bet. The author: "for PnL per bets i think you can remove
# that graphs entirely." What it did that a signed column cannot was put losses and gains
# on ONE axis, which is only worth the space when there are several bets to compare.

