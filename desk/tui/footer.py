"""The generated footer: a status row, then ONE ROW PER GROUP OF KEYS.

Row 1 is the active pane's own status hint. Then one row for what writes, one for what
changes the view, one for getting around — each shedding its own hints independently when
the terminal is narrow, with pinned keys surviving.

STACKED, NOT FLAT, AND THAT WAS THE POINT. The author, 2026-09-24: "you can learn from daylogs to
stack key options instead of laying them flat." The bets tab was rendering this on one line:

    n bet · e note · R rename · a allocation · s status · u undo   t theme · ? keys   q quit

Eleven hints in a row is a wall — every key looks equally important, and finding one means
reading all of them. daylogs hit this first and wrote the finding down: grouping on a single
line and colour-coding the groups was its first attempt and was NOT enough, because the
length is the problem, not the ordering. Three short lines are scannable in a way one long
line is not, at the cost of two rows that the pane above can spare.

SAME HEIGHT ON EVERY TAB, also from daylogs: collapsing a group when it happens to fit
makes the footer's height depend on which tab you are on, so every tab switch reflows the
table above it. A predictable frame is worth the rows.

It asks the app's own resolver whether each key actually has a handler, so it cannot
advertise a dead key. Copied from daylogs/tui/footer.py.
"""
from __future__ import annotations

from textual.widgets import Static

from . import keymap
from .widgets import BAD, FAINT, mark

_GLYPH = {
    "question_mark": "?", "escape": "esc", "enter": "↵",
    "left": "←", "right": "→", "shift+tab": "S-tab",
}
_KIND_STYLE = {"write": "#67acb9", "view": "#9d81b8", "danger": BAD, "nav": FAINT}
_GROUPS = (("write", "danger"), ("view",), ("nav",))
_SEP = " · "


def _hint(k) -> tuple[str, str]:
    """(plain, styled) for one key. BOTH, because markup counts toward len().

    Measuring the styled string makes every fit check wrong by the length of its colour
    codes, and silently drops hints that would have fitted.
    """
    plain = f"{_GLYPH.get(k.key, k.key)} {k.label}"
    return plain, mark(plain, _KIND_STYLE.get(k.kind, ""))


def render_keys(scope: str, width: int, live=None) -> str:
    """Grouped, colour-coded hints that fit `width`, SHEDDING ONE HINT AT A TIME.

    IT USED TO POP WHOLE GROUPS, and that was the bug behind the author's "seems like for desk i
    need to open up ?". Measured before the fix:

        80 columns, every tab:     `? keys · q quit`         -- every write key gone
        100 columns, positions:    `? keys · q quit`         -- same

    Losing one column of room dropped six keys, because the write group is one string and
    `parts.pop()` removed all of it. A footer that answers "what can I do here?" with
    nothing is worse than a narrow footer: it teaches you the footer is not the place to
    look, which is exactly what happened.

    So hints go one by one, from the END -- the pane's own verbs sit first and are what you
    came for, so navigation sheds before them. Pinned keys (`?`, `q`) always survive; they
    sit last, so right-truncation would drop them first, which is backwards.

    ONE LINE PER GROUP NOW, and the shedding is PER LINE rather than across the footer.
    That is not a detail: while the groups shared one line a global drop order was the only
    thing that made sense, but once they are separate rows an over-wide FIRST group could
    not shrink until every later group had been emptied -- so the write row would overflow a
    narrow terminal while the nav row sat empty beneath it. daylogs shipped that bug and
    fixed it the same way.
    """
    keys = [k for k in keymap.for_scope(scope)
            # A key with no handler in this scope is not a key. `live` is the app's own
            # resolver, so the footer and the dispatcher cannot disagree.
            if live is None or live(k.action) is not None]
    by_kind: dict[str, list] = {}
    for k in keys:
        by_kind.setdefault(k.kind, []).append(k)
    groups = [[k for kind in kinds for k in by_kind.get(kind, [])] for kinds in _GROUPS]
    groups = [g for g in groups if g]

    def fit(members: list) -> list:
        """Drop hints from the end of ONE group until that line fits."""
        keep = list(members)
        while keep and len(_SEP.join(_hint(k)[0] for k in keep)) > width:
            droppable = [i for i, k in enumerate(keep) if not k.pin]
            if not droppable:
                # Even the pinned keys do not fit. Drop the line rather than overflow it: a
                # line wider than the terminal corrupts the layout above it, and at a width
                # where `? keys` cannot be drawn there is nothing useful left to show.
                return []
            keep.pop(droppable[-1])
        return keep

    lines = []
    for g in groups:
        members = fit(g)
        if members:
            lines.append(_SEP.join(_hint(k)[1] for k in members))
    return "\n".join(lines)


class KeyFooter(Static):
    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._scope = "positions"
        self._extra = ""
        self._live = None
        self.text = ""      # the last rendered content, so a test can assert on it
                            # without reaching into Textual's internals

    def update_for(self, scope: str, extra: str = "", live=None) -> None:
        self._scope, self._extra, self._live = scope, extra, live
        width = self.size.width or getattr(self.app, "size", None) and self.app.size.width
        keys = render_keys(scope, max((width or 100) - 2, 10), live)
        self.text = f"{extra}\n{keys}" if extra else f"\n{keys}"
        self.update(self.text)

    def on_resize(self) -> None:
        self.update_for(self._scope, self._extra, self._live)
