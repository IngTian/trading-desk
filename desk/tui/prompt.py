"""The inline prompt. One line at the bottom; every write goes through it.

No forms and no modals, which is the friction argument: registering a fill is `n`,
then one line, then enter. Copied from daylogs/tui/prompt.py.

THE THREE SLOTS of a bordered Input, so a prompt costs zero extra rows:

    ╭─ fill › ─────────────────────────────────────────────────────────────╮
    │ ACME 10 12.34 !example-bet @today @14:32                             │
    ╰─ ticker · qty (negative reduces) · price · !bet · @date · =fee · ~note╯

  border title    the label
  placeholder     a copyable example that is genuinely valid
  border subtitle the grammar — or, on rejection, the error

The error goes in the SUBTITLE, taking the grammar's place, and never in the
placeholder: Textual renders a placeholder only while the input is empty, and the
entire point is that your text survives a rejection.
"""
from __future__ import annotations

from collections import deque

from textual.message import Message
from textual.widgets import Input

from . import keymap
from .widgets import esc

# `listing` is uppercased by the branch below it, so it is not in here.
_CYCLES = ("leg bet",)


class InlinePrompt(Input):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.display = False
        self.label = ""
        self.error = ""
        self._history: dict[str, deque[str]] = {}
        self._hist_pos = 0

    @property
    def is_open(self) -> bool:
        return bool(self.display)

    def open(self, label: str, prefill: str = "") -> None:
        self.label = label
        self.value = prefill
        self.display = True
        self.clear_error()
        self._hist_pos = 0
        self.focus()
        if prefill:
            self.action_end()

    def close(self) -> None:
        self.display = False
        self.value = ""
        self.label = ""
        self.clear_error()

    def clear_error(self) -> None:
        """Restore the three slots: label above, example inside, grammar below."""
        self.error = ""
        hint = keymap.hint_for(self.label)
        self.border_title = f"{self.label} ›" if self.label else ""
        self.border_subtitle = hint.grammar if hint else ""
        self.placeholder = hint.example if hint else ""
        self.remove_class("error")

    def show_error(self, message: str) -> None:
        """Keep the text; say why it was rejected."""
        self.error = message
        self.border_subtitle = esc(message)
        self.add_class("error")

    # -- per-label history, so up-arrow in the fill prompt recalls fills ------
    def remember(self, label: str, value: str) -> None:
        d = self._history.setdefault(label, deque(maxlen=50))
        if not d or d[-1] != value:
            d.append(value)
        self._hist_pos = 0

    def _walk(self, delta: int) -> None:
        d = self._history.get(self.label)
        if not d:
            return
        self._hist_pos = max(0, min(len(d), self._hist_pos + delta))
        self.value = "" if self._hist_pos == 0 else d[-self._hist_pos]
        self.action_end()

    # -- completion ------------------------------------------------------------
    def _complete(self) -> None:
        """`tab` completes the word under the caret against what the book holds.

        The author: "you dont have tab completions it seems" -- said while typing a ticker the
        registry had never heard of, which is the case completion helps least and a clear
        rejection helps most, so both landed together.

        IT WILL NOT GUESS. One match is inserted. Several insert only the prefix they all
        share and list the candidates in the SUBTITLE, where the grammar and the errors
        already live, so completion costs no extra row and no popup. None says so out
        loud rather than doing nothing quietly, because a dead key reads as a broken key
        and this project has already shipped three of those.
        """
        raw, caret = self.value, self.cursor_position
        c = self.app.completions(raw, caret, self.label)
        # THE LABELS WHOSE `tab` STEPS rather than completes. Both have small, fully known
        # pools that the user picks FROM rather than types -- ambiguous listings share a
        # prefix by construction, and a bet name has spaces in it, so prefix completion
        # inserts nothing in either case and the key reads as dead.
        if self.label in _CYCLES:
            pool = sorted(self.app.vocabulary(self.label))
            if not pool:
                return
            here = self.value.strip()
            fold = [x.lower() for x in pool]
            nxt = (fold.index(here.lower()) + 1) % len(pool) if here.lower() in fold else 0
            self.value = pool[nxt]
            self.cursor_position = len(self.value)
            shown = [x or "(no bet)" for x in pool]
            self.border_subtitle = esc(
                f"{nxt + 1} of {len(pool)} — {'  '.join(shown)}")
            return
        if c.field == "listing":
            # CYCLES, IT DOES NOT COMPLETE. The author: "let me tab through the ticker name." The
            # candidates for an ambiguous ticker share a prefix by construction -- CBIL and
            # CBIL.TO -- so prefix completion inserts nothing at all and the key reads as
            # dead. There are never more than a handful, and every one is already on screen
            # in the toast, so stepping is what the user means by tab here.
            pool = sorted(self.app.vocabulary("listing"))
            if not pool:
                return
            here = self.value.strip().upper()
            nxt = (pool.index(here) + 1) % len(pool) if here in pool else 0
            self.value = pool[nxt]
            self.cursor_position = len(self.value)
            self.border_subtitle = esc(
                f"{nxt + 1} of {len(pool)} — {'  '.join(pool)}")
            return
        if not c.field:
            self.border_subtitle = esc("tab completes a ticker, !bet or /account")
            return
        if not c.matches:
            known = sorted(self.app.vocabulary(c.field))
            self.border_subtitle = esc(
                f"no {c.field} starts with {c.prefix!r} — "
                f"{', '.join(known[:6]) if known else 'none recorded'}"
                + ("  …" if len(known) > 6 else ""))
            return

        if len(c.common) > len(c.prefix):
            self.value, self.cursor_position = c.replaced(raw, c.common)
        if len(c.matches) == 1:
            self.clear_error()
        else:
            self.border_subtitle = esc(
                f"{len(c.matches)} {c.field}s — " + "  ".join(c.matches[:8])
                + ("  …" if len(c.matches) > 8 else ""))

    async def _on_key(self, event) -> None:
        """Only the keys Input does not already own."""
        if event.key == "tab":
            # Stopped HERE so Textual's focus-next never runs: moving focus off an open
            # prompt used to wedge the app, because the escape handler below stopped
            # firing and dispatch refused every key while the prompt was open.
            event.stop()
            event.prevent_default()
            self._complete()
        elif event.key == "escape":
            event.stop()
            event.prevent_default()
            self.close()
            self.post_message(self.Cancelled(self))
        elif event.key == "up":
            event.stop()
            event.prevent_default()
            self._walk(+1)
        elif event.key == "down":
            event.stop()
            event.prevent_default()
            self._walk(-1)

    class Cancelled(Message):
        """Escape was pressed. The app restores focus and clears any pending edit.

        A plain Message, NOT an Input.Changed. It subclassed Input.Changed once, whose
        __init__ requires (input, value) -- so constructing it with one argument threw
        TypeError and `esc` took the app down every time it was pressed. Nothing caught
        that because no test had ever pressed escape: the prompt tests all submitted or
        were rejected, and cancelling looked too trivial to be worth a test.
        """

        def __init__(self, prompt: InlinePrompt) -> None:
            super().__init__()
            self.prompt = prompt
