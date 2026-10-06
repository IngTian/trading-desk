"""The app shell. Dispatch, prompt handling, undo, confirmation.

Contains no arithmetic — every figure comes from desk/book.py. Structure copied from
daylogs/tui/app.py, including the two habits that matter most:

  * ONE entry point for every key. `action_dispatch` resolves the meaning per active
    pane at press time, so a pane that omits a handler makes the key inert AND drops
    it from the footer, because the footer asks the same resolver.
  * HANDLE FIRST, CLOSE ON SUCCESS. A rejected line keeps your text and shows why in
    the prompt's border subtitle. Closing on submit would throw away what you typed.
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import tempfile

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Header, Input, Static, TabbedContent, TabPane

from .. import book, config, parse
from ..parse import ParseError
from . import keymap
from .footer import KeyFooter
from .panes import BetsPane, DashboardPane, PositionsPane
from .prompt import InlinePrompt
from .themes import ThemePicker
from .themes import resolve as resolve_theme
from .widgets import FAINT, mark

RETRYABLE = (ParseError, book.DeskError, ValueError)

# TAB ORDER, and it is load-bearing: _step_tab walks this and the digit keys are
# numbered from it. The dashboard is first because it is the only pane that answers a
# question you have before deciding anything.
_TAB_SCOPE = {"tab-dashboard": "dashboard", "tab-positions": "positions",
              "tab-bets": "bets"}
_SCOPE_TAB = {v: k for k, v in _TAB_SCOPE.items()}


_HELP_TITLES = {"nav": "Move around", "view": "Change the view",
                "write": "Record something", "danger": "Undo and delete"}


# WHERE THE NET COMES FROM. This replaced a paragraph about how the loss limit was
# measured, which described a wall that no longer exists. What earns the space now is the
# arithmetic behind the one figure on page 1 -- because it is DERIVED, and a derived
# number nobody can reconstruct is a number nobody can check against their broker.
_HELP_NET = (
    "Net is computed, never typed:\n\n"
    "  net = cash + positions at their marks\n\n"
    "Five things move cash and there is no sixth: a deposit or withdrawal, the offset "
    "row, dividends and interest, a currency conversion, and a trade.\n\n"
    "EACH CURRENCY IS HELD NATIVE and only the current balance is translated, at the "
    "latest rate. That is not a shortcut -- translating every past movement at today's "
    "rate cancels the FX gain on the principal, because the cash that left and the "
    "position that arrived are the same figure in the same currency.\n\n"
    "Contributed capital is the exception: it is translated at the rate on the "
    "deposit's own date, because what you put in is history and must not move because "
    "the loonie did.\n\n"
    "If a figure is blank, the reason is printed under it. Nothing is estimated."
)


# THE EXPANSION THE SUBTITLES NO LONGER CARRY. Every prompt grammar is now under 70
# characters so it fits on one line beside the input -- the author: "seems like for desk i need to
# open up ?" -- and the price of that is `@when` and `±` needing a definition somewhere. This
# is somewhere. The terse form is what you read while typing; this is what you read once.
#
# TWO KINDS OF LINE, and the difference is load-bearing. The INDENTED rows are a table, so
# they must fit the overlay's 64-column content box: a wrapped table row puts its tail at
# the LEFT MARGIN, where `@14:32  @today` reads as another field entirely. The prose is one
# logical line per paragraph and is wrapped by Textual at whatever width it gets -- hard
# breaks are worse than no breaks, because a break chosen for 64 columns re-wraps into an
# orphan ("-- and where a symbol is") at any other width. A test enforces the indented half.
_HELP_GRAMMAR = (
    "Every write is one line. The LEADING words are positional; everything else is a "
    "sigil and may go in any order.\n\n"
    "  @when     when it happened     @2026-09-08  @today  @yesterday\n"
    "  !bet      which bet a fill belongs to     !ai-capex-semis\n"
    "  /account  which sleeve it lands in        /tfsa\n"
    "  =         an amount: a fee, a stop price, a bet's allocation\n"
    "  ~         free text, LAST: it absorbs the rest of the line\n"
    "  ±         a sign is meaningful. -4 trims, -1000 withdraws\n\n"
    "Leaving `@` off means today, which is what a fill you are typing nearly always is.\n\n"
    "`tab` completes a ticker, a !bet, an /account and a bet status. Where a symbol is "
    "ambiguous -- CBIL is one fund in New York and another in Toronto -- it CYCLES the "
    "listings instead, because candidates that share a prefix are exactly the ones "
    "completion cannot help with.\n\n"
    "A sigil the prompt does not take is REFUSED, never quietly dropped. `\\!` escapes a "
    "leading sigil, and a sigil mid-word is just a character: 50% off."
)


def _book_lines() -> str:
    """The `desk where` report, for the help overlay. Reads nothing but paths."""
    p = config.book_path()
    cfg = config.config_file()
    size = f"{p.stat().st_size:,} bytes" if p.exists() else "DOES NOT EXIST"
    return "\n".join((
        f"  book    {p}",
        f"          {size}   ({config.book_source()})",
        f"  home    {config.home()}",
        f"  config  {cfg}" + ("" if cfg.is_file() else "   (absent, which is fine)"),
        f"  schema  {config.schema_sql()}",
    ))


class HelpScreen(ModalScreen):
    BINDINGS = [Binding(k, "close", "close") for k in ("escape", "question_mark", "q")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-body"):
            yield Static("desk keys", classes="pane-title")
            for kind, rows in keymap.help_groups().items():
                lines = [mark(_HELP_TITLES.get(kind, kind), "bold")]
                for k in rows:
                    where = "" if k.scope == "app" else f"  ({k.scope})"
                    lines.append(f"  {k.key:<16} {k.label}{mark(where, FAINT)}")
                yield Static("\n".join(lines) + "\n")
            yield Static(mark("The one-line grammar", "bold"))
            yield Static(_HELP_GRAMMAR + "\n")
            yield Static(mark("Where the net comes from", "bold"))
            yield Static(_HELP_NET + "\n")
            # WHICH BOOK, AND WHY THAT ONE. This was `desk where`, and it is here because
            # four things can decide the path -- when a figure looks wrong the first
            # question is which book produced it, and guessing at that is how someone
            # edits the wrong file for twenty minutes.
            yield Static(mark("This book", "bold"))
            yield Static(_book_lines() + "\n")
            yield Static(mark("esc or ? to close", FAINT))

    def action_close(self) -> None:
        self.app.pop_screen()


class DeskApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "desk"
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (100, "-wide")]

    # 60s, and a class attribute so a test can turn it off rather than monkeypatching a
    # timer. Yahoo is an undocumented endpoint being polled from one desktop; a minute is
    # a courteous interval and is already faster than any decision made here.
    # 30 SECONDS, halved on 2026-09-26 at the author's request. Affordable now for a reason that
    # was not true before: the fetch used to hold a write lock for its whole run, so a
    # shorter interval would have made the desk unwritable more of the time rather than
    # merely busier. Ten instruments at 0.4s of pause is 5-10 seconds of work, so the duty
    # cycle is about a quarter and overlapping ticks are skipped outright.
    POLL_SECONDS = 30.0
    POLL_DISABLED = False
    # The held tickers as of the last redraw, so a change can be noticed. None means "not
    # looked yet", which is distinct from "nothing held" -- an empty book must not read as a
    # change on the first draw.
    _held = None

    # RE-RAISE A FAILED PANE RENDER. False in the app, because a reader is better served by
    # a pane that says it could not be drawn than by one that crashes the desk. True under
    # test, because two render bugs this session were swallowed by Textual's message loop
    # and the suite went green against a pane that had silently drawn nothing. See
    # Pane.reload.
    PROPAGATE_RENDER_ERRORS = False

    # HOW AN UNKNOWN TICKER GETS IDENTIFIED. None means book.register_from_quote uses its own
    # default, which asks Yahoo. A test sets this to a function and never touches the network.
    quote_lookup = None

    BINDINGS = [
        Binding(key, action, desc, show=False, priority=prio)
        for key, action, desc, prio in keymap.app_bindings()
    ]

    def __init__(self, con, **kwargs) -> None:
        super().__init__(**kwargs)
        # NO ANIMATION. The author: "switching between tabs feel sluggish and losing frame
        # rates." Measured: 430 ms per tab switch with the default, 113 ms without --
        # and only 2 to 16 SQL queries either way, so it was never the data. Textual
        # animates the Tabs underline, and on a tool whose entire argument is that
        # recording a fill is fast, a 300 ms flourish on every keypress is the wrong
        # trade. Set here rather than by exporting TEXTUAL_ANIMATIONS so it holds
        # however the desk is launched.
        self.animation_level = "none"
        self.con = con
        self.prompt = InlinePrompt(id="prompt")
        self.key_footer = KeyFooter(id="keyfooter")
        self.theme_picker = ThemePicker(id="theme-picker")
        self.undo: list[tuple[str, dict]] = []
        self._confirm = None
        # READ BY THE DASHBOARD HEADER, which is how a poll that announces nothing is
        # still visible. Set on the UI thread only.
        self.fetching = False
        # A HOLDINGS CHANGE THAT ARRIVED WHILE A FETCH WAS RUNNING. Serialised rather than
        # dropped: see _fetch_if_holdings_changed and _fetch_done.
        self._refetch_wanted = False

    # -- clock is injected everywhere, never read inline ---------------------
    def now(self) -> dt.datetime:
        return dt.datetime.now()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="tab-dashboard", id="tabs"):
            with TabPane("1 Dashboard", id="tab-dashboard"):
                yield DashboardPane(id="dashboard")
            with TabPane("2 Positions", id="tab-positions"):
                yield PositionsPane(id="positions")
            with TabPane("3 Bets", id="tab-bets"):
                yield BetsPane(id="bets")
        # One bottom-docked container, not two docked widgets: docking both makes the
        # footer claim the last row and clip the prompt's bottom border.
        with Vertical(id="bottom"):
            yield self.prompt
            yield self.theme_picker
            yield self.key_footer

    def on_mount(self) -> None:
        book.set_actor(self.con, "desk-tui")
        # FROM THE PROFILE, falling back to gruvbox. A stale name in config.toml
        # must not stop the desk opening -- see themes.resolve.
        self.theme = resolve_theme(config.config().get("theme"))
        self.refresh_footer()
        # And on the FIRST tab too. TabActivated does not fire for the initial tab,
        # so without this the very first pane you see is the one pane whose rows you
        # cannot select -- the worst one to get wrong.
        self.call_after_refresh(self._focus_active_pane)
        # EVERY 60 SECONDS, and one straight away. The author: "learn from tokscale that we can
        # have this pulling periodic every 60s or so. so that while i open the desk, i can
        # see the balance in real time."
        #
        # NOT UNDER TEST. run_test() drives its own clock and a live HTTP call inside a
        # test would be slow, flaky and dependent on a market being open; every test that
        # exercises the fetch calls it directly instead.
        if not self.POLL_DISABLED:
            self.set_interval(self.POLL_SECONDS, self._tick)
            self.call_after_refresh(self._tick)

    def _focus_active_pane(self) -> None:
        # NEVER STEAL FOCUS FROM AN OPEN PROMPT. call_after_refresh runs a frame later,
        # which can land after `n` has already opened the prompt -- and then `enter`
        # goes to the table and the line you typed is never submitted.
        #
        # OR FROM THE THEME PICKER, which is the same defect arriving by the same route and
        # was found by the same kind of test: press `t` in the first frames after boot and
        # the queued focus call lands after the picker has taken focus, so `←`/`→` go to the
        # tab bar and change tabs instead of previewing. The picker is left displayed,
        # advertising three keys that no longer reach it.
        if self.prompt.is_open or self.theme_picker.is_open:
            return
        pane = self._active_pane()
        if hasattr(pane, "focus_default"):
            pane.focus_default()

    # -- scope and panes -----------------------------------------------------
    @property
    def scope(self) -> str:
        return _TAB_SCOPE.get(self.query_one("#tabs", TabbedContent).active, "dashboard")

    def _active_pane(self):
        return self.query_one(f"#{self.scope}")

    def handler_for(self, action: str):
        pane = self._active_pane()
        return getattr(pane, f"key_{action}", None) or getattr(self, f"app_{action}", None)

    def refresh_footer(self) -> None:
        pane = self._active_pane()
        hint = pane.status_hint() if hasattr(pane, "status_hint") else ""
        self.key_footer.update_for(self.scope, hint, live=self.handler_for)

    def refresh_all(self) -> None:
        """A write in one pane changes the others: capital moves the dashboard."""
        for pane_id in _TAB_SCOPE.values():
            try:
                self.query_one(f"#{pane_id}").reload()
            except Exception:  # noqa: BLE001 - a pane that will not render must not
                pass           # take the app down; the others are still useful
        self.refresh_footer()
        self._fetch_if_holdings_changed()

    def _fetch_if_holdings_changed(self) -> None:
        """Fetch as soon as the set of held tickers changes. The author: "when i have new positions
        or close positions. auto refresh prices."

        A NEW TICKER HAS NO MARK AT ALL, which is the case this is really for: record a fill
        for something the book has never held and every figure on its row is an em dash until
        a poll happens to come round. Waiting thirty seconds to find out what you just bought
        is worth a round trip.

        ON THE SET, NOT ON EVERY WRITE, and that distinction is load-bearing. The fetch worker
        is `exclusive`, so starting one CANCELS any in flight -- fire on every fill and
        entering four in a row would cancel three fetches and complete none of them. The
        held-ticker set changes rarely and only when it matters.

        Hooked here rather than in each pane because every route arrives here: a fill, a trim,
        a close, `x`, `u`, and moving a leg between bets.
        """
        try:
            held = frozenset(
                r[0] for r in self.con.execute(
                    "SELECT DISTINCT ticker FROM trades WHERE status='open'"))
        except Exception:  # noqa: BLE001 - a read that fails must not break a redraw
            return
        first = self._held is None
        changed = self._held != held
        self._held = held
        # NOT ON THE FIRST CALL. on_mount already kicks a fetch, and firing again here would
        # cancel it before it finished -- the one case where `exclusive` bites.
        if not (changed and not first and not self.POLL_DISABLED):
            return
        # AND NOT WHILE ONE IS IN FLIGHT, which _tick has always checked and this did not.
        # `@work(exclusive=True)` cancels the TASK but cannot stop a running OS thread, so
        # without this the two ran concurrently: both hammering the same undocumented
        # endpoint, each ignoring the other's 0.4s pacing, and the first to finish clearing
        # `self.fetching` while the second was still writing. That overlap is also what put
        # fetch output on the user's terminal -- see _run_prices.
        #
        # REMEMBERED, NOT DROPPED. A new ticker has no mark at all, so the fetch it triggers
        # is the one the user is waiting for; firing it when the current run reports back
        # keeps that promptness without a second run racing the first.
        if self.fetching:
            self._refetch_wanted = True
            return
        self._fetch(announce=False)

    # An open prompt swallows every other key, EXCEPT the two that get you out of it.
    # Without that exception the app could be wedged with no way back: `tab` moved focus
    # off the prompt, so InlinePrompt._on_key no longer saw escape, and dispatch refused
    # everything because the prompt was still open -- escape, q and the digits were all
    # dead and only Ctrl+C ended it.
    _ESCAPES_A_PROMPT = frozenset({"back", "quit"})

    # -- the single key entry point ------------------------------------------
    def action_dispatch(self, key: str) -> None:
        entry = keymap.lookup(key, self.scope)
        if entry is None:
            return
        # ANY OTHER KEY PUTS THE PREVIEWED THEME BACK. The picker owns `←`/`→`/`enter`/`esc`
        # only while it has focus, and every write key opens a prompt that takes it -- which
        # in daylogs left the picker displayed but deaf, still advertising three keys that
        # now did something else and an `esc` that no longer restored what its own subtitle
        # named. Cancelling here means a preview can never outlive the picker.
        if self.theme_picker.is_open and entry.action != "theme":
            self.theme_picker.cancel()
        if self.prompt.is_open and entry.action not in self._ESCAPES_A_PROMPT:
            return
        self._confirm = None   # any other key abandons a pending y/n
        fn = self.handler_for(entry.action)
        if fn is not None:
            fn()
        self.refresh_footer()

    def on_tabbed_content_tab_activated(self, event) -> None:
        # FOCUS THE PANE'S TABLE, not the tab bar. Textual leaves focus on ContentTabs
        # after a switch, and three things were broken by that and only that:
        #
        #   * `enter` is delivered as DataTable.RowSelected, which never fired, so
        #     editing was unreachable however it was implemented
        #   * up/down moved between TABS instead of between rows
        #   * and therefore cursor_coordinate never left (0, 0), so `x` deleted from
        #     the FIRST row no matter which one you believed you had selected
        #
        # The third is the dangerous one: a destructive key acting on a row the user
        # cannot see they have selected.
        self.refresh_footer()
        self._focus_active_pane()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """`enter` on a pane whose FOCUS is a table. The dashboard has its own route.

        DataTable binds `enter` itself, so a focused table beats any app binding -- which is
        why this message exists rather than a keymap entry. Making the binding `priority`
        instead was tried and is not an option: it would take Enter from the PROMPT, which is
        how every line in this tool is submitted.
        """
        if self.prompt.is_open:
            return
        fn = getattr(self._active_pane(), "key_activate", None)
        if fn is not None:
            event.stop()
            fn()
            self.refresh_footer()

    # -- completion vocabularies ---------------------------------------------
    # The prompt is a widget and has no business opening the book, and parse.py has no
    # business knowing what is in it. So the grammar decides WHICH vocabulary applies
    # (parse.complete) and the app supplies WHAT is in it, from here.
    def vocabulary(self, field: str) -> frozenset[str]:
        if field == "ticker":
            return book.known_tickers(self.con)
        if field == "bet":
            return book.known_bets(self.con)
        if field == "account":
            return book.known_accounts(self.con)
        if field == "when":
            return frozenset({"today", "yesterday"})
        if field == "listing":
            return frozenset(getattr(self._active_pane(), "_listings", ()))
        if field == "leg bet":
            # THE NAMES, and an empty string for "no bet" -- so tab can reach detaching
            # rather than leaving it as a blank line you have to know about.
            return frozenset({b.name for b in book.bets(self.con)
                              if book.is_live(b.status)} | {""})
        return frozenset()

    def bets_by_name(self) -> dict[str, str]:
        """Lowercased name AND id -> id, for resolving what `tab` put in the prompt."""
        out = {}
        for b in book.bets(self.con):
            out[b.id.lower()] = b.id
            out[b.name.lower()] = b.id
        return out

    def completions(self, raw: str, caret: int, label: str):
        # `listings` COMES FROM THE PANE, not the book: they are the candidates a lookup just
        # returned for a ticker that names two securities, and nothing has been written yet.
        # That is what `tab` cycles in the `listing` prompt.
        return parse.complete(raw, caret, label,
                              tickers=book.known_tickers(self.con),
                              bets=book.known_bets(self.con),
                              accounts=book.known_accounts(self.con),
                              listings=getattr(self._active_pane(), "_listings", ()))

    def ask_confirm(self, message: str, callback) -> None:
        """A toast plus a one-shot key, not a modal.

        The message always quotes the row it will act on, so a `y` is never a guess
        about which one. Any key other than y/Y cancels, and any OTHER dispatched key
        abandons the pending confirm rather than leaving it armed.
        """
        self._confirm = callback
        self.notify(message, timeout=8)

    def on_key(self, event) -> None:
        """The y/n confirmation, and holding focus on an open prompt."""
        # TAB MUST NOT LEAVE AN OPEN PROMPT. Textual's tab is focus-next, and there is
        # nothing else on this screen worth tabbing to while a line is being typed --
        # whereas moving focus off the prompt used to wedge the app entirely, because the
        # prompt's own escape handler stopped firing.
        if self.prompt.is_open and event.key in ("tab", "shift+tab"):
            event.stop()
            event.prevent_default()
            self.prompt.focus()
            return
        if self._confirm is None or self.prompt.is_open:
            return
        event.stop()
        cb, self._confirm = self._confirm, None
        if event.key in ("y", "Y"):
            cb()
        else:
            self.notify("cancelled", timeout=2)

    # -- prompt submission: handle first, close only on success ---------------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input is not self.prompt:
            return
        event.stop()
        label, value = self.prompt.label, event.value.strip()
        pane = self._active_pane()
        if not value:
            # AN EMPTY SUBMIT IS A CANCEL, so it has to disarm a pending edit exactly as
            # escape does. It did not, and the consequence was silent: `enter` on a row
            # armed the edit, clearing the line and pressing enter closed the prompt with
            # the edit STILL ARMED, and the next `n` or `c` then deleted the row that had
            # been under the cursor and put a different one in its place.
            if hasattr(pane, "cancel_edit"):
                pane.cancel_edit()
            self.prompt.close()
            pane.focus_default()
            self.refresh_footer()
            return
        # SNAPSHOTTED, so a handler may deliberately CHAIN to another prompt. A fill whose
        # ticker names two securities opens the `listing` choice from inside handle_prompt --
        # and this method used to close the prompt unconditionally on success, which shut the
        # question a moment after it was asked. daylogs carries the same guard for the same
        # reason: an uncategorised expense re-opens as "fix category" there.
        before = (self.prompt.is_open, self.prompt.label)
        try:
            pane.handle_prompt(label, value)
        except RETRYABLE as exc:
            self.prompt.show_error(str(exc))
            self.prompt.focus()
            return
        except Exception as exc:   # noqa: BLE001 - surfaced, never swallowed
            self.prompt.show_error(f"{type(exc).__name__}: {exc}")
            self.prompt.focus()
            return
        self.prompt.remember(label, value)
        # Only close the prompt WE WERE GIVEN. If the handler opened a different one, it is
        # waiting on an answer.
        if (self.prompt.is_open, self.prompt.label) != before:
            self.refresh_footer()
            return
        self.prompt.close()
        pane.focus_default()
        self.refresh_footer()

    def on_inline_prompt_cancelled(self, event) -> None:
        # An abandoned edit must not leak into the next create. Without this, `enter`
        # then escape then `n` would delete the row that was being edited.
        pane = self._active_pane()
        if hasattr(pane, "cancel_edit"):
            pane.cancel_edit()
        self._active_pane().focus_default()
        self.refresh_footer()

    # -- app-scope actions ---------------------------------------------------
    def _show(self, scope: str) -> None:
        self.query_one("#tabs", TabbedContent).active = _SCOPE_TAB[scope]

    def app_show_dashboard(self) -> None:
        self._show("dashboard")

    def app_show_positions(self) -> None:
        self._show("positions")

    def app_show_bets(self) -> None:
        self._show("bets")

    def _step_tab(self, delta: int) -> None:
        # One source of tab order, so adding or reordering a tab cannot leave left/right
        # walking a stale list.
        order = list(_TAB_SCOPE)
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.active = order[(order.index(tabs.active) + delta) % len(order)]

    def app_next_tab(self) -> None:
        self._step_tab(+1)

    def app_prev_tab(self) -> None:
        self._step_tab(-1)

    def app_help(self) -> None:
        self.push_screen(HelpScreen())

    def app_back(self) -> None:
        """esc steps back. It never quits.

        IT CLEARS AN ARMED EDIT, and not doing so was a silent delete. `enter` on a position
        arms `_editing` with the fill id; submitting then deletes that fill and re-adds it.
        on_inline_prompt_cancelled below already guards this -- its comment says "without
        this, `enter` then escape then `n` would delete the row that was being edited" -- but
        THAT handler fires on the prompt's own cancel message, and this one is the app-level
        `esc` binding, which is what receives the key whenever focus is not in the prompt (a
        mouse click is enough). So the same escape took a guarded path or an unguarded one
        depending on where focus happened to be, and on the unguarded one the next `n` ran
        the edit branch: the fill under the old cursor was deleted, and the toast said
        "edited". On a single-fill leg delete_fill takes the parent trade too.
        """
        if self.prompt.is_open:
            pane = self._active_pane()
            if hasattr(pane, "cancel_edit"):
                pane.cancel_edit()
            self.prompt.close()

    def app_quit(self) -> None:
        self.exit()

    def app_theme(self) -> None:
        """`t`. Open the picker on the theme currently in effect.

        `self.theme` rather than the config value: they differ after a cancelled preview,
        and the picker must reopen on what is on screen. A second `t` mid-preview does
        nothing rather than re-anchoring, which would make `esc` restore the preview.
        """
        if self.theme_picker.is_open:
            return
        self.theme_picker.open(self.theme)

    def on_theme_picker_chosen(self, event: ThemePicker.Chosen) -> None:
        self.theme = event.name
        saved = config.save_theme(event.name)
        self.refresh_footer()
        self._focus_active_pane()
        self.notify(f"theme {event.name}"
                    + (f" · saved to {saved.name}" if saved
                       else " · this session only, config.toml is not writable"),
                    timeout=5)

    def on_theme_picker_cancelled(self, event: ThemePicker.Cancelled) -> None:
        self.refresh_footer()
        self._focus_active_pane()

    # app_refresh() WAS HERE AND WENT WITH THE `m` KEY. Handlers are resolved by NAME --
    # `handler_for` does getattr(self, f"app_{action}") -- so leaving it would have left a
    # live handler for an action nothing dispatches, and the footer asks `handler_for` whether
    # a key is real. Deleting both keeps those two in step, which is the whole point of
    # generating the footer from the keymap.

    def _tick(self) -> None:
        """The 30-second poll. The author: "so that while i open the desk, i can see the
        balance in real time."

        SILENT. It announces nothing and it does not steal the toast area -- the header
        already says how fresh the marks are, which is the whole signal. A background
        job that interrupts every minute is a background job you turn off.

        SKIPPED WHILE ONE IS IN FLIGHT. Yahoo can take longer than the interval, and two
        overlapping fetches would write the same rows twice and race the connection.
        """
        if self.fetching:
            return
        self._fetch(announce=False)

    def _fetch(self, *, announce: bool) -> None:
        # SET HERE, ON THE UI THREAD, and before the worker starts. A scripted patch moved
        # this into _fetch_worker, where it would have been set on the WORKER thread -- after
        # _fetch returned, so _tick's guard could still pass and start a second run, which is
        # the overlap this whole commit exists to prevent.
        self.fetching = True
        self.refresh_all()          # so the header can say "fetching…"
        if announce:
            self.notify("fetching marks and rates…", timeout=3)
        self._fetch_worker(announce)

    # THE LATCH IS CLEARED IN _fetch_done, which only runs if the worker reports back. If
    # the worker dies -- and before the BaseException fix above, a missing book killed it --
    # `self.fetching` stayed True, both guards returned early forever, and the dashboard read
    # "fetching…" for the rest of the session with marks that never refreshed. A worker that
    # ends any other way must still clear it.
    @work(thread=True, exclusive=True, group="fetch")
    def _fetch_worker(self, announce: bool) -> None:
        """OFF THE UI THREAD, because this one is on a timer now.

        It used to run synchronously on `m`, which was defensible for a keystroke -- you
        pressed it and you waited. On a 60-second interval it is not: every tick would
        freeze the desk for as long as five HTTP requests take, and a tool whose entire
        argument is that recording a fill is fast cannot stall while you type one.

        A THREAD rather than async: prices.py uses urllib, which blocks. Marking it
        `exclusive` in its own group means a manual `m` mid-tick replaces the tick
        instead of running beside it.
        """
        out, err = self._run_prices()
        self.call_from_thread(self._fetch_done, out, err, announce)

    def _fetch_done(self, out: str, err: str | None, announce: bool) -> None:
        self.fetching = False
        self.refresh_all()
        # A HOLDINGS CHANGE ARRIVED MID-FETCH. Honour it now: the guard in
        # _fetch_if_holdings_changed exists to SERIALISE these, not to lose one.
        if self._refetch_wanted:
            self._refetch_wanted = False
            if not self.POLL_DISABLED:
                self._fetch(announce=False)
        if err is not None:
            # A FAILED POLL IS STILL REPORTED. Swallowing it silently would leave the
            # header saying "marked 3 days ago" with no hint that the desk has been
            # trying and failing to fix that every minute since.
            self.notify(f"refresh failed: {err}", timeout=8, severity="warning")
            return
        if announce:
            lines = [ln for ln in out.splitlines() if ln.strip()]
            self.notify(lines[-1] if lines else "refreshed", timeout=5)

    def edit_text(self, initial: str, *, suffix: str = ".md") -> str | None:
        """Hand a block of prose to $EDITOR. Returns the new text, or None if unchanged.

        THE ONE PLACE THIS TOOL LEAVES ITS OWN PROMPT, and only because a paragraph is not
        a line. Every other write here is one line of grammar, which is the whole friction
        argument -- but a bet's thesis is three paragraphs and 1,400 characters, and a
        one-line prompt that replaced it would destroy the reasoning to fix a typo in it.

        None FOR UNCHANGED, distinct from empty. Quitting the editor without saving must be
        a no-op, and an editor that writes back a byte-identical file is the same thing --
        neither should stack an undo entry or claim in a toast that something was recorded.
        Deliberately emptying the note is a real edit and returns "".

        A SEPARATE METHOD SO TESTS CAN REPLACE IT. Suspending the app and spawning a child
        process cannot run under `run_test`, and the logic worth testing is what the pane
        does with the result.
        """
        # FALLS BACK RATHER THAN REFUSING. This used to give up when $EDITOR was unset and
        # tell the user to export one -- which made `e` a dead key on arrival for anyone who
        # has never set it, and the author has not: "why do i need to export EDITOR?" They do not.
        # A tool that works only after you configure your shell for it is a tool with a
        # setup step it never announced, and this project has already shipped three dead
        # keys without adding a fourth on purpose.
        #
        # `vi` is the fallback because POSIX requires it and macOS and every Linux ship it,
        # so the fallback cannot itself be missing. $VISUAL wins over $EDITOR by the usual
        # convention: VISUAL is the full-screen one, which is what prose wants.
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, f"note{suffix}")
            with open(path, "w") as fh:
                fh.write(initial)
            # SUSPENDED, not backgrounded. Textual owns the terminal -- its input loop and
            # the editor's would otherwise fight over the same tty, which shows up as an
            # editor that renders but ignores half the keystrokes.
            with self.suspend():
                subprocess.call([*editor.split(), path])
            with open(path) as fh:
                edited = fh.read()
        return None if edited == initial else edited

    def stack_undo(self, table: str, row: dict) -> None:
        """Remember a DELETED row, to be put back by INSERT."""
        self.undo.append([table, row, None, "insert"])

    def stack_undo_update(self, table: str, row: dict) -> None:
        """Remember a row as it was BEFORE an in-place update, to be put back by UPDATE.

        A separate mode because the two are not interchangeable: pushing a trades
        pre-image through the insert path would try to create a row that still exists and
        fail on the primary key -- undo appearing to work and doing nothing, which this
        project has already shipped once.
        """
        self.undo.append([table, row, None, "update"])

    def superseded_by(self, row_id: int) -> None:
        """Name the row that took the place of the one on top of the stack.

        An edit is delete-then-add, so undoing it means BOTH removing the replacement
        and putting the original back. Recording only the original left the replacement
        behind -- and when SQLite reused the freed rowid, restore hit the primary key
        and the undo failed outright, so `u` after an edit did nothing at all.
        """
        if self.undo:
            self.undo[-1][2] = row_id

    def app_undo(self) -> None:
        if not self.undo:
            self.notify("nothing to undo", timeout=2)
            return
        entry = self.undo.pop()
        table, row, superseded, mode = entry
        try:
            # ONE TRANSACTION, because this is two statements and the first one DESTROYS a
            # row. Unwrapped, a restore that failed after the delete committed left the book
            # worse than before the undo: the superseding row gone, the original not back,
            # and -- since delete_row on a leg's last fill takes the emptied parent trade
            # with it -- possibly a trade with no fills, which check.py rejects and the TUI
            # cannot repair, because `x` on a position with nothing left answers "has no
            # fills to delete". The except below pushes the entry back so the user can try
            # again, which was a promise about state that had already been half-written.
            with book.atomic(self.con):
                if mode == "update":
                    book.restore_update(self.con, table, row)
                else:
                    # Replacement first: the original's id is often the one just freed.
                    if superseded is not None:
                        book.delete_row(self.con, table, superseded)
                    book.restore(self.con, table, row)
        except Exception as exc:   # noqa: BLE001 - a failed undo must not kill the app
            self.undo.append(entry)   # push back rather than consume it
            self.notify(f"could not undo: {exc}", timeout=6, severity="warning")
            return
        self.refresh_all()
        verb = (f"put the {table} row back as it was" if mode == "update"
                else f"reverted the edit to the {table} row"
                if superseded is not None else f"restored the {table} row")
        self.notify(verb, timeout=4)

    def _run_prices(self) -> tuple[str, str | None]:
        """Runs prices.py in-process and returns (its output, an error or None).

        ON A WORKER THREAD, so it must not touch a widget or self.con -- everything it
        needs it opens itself. Returning the error rather than notifying keeps that true:
        Textual's notify is not thread-safe and the caller re-enters the UI thread
        through call_from_thread.
        """
        # A PLAIN IMPORT, for the reason in book.identify_quote: the two-branch version
        # could not find prices from an installed wheel at all.
        from .. import prices as mod

        # NO contextlib.redirect_stdout. It replaces PROCESS-GLOBAL sys.stdout, and this runs
        # on a worker thread -- so two overlapping fetches interleaved their save/restore, the
        # first to finish handed the real terminal back while the second was still printing,
        # and the output landed on the user's shell. The author saw six lines of it after
        # quitting, which is when the TUI stopped overdrawing it. prices.main takes an `emit`
        # callable now: a thread patching global state was the bug, and a lock would only have
        # narrowed the window rather than closing it.
        lines: list[str] = []
        try:
            # 7 DAYS, not 1. Same-day refetches REPLACE rather than append -- the
            # prices primary key is (base, quote, kind, on_date) -- so a window costs
            # no rows and covers a long weekend, which a 1-day window does not.
            mod.main(dry=False, days=7, emit=lines.append)
        except BaseException as exc:   # noqa: BLE001 - handed back, never swallowed
            # BaseException, NOT Exception, and that is a bug fix rather than breadth for its
            # own sake: prices._require_book raises SystemExit when the book is missing, and
            # SystemExit does not derive from Exception. It therefore left this worker, passed
            # through Textual's own `except Exception`, and came out of the event loop -- the
            # desk vanished with a raw traceback and took the open prompt's contents with it.
            #
            # The trigger is ordinary. config.py's own note says the book lives in a synced
            # folder: iCloud evicts it to a placeholder, a backup renames it, a volume
            # detaches. The TUI's connection stays valid on the open inode so the desk keeps
            # working, and then the 30-second poll fires.
            #
            # A worker thread never sees KeyboardInterrupt, so widening this far costs
            # nothing a narrower clause would have kept.
            return "\n".join(lines), f"{type(exc).__name__}: {exc}"
        return "\n".join(lines), None

    def notify(self, message, **kwargs):   # noqa: A003
        """markup=False once here rather than at every call site: every toast
        interpolates stored text, and a `[` in a note would eat the message."""
        kwargs.setdefault("markup", False)
        return super().notify(message, **kwargs)
