"""Theme names and the picker. Borrowed from daylogs/tui/themes.py, deliberately.

The author: "can i switch themes, just like daylogs?" — so this is that widget, adapted rather
than reinvented, because its design decisions were already argued out there and they hold
here for the same reasons.

IT WORKS AT ALL BECAUSE app.tcss CARRIES NO COLOUR LITERALS. Every rule styles with
Textual design tokens — `$surface`, `$accent`, `$text`, `$text-muted`, `$panel`, `$error` —
so `App.theme = name` re-themes every border and background without touching a stylesheet,
and Textual's ~20 maintained themes come for free. Checked before this was written: the
stylesheet has exactly one `#` in it and it is in a comment.

WHAT A THEME DELIBERATELY DOES NOT TOUCH: the signal colours in widgets.py — GOOD, BAD,
WARN, FAINT — and the per-kind key colours in the footer. A gain being green is a fact
about the gain, not about the chrome around it. daylogs draws the same line around its
category palette, and for the same reason: if the colour that means "you lost money"
changes with the theme, it stops meaning that.
"""
from __future__ import annotations

from textual.message import Message
from textual.theme import BUILTIN_THEMES
from textual.widgets import Static

# The desk has always set gruvbox in on_mount. Keeping it as the default means the picker
# changes nothing for anyone who never presses `t`.
DEFAULT = "gruvbox"


def names() -> tuple[str, ...]:
    """Every theme on offer, sorted.

    Read from Textual at call time rather than frozen into a literal, so a Textual upgrade
    that adds a theme offers it without a code change here.
    """
    return tuple(sorted(BUILTIN_THEMES))


def resolve(name: str | None) -> str:
    """A usable theme name, for the config path. Never raises.

    A stale or misspelled name in config.toml falls back to the default. Refusing to start
    would be a cosmetic setting taking the whole desk down, and it is a setting someone
    edits by hand -- the same reasoning as config.profile() falling back on a bad currency.
    """
    if not name:
        return DEFAULT
    return name if name in BUILTIN_THEMES else DEFAULT


# ── the picker ───────────────────────────────────────────────────────────
# IT PREVIEWS RATHER THAN ASKS FOR A NAME. daylogs tried a text prompt with completion
# first, and the note left behind says why it was wrong: it assumes you already know which
# name you want, and the whole difficulty with a theme is that a name tells you nothing --
# you have to see it against the numbers, the arrows and the good/bad colours, which are
# deliberately NOT themed.
#
# So `←`/`→` apply each theme live, `enter` keeps the one on screen, `esc` puts back the one
# you started with. A focused widget in the bottom container, not a modal screen, for the
# reason the feature exists at all: a ModalScreen covers the interface you are previewing.
_SEP = "   "
# Literal marks, not colour: the cursor sits on a name whose own colours are changing under
# it, so highlighting is exactly the signal that cannot be trusted here.
_CURSOR = ("▸", "◂")
# Reserved for the two ellipses, always, so growing the window cannot overshoot the panel.
_ELLIPSIS_RESERVE = 4


def strip(names_, index: int, width: int) -> str:
    """A window of theme names centred on `index`, as wide as `width` allows.

    Plain text, and the arithmetic happens here -- the cursor is marked with characters
    rather than markup precisely so `len()` still measures what reaches the screen.

    Grows rightward first: the next name `→` lands on is the one most worth seeing, and at
    the start of the list there is nothing to the left anyway.
    """
    names_ = list(names_)
    if not names_:
        return ""
    index = max(0, min(index, len(names_) - 1))
    marked = [f"{_CURSOR[0]}{n}{_CURSOR[1]}" if i == index else n
              for i, n in enumerate(names_)]
    budget = max(len(marked[index]), width - _ELLIPSIS_RESERVE)

    lo = hi = index
    out = marked[index]
    while True:
        grew = False
        if hi + 1 < len(names_) and len(out) + len(_SEP) + len(marked[hi + 1]) <= budget:
            hi += 1
            out = out + _SEP + marked[hi]
            grew = True
        if lo - 1 >= 0 and len(out) + len(_SEP) + len(marked[lo - 1]) <= budget:
            lo -= 1
            out = marked[lo] + _SEP + out
            grew = True
        if not grew:
            break

    if lo > 0:
        out = "… " + out
    if hi < len(names_) - 1:
        out = out + " …"
    return out


class ThemePicker(Static):
    """`t`'s surface: a live preview you arrow through.

    Keys are captured in `on_key` with `stop()` + `prevent_default()`, the same way
    InlinePrompt takes `escape`. That is what makes `←`/`→` work at all: they are app-scope
    tab navigation, deliberately NOT priority bindings, so a focused widget's handler runs
    first and can claim them.
    """

    class Chosen(Message):
        def __init__(self, name: str) -> None:
            super().__init__()
            self.name = name

    class Cancelled(Message):
        pass

    can_focus = True

    def __init__(self, **kw) -> None:
        super().__init__("", **kw)
        self._names: tuple[str, ...] = ()
        self._index = 0
        self._restore = DEFAULT
        self.display = False

    @property
    def is_open(self) -> bool:
        return self.display

    @property
    def selected(self) -> str:
        return self._names[self._index] if self._names else DEFAULT

    def open(self, current: str) -> None:
        """Show the picker, positioned on the theme in effect."""
        self._names = names()
        self._restore = current
        self._index = self._names.index(current) if current in self._names else 0
        self.display = True
        self.repaint()
        self.focus()

    def close(self) -> None:
        self.display = False

    def cancel(self) -> None:
        """Put the theme back and go away, WITHOUT asking for focus.

        daylogs learned this one the hard way: the picker's only exit ran from `on_key`,
        which needs focus, and every write key opens a prompt that takes it. That left the
        picker displayed but deaf -- three keys advertised on its own border doing something
        else, and `esc` no longer restoring the theme its subtitle named.

        No `Cancelled` message, deliberately: that is what returns focus to the pane, and
        the prompt that just took focus should keep it.
        """
        self.app.theme = self._restore
        self.close()

    def repaint(self) -> None:
        if not self._names:
            return
        width = self.content_size.width or 80
        self.update(strip(self._names, self._index, width))
        self.border_title = (f"theme › {self.selected}   "
                             f"{self._index + 1} of {len(self._names)}")
        # NAMES THE THEME `esc` PUTS BACK, because that is the one thing you cannot see once
        # every colour on screen has changed.
        self.border_subtitle = (f"← → preview · enter keeps it · "
                                f"esc restores {self._restore}")

    def on_resize(self) -> None:
        """The window is sized from the panel: a width frozen at mount is wrong the first
        time the terminal is resized."""
        if self.display:
            self.repaint()

    def _step(self, delta: int) -> None:
        """Wraps, so the far end of the list is never more than half of it away."""
        self._index = (self._index + delta) % len(self._names)
        self.app.theme = self.selected
        self.repaint()

    def on_key(self, event) -> None:
        if not self.display:
            return
        if event.key in ("left", "right"):
            event.stop()
            event.prevent_default()
            self._step(-1 if event.key == "left" else 1)
        elif event.key == "enter":
            event.stop()
            event.prevent_default()
            name = self.selected
            self.close()
            self.post_message(self.Chosen(name))
        elif event.key == "escape":
            event.stop()
            event.prevent_default()
            self.app.theme = self._restore
            self.close()
            self.post_message(self.Cancelled())
