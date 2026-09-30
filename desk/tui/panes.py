"""The three panes. They render and handle keys. They contain no arithmetic.

Every number here comes from desk/book.py, where it is testable without an app. A
pane that grows arithmetic is a bug — the rule is copied from daylogs and it is the
reason that codebase can test its maths at all.

DataTable cells are rich.Text, never markup strings: DataTable measures column width
from the cell's own render, and a plain str cell IS parsed as markup, so a `[` in
stored text would both vanish and mismeasure.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import os
import textwrap

import textual
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Static

from .. import book, config, parse
from . import charts
from .widgets import BAD, FAINT, GOOD, NA, WARN, arrow, mark, money, pct, share, signed, trend_style

_TEXTUAL_DIR = os.path.dirname(os.path.abspath(textual.__file__))


def _innermost_file(exc: BaseException) -> str:
    tb = exc.__traceback__
    if tb is None:
        return ""
    while tb.tb_next is not None:
        tb = tb.tb_next
    return os.path.abspath(tb.tb_frame.f_code.co_filename)


def _textual_layout_hiccup(exc: BaseException) -> bool:
    """The ONE exception Pane.reload passes straight through, named rather than guessed.

    `Static.update()` can drive a synchronous refresh, and inside that Textual sometimes
    raises `AttributeError: 'NoneType' object has no attribute 'render_strips'` from its own
    layout pass, while a widget is mid-mount. It happens AFTER the content has been set, so
    nothing is lost, and it predates this guard -- the message loop has always logged it and
    carried on.

    MATCHED NARROWLY AND DELIBERATELY SO. The first attempt was a heuristic -- "was the
    innermost frame inside our package?" -- and it sorted real bugs into the wrong bin: a
    wrong cell count passed to DataTable.add_row raises inside Textual too, and that is
    entirely our mistake. One known quirk, identified by its type, its message and its
    origin, is more honest than a rule that quietly forgives a category.
    """
    return (isinstance(exc, AttributeError)
            and "render_strips" in str(exc)
            and _innermost_file(exc).startswith(_TEXTUAL_DIR))


class PaneTable(DataTable):
    """A DataTable that hands `up`/`down` BACK when its cursor is already at the edge.

    THE BUG THIS EXISTS TO FIX, which shipped twice before it was named. A pane whose focus is
    a table, with anything below that table inside a scroll container, cannot reach the
    content below it: DataTable binds up/down and its bindings consume the key whether or not
    the cursor moves. At 100x20 the bets page had six rows of P&L bars past the fold and no
    keystroke that would show them, and the dashboard opened with `net` off screen for the
    same family of reason.

    So at the last row, `down` scrolls the pane instead of doing nothing, and at row 0 `up`
    does the same. The cursor keeps the key everywhere in between, which is what makes `x`,
    `enter` and `s` safe -- they act on a row the user chose.

    Overriding the ACTIONS rather than on_key, because that is where DataTable decides; an
    on_key handler here would run after the binding had already been consumed.
    """

    def _pane_scroll(self) -> VerticalScroll | None:
        node = self.parent
        while node is not None:
            if isinstance(node, VerticalScroll):
                return node
            node = node.parent
        return None

    def _hand_back(self, dy: int) -> bool:
        """Scroll the pane by one row. False when there is no container to scroll."""
        scroll = self._pane_scroll()
        if scroll is None:
            return False
        scroll.scroll_relative(y=dy, animate=False)
        return True

    def action_cursor_down(self) -> None:
        if self.cursor_coordinate.row >= self.row_count - 1 and self._hand_back(1):
            return
        super().action_cursor_down()

    def action_cursor_up(self) -> None:
        if self.cursor_coordinate.row <= 0 and self._hand_back(-1):
            return
        super().action_cursor_up()


def _cell(text: str, style: str = "") -> Text:
    return Text(text, style=style) if style else Text(text)


def _fit(text: str, width: int) -> str:
    """Cut to `width` with an ellipsis, so truncation LOOKS like truncation.

    A DataTable clips silently, which is fine for a slug and wrong for prose: "Semis and Big
    Techs" clipped to "Semis and Big Tech" reads as a name spelled wrong rather than one cut
    short. Free-text bet names arrived in v13 and can be any length, so the cut is explicit.
    """
    return text if len(text) <= width else text[:width - 1].rstrip() + "…"


def _join(lines: list[Text]) -> Text:
    """One Text from many, newline-separated.

    Static.update() takes ONE renderable, and a list of Text is not one. Sections that need
    to be spaced against each other therefore have to be assembled before they are handed
    over, which is the same reason they share a widget at all.
    """
    out = Text()
    for i, line in enumerate(lines):
        if i:
            out.append("\n")
        out.append_text(line)
    return out


def _first_line(text: str | None) -> str:
    """The first line of a note, for a one-line table cell.

    A NOTE IS NOT A CELL. `bets.notes` holds paragraphs -- the 2026-q4-reverse note is
    three of them, roughly 1,400 characters, and the whole thing was being handed to a
    29-column DataTable column. DataTable does not wrap, so the row rendered as one
    enormous line and the five bets below it were pushed off the screen.

    Truncated rather than wrapped: this table is a list, and the place to read a note is
    the note. The cell says a note exists and roughly what it opens with.
    """
    if not text:
        return NA
    first = text.strip().split("\n", 1)[0].strip()
    rest = text.strip()[len(first):].strip()
    return first + (" …" if rest else "")


class Pane(Vertical):
    """Shared shape: a title line, a body, and a reload() the app calls after a write."""

    scope = "positions"
    # WHICH ROW A PROMPT IS ARMED AGAINST. Every one of these must be cleared when the
    # prompt is cancelled or the next prompt acts on the wrong row: leaving `_editing` set
    # meant the next plain `n` silently replaced whatever had been under the cursor, and
    # `_stopping` and `_editing_bet` are the same shape, so they are listed together
    # rather than each remembering to clear itself.
    _ARMED = ("_editing", "_stopping", "_editing_bet", "_pending_fill", "_moving")
    _editing = None       # a fill or flow being edited in place
    _stopping = None      # the leg a stop is being set on
    _editing_bet = None   # the bet an allocation is being set on
    _moving = None        # the leg whose bet is being re-pointed

    def cancel_edit(self) -> None:
        for attr in self._ARMED:
            setattr(self, attr, None)

    @contextlib.contextmanager
    def keeping_the_cursor(self, table: DataTable):
        """Rebuild a table without moving the selection off the row the user chose.

        DataTable.clear() resets cursor_coordinate to (0, 0), and every write calls
        reload(). So the SECOND action in a row landed on whatever was at the top: setting
        a stop, then setting it again, silently wrote to a different leg. That is the same
        defect as `x` deleting row zero -- a key acting on a row the user did not select --
        arriving by a different route, so it is fixed here once for every pane rather than
        in each handler.
        """
        key = None
        if table.row_count and table.is_valid_coordinate(table.cursor_coordinate):
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        try:
            yield
        finally:
            # NO `return` IN HERE, and that was a real defect rather than a style point: a
            # `return` inside `finally` DISCARDS the exception that is propagating through
            # it, so a write that raised inside `with keeping_the_cursor(...)` was swallowed
            # and the user saw an unchanged screen and no message. Both returns are in the
            # helper now, where a return means what it says. Found by ruff's B012 the first
            # time the linter was run on this project -- it had been on the known-defect
            # list, unfixed, since the adversarial audit.
            self._restore_cursor(table, key)

    @staticmethod
    def _restore_cursor(table: DataTable, key) -> None:
        """Put the cursor back on `key`, or as near as the table still allows."""
        if key is None:
            return
        for row in range(table.row_count):
            if table.coordinate_to_cell_key((row, 0)).row_key.value == key:
                table.cursor_coordinate = (row, 0)
                return
        # The row is gone -- deleted, or closed out of the view. Landing on the last
        # row rather than the first is the smaller surprise after a delete.
        if table.row_count:
            table.cursor_coordinate = (min(table.cursor_coordinate.row,
                                           table.row_count - 1), 0)

    @property
    def con(self):
        return self.app.con

    # The pane's title Static, which is where a render failure is reported.
    title_id = ""

    def reload(self) -> None:
        """Render, and SAY SO IF THAT FAILS. Panes override redraw(), never this.

        THREE NAME COLLISIONS IN ONE SESSION, all of them silent:

          * `_closed` -- Textual's MessagePump.__init__ does `self._closed: bool = False`,
            so an instance attribute shadowed the method and the call raised "'bool' object
            is not callable". The positions pane rendered its open legs and stopped, missing
            its whole closed section, and the entire suite stayed green.
          * a local `share = f"..."` shadowed the imported share(), raising "'str' object is
            not callable". The bets table rendered completely EMPTY.
          * this method was briefly called `_render`, which is Textual's OWN
            `Widget._render` -- it returns the widget's Visual. Overriding it to return None
            made Textual raise "'NoneType' object has no attribute 'render_strips'" from its
            layout pass and took out 75 tests at once. That one at least failed loudly.

        A METHOD ON A TEXTUAL WIDGET SHARES A NAMESPACE WITH FIVE BASE CLASSES, and two of
        those three names looked entirely safe. So: check before naming a method after a
        state word or a render verb, and do not trust the message loop to tell you.

        A BLANK TABLE IS THE WORST FAILURE THIS CODEBASE CAN HAVE. The whole design rests on
        never showing a figure it does not have, and a pane that silently renders nothing
        breaks that promise more thoroughly than a wrong number would: a wrong number at
        least invites doubt.

        So the exception is caught, named in the title where the numbers should have been,
        and re-raised under test -- `app.PROPAGATE_RENDER_ERRORS` -- so the suite fails
        loudly rather than asserting against a pane that drew nothing.
        """
        try:
            self.redraw()
        except Exception as exc:                       # noqa: BLE001 - reported, not hidden
            if _textual_layout_hiccup(exc):
                # Textual's own layout pass, after our content was already set. It broke 75
                # tests the moment this method started re-raising indiscriminately, and it
                # is not ours to report or to fix, so it goes back to the message loop
                # exactly as it always did.
                raise
            if getattr(self.app, "PROPAGATE_RENDER_ERRORS", False):
                raise
            if self.title_id:
                with contextlib.suppress(Exception):
                    self.query_one(f"#{self.title_id}", Static).update(
                        mark(f"this pane could not be drawn — "
                             f"{type(exc).__name__}: {exc}", BAD))
            with contextlib.suppress(Exception):
                self.app.notify(f"{type(self).__name__} failed to render — "
                                f"{type(exc).__name__}: {exc}",
                                timeout=12, severity="error")

    def redraw(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def status_hint(self) -> str:
        return ""

    def focus_default(self) -> None:
        table = self.query(DataTable)
        if table:
            table.first().focus()


# --------------------------------------------------------------------------- #
class PositionsPane(Pane):
    scope = "positions"
    title_id = "pos-head"

    def compose(self) -> ComposeResult:
        yield Static("", id="pos-head", classes="pane-title")
        # NO SCROLL CONTAINER AND NOTHING BELOW THE TABLE. A sparkline per holding lived here
        # and the author removed it: "you dont need the recent closes in the positions tab. i can
        # see
        # prices in tradingview." They are right -- a price chart is what a charting tool is for,
        # and this pane's job is what they hold and what it cost. The table scrolls itself
        # again, which is one fewer moving part.
        yield PaneTable(id="pos-table", cursor_type="row", zebra_stripes=False)

    # A ROW KEY THAT IS NOT A TRADE STARTS WITH THIS. Group headers and closed legs are
    # rows in the same DataTable -- it has no row spans -- so `x`, `enter` and `s` would
    # otherwise act on whatever a heading happened to sit above. `_cursor_trade` refuses
    # anything carrying this prefix, and `·` can never begin a trade id, which is a
    # date-slug.
    INERT = "·"

    def on_mount(self) -> None:
        t = self.query_one("#pos-table", DataTable)
        # Widths are budgeted, not guessed: DataTable adds 2 cells of padding per column,
        # so 10 columns cost 20 on top of these, and the sum below is 88 -- exactly the
        # 108 cells a 110-column terminal leaves. (This said 86/106 while the widths
        # summed to 88; the numbers were stale from a 14-wide `leg` and were never
        # re-added, which is what a budget stated in prose rather than asserted gets you.)
        # Overflowing silently clips the
        # RIGHTMOST columns, which is where P&L lives, the one number that must never be
        # half-shown.
        #
        # `acct` AND `ticker` MERGED INTO `leg`, which paid for the grouping. A group
        # heading has to live in a cell, and the widest thing that goes in this one is a
        # bet id, so the column is 18 rather than the 11 an indented "  tfsa CASH.TO"
        # needs. `opened` became `date` and lost the time and 6 cells with it: it holds an
        # open date on an open leg and a close date on a closed one, and the section
        # heading says which.
        #
        # P&L is 11 because "▲ +1,599.72" is 11 and the old 10 would have clipped it the
        # first time a leg made four figures.
        #
        # TWO WIDTHS WERE MEASURED WRONG and the closed section proved it. `qty` at 4 clipped
        # VFV's 192.145 -- a fractional share count, which a DRIP or a dollar-value order
        # produces routinely -- to "192.". And `%` at 6 clipped INTC's +12.62% to "+12.62",
        # dropping the sign of the unit. Both were invisible while the only rows were round
        # lots inside 10%.
        #
        # `ccy` EXISTS BECAUSE THE ROW WAS LYING BY OMISSION. The author: "i think you need to note
        # down which currencies these positions are in right?" -- and the CAT row is the
        # proof: entry 815.5100, last 816.05, value 1,126.64. The prices are USD and the
        # value is CAD, so 1 x 816.05 = 1,126.64 is arithmetic that cannot be right, and the
        # reader has no way to see why. `Position.currency` was already there; only the
        # column was missing. It sits immediately BEFORE the two columns it governs.
        #
        # `tp` PAID FOR IT, and it cost nothing, because NOTHING IN THIS CODEBASE WRITES
        # `trades.target`: one read in book.positions() and no UPDATE anywhere. All seven
        # open legs hold NULL, so the column rendered a full stack of em-dashes that no key
        # could ever fill -- 8 of the 108 cells spent on furniture. Same disease as the two
        # unwritable halves, same cure the author already chose there: delete the reader rather
        # than invent a writer for a field nothing asked for.
        #
        # `value` AND `P&L` CARRY NO CURRENCY IN THE HEADER, and the reason is the whole
        # design of this column. They hold the HOME currency on an open leg and the NATIVE
        # one on a closed leg -- closed legs are deliberately never translated, because a
        # result that happened in USD on its own dates should not be restated at today's
        # rate. Two bases in one column means a header label is not merely unhelpful, it is
        # false for half the table: `value CAD` sits directly above INTC's 4,096.55 USD.
        # So the basis is stated PER ROW, in `ccy`, which is the only place that can be
        # right for every row.
        # `leg` IS 20 NOW, paid for by `ccy` 4->3 and `qty` 8->7. Bet names became free text
        # in v13, so this column holds prose rather than a slug: "Semis and Big Techs" is 19
        # characters and 18 clipped it to "Semis and Big Tech", dropping exactly one letter,
        # which reads as a typo rather than as truncation. `ccy` only ever holds a 3-letter
        # code, and "192.145" -- the widest real quantity, a DRIP fraction -- is 7.
        for label, width in (("leg", 20), ("qty", 7), ("ccy", 3), ("entry", 8),
                             ("last", 7), ("date", 10), ("sl", 6),
                             ("value", 9), ("P&L", 11), ("%", 7)):
            t.add_column(label, width=width)
        self.reload()

    def redraw(self) -> None:
        t = self.query_one("#pos-table", DataTable)
        with self.keeping_the_cursor(t):
            self._fill_table(t)
        self._off_a_heading(t)

    def _off_a_heading(self, t: DataTable) -> None:
        """Never leave the cursor parked where every key is inert.

        Row 0 is now a group heading, so on opening the tab the cursor sat on a row where
        `n`, `x`, `enter` and `s` all did nothing -- and did nothing SILENTLY, which reads
        as the whole pane being broken. keeping_the_cursor restores a remembered row and
        knows nothing about headings, so the nudge happens after it rather than inside it.

        Downwards, and only to an OPEN leg: a closed one accepts no key either.
        """
        if not t.row_count or not t.is_valid_coordinate(t.cursor_coordinate):
            return
        start = t.cursor_coordinate.row
        for row in list(range(start, t.row_count)) + list(range(0, start)):
            key = t.coordinate_to_cell_key((row, 0)).row_key.value
            if key is not None and not key.startswith(self.INERT):
                t.cursor_coordinate = (row, 0)
                return

    def _fill_table(self, t: DataTable) -> None:
        t.clear()
        rows = book.positions(self.con)
        base = book.base_currency(self.con)
        if not rows:
            self.query_one("#pos-head", Static).update(
                mark("no open positions — press n to record a fill", WARN))
            return
        # `or 0.0` HERE WAS A LIE. A leg with no recorded entry contributed nothing to
        # cost and its full market value to value, so this header advertised
        # +11,760.18 (+64.30%) on a book whose real open-leg P&L was +255.58. The
        # DashboardPane already guarded the same hazard; this pane did not, and it is
        # the one the desk opens on.
        #
        # So P&L is computed ONLY over legs that have both sides, and the value line
        # says what it includes. Total value still counts every leg -- a marked
        # position IS worth that much, whatever its cost basis.
        # EVERY total here can be short, and a total that is short must not print as
        # a confident number. `value 0.00  cost 0.00` for a leg the book has an entry
        # price for is two lies at once: one about a figure it knows, one about a
        # figure it does not.
        valued = [r for r in rows if r.value_base is not None]
        priced = [r for r in rows
                  if r.cost_base is not None and r.value_base is not None]
        tot_val = sum(r.value_base for r in valued) if valued else None
        cost = sum(r.cost_base for r in priced) if priced else None
        pnl = (sum(r.value_base for r in priced) - cost) if priced else None

        missing = []
        if any(r.fx is None for r in rows):
            missing.append("no FX rate: "
                           + " ".join(sorted({r.ticker for r in rows if r.fx is None})))
        if any(r.mark is None for r in rows):
            missing.append("no mark: "
                           + " ".join(sorted({r.ticker for r in rows if r.mark is None})))
        if any(r.entry is None for r in rows):
            missing.append("no entry: "
                           + " ".join(sorted({r.ticker for r in rows if r.entry is None})))

        # THE RATE THE TOTALS USED, and it is here because native rows would otherwise close
        # one reconciliation gap by opening another. A bet subtotal is in the home currency
        # and its legs are each in their own, so getting from `815.56 USD + 873.60 USD +
        # 650.90 CAD` to the `2,983.12 CAD` printed above them needs a multiplier -- and
        # until now that multiplier appeared only on tab 1. Being unable to check a total
        # against the rows under it is the whole complaint that started this, one level up.
        #
        # Only the NON-HOME currencies, because `CAD @ 1.0000` in a CAD book is furniture.
        # A currency whose legs have no rate is simply absent: `missing` already names those
        # tickers, and a rate this pane does not have must not be invented -- printing
        # `USD @ 1.0000` would be the parity lie that NULL exists to prevent.
        #
        # A dict loses nothing: fx_rate() is looked up per CURRENCY, not per leg, so every
        # USD leg on the page carries the same number by construction.
        rates = {r.currency: r.fx for r in rows
                 if r.currency != base and r.fx is not None}
        # HOISTED OUT OF THE f-STRING, not for style: a line break inside a replacement
        # field is Python 3.12+ syntax, and `requires-python` promises 3.11. Wrapping it
        # in place is a SyntaxError for the oldest interpreter this package invites.
        pnl_pct = 100 * pnl / cost if cost and pnl is not None else None
        head = (f"{len(rows)} open   value {mark(money(tot_val), 'bold')} {base}   "
                f"cost {money(cost)}   "
                f"{mark(arrow(pnl) + ' ' + signed(pnl), trend_style(pnl))}   "
                f"{mark(pct(pnl_pct), trend_style(pnl))}")
        if rates:
            head += mark("   " + "  ".join(f"{c} @ {v:,.4f}"
                                           for c, v in sorted(rates.items())), FAINT)
        if missing:
            head += mark(f"   totals exclude — {'; '.join(missing)}", WARN)
        self.query_one("#pos-head", Static).update(head)

        # GROUPED BY BET, which the author chose over a `bet` column: "let me link bets and
        # trades" is the tool's stated job, and four of five legs would have shown an em
        # dash in that column. Grouping states the same link and costs no width.
        meta = {b.id: b for b in book.bets(self.con)}
        groups: dict[str | None, list] = {}
        for r in rows:
            groups.setdefault(r.bet_id, []).append(r)

        def opened_of(bet_id):
            b = meta.get(bet_id)
            return (b.opened or "") if b else ""

        # Newest bet first; the unattached legs last, because they are the parking rather
        # than the thesis. The author, on SGOV and CBIL: "this has nothing to do with any bet."
        self._gaps = 0
        for i, bet_id in enumerate(sorted((k for k in groups if k is not None),
                                          key=opened_of, reverse=True)):
            if i:
                self._gap(t)
            self._group(t, bet_id, groups[bet_id], meta.get(bet_id), base=base)
        if None in groups:
            if groups.keys() - {None}:
                self._gap(t)
            self._group(t, None, groups[None], None, base=base)
        self._closed_legs(t)

    def _gap(self, t: DataTable) -> None:
        """A blank row, so a group heading does not sit on the previous group's last leg.

        The author: "you should have some breadthing air between sections. now everything is
        scrambled together." Between WIDGETS that is a CSS margin, but these sections are
        rows of one DataTable -- it has no row spans and no way to space groups -- so the
        gap has to be a row.

        Keyed with the INERT prefix and a counter, because every row key must be unique and
        `x`, `enter` and `s` must all refuse it. `_cursor_trade` already rejects the prefix
        and `_off_a_heading` already steps over it, so a blank row costs no new guard.
        """
        self._gaps = getattr(self, "_gaps", 0) + 1
        t.add_row(*[_cell("")] * len(t.columns), key=f"{self.INERT}gap:{self._gaps}")

    def _group(self, t: DataTable, bet_id, legs, bet, *, base: str) -> None:
        """A heading row for one bet, then its legs indented under it.

        The heading carries a SUBTOTAL of the legs beneath it -- value and P&L, in the home
        currency, computed the same withholding way as the pane header. A group heading
        that showed only a name would make the reader add the rows up.
        """
        valued = [r for r in legs if r.value_base is not None]
        priced = [r for r in legs
                  if r.cost_base is not None and r.value_base is not None]
        sub_val = sum(r.value_base for r in valued) if valued else None
        sub_cost = sum(r.cost_base for r in priced) if priced else None
        sub_pnl = (sum(r.value_base for r in priced) - sub_cost) if priced else None
        t.add_row(
            # THE NAME, NOT THE SLUG. The author, 2026-09-26: "in the bets yes i can change the name
            # to whatever format yet in positions they are linked to the old id." Renaming a
            # bet changed the bets table and left this heading reading `semis-and-big-tech` --
            # the slug, clipped by one character at 18 cells, which is the worst of both.
            # `bet` is the Bet row this group belongs to, so the name is already here.
            _cell(_fit(bet.name if bet else (bet_id or "no bet"), 20),
                  "bold" if bet_id else FAINT),
            _cell(""),
            # A HEADING NAMES THE HOME CURRENCY, and it is the one row here that must. Its
            # legs are native and may be in several currencies, so a subtotal has no choice
            # but to translate -- and an unlabelled 2,983.12 sitting directly above 815.56
            # USD and 650.90 CAD is the exact ambiguity this column was added to kill. The
            # column therefore means one thing on every row: the currency of the money on
            # THIS row.
            _cell(base, FAINT),
            _cell(bet.status if bet else "", GOOD
                  if bet and bet.status in ("live", "weakened") else FAINT),
            _cell(""),
            _cell((bet.opened or "") if bet else "", FAINT),
            _cell(""),
            _cell(money(sub_val) if sub_val is not None else "", "bold"),
            _cell(f"{arrow(sub_pnl)} {signed(sub_pnl)}" if sub_pnl is not None else "",
                  trend_style(sub_pnl)),
            _cell(pct(100 * sub_pnl / sub_cost) if sub_cost and sub_pnl is not None
                  else "", trend_style(sub_pnl)),
            key=f"{self.INERT}bet:{bet_id or 'none'}")
        for r in legs:
            # NATIVE, not `_base`, so the row adds up: qty x last IS the value shown, and
            # `ccy` two columns to the left says which currency all of it is in. See
            # Position.cost_native for why this is also the only version that can be shown
            # when no FX rate is recorded.
            st = trend_style(r.pnl_native)
            t.add_row(
                _cell(f"  {r.account or '?':<4} {r.ticker}",
                      "" if r.account else WARN),
                _cell(f"{r.qty:g}"),
                # FAINT WHEN IT IS THE HOME CURRENCY, plain when it is not. Every row
                # carrying a loud "CAD" in a mostly-CAD book is noise that trains the eye
                # to skip the column, and the column only earns its space on the rows
                # where the answer is surprising. It is still PRINTED on every row -- a
                # blank would be indistinguishable from "not recorded", which in this
                # codebase means something specific and must never be implied.
                _cell(r.currency, FAINT if r.currency == base else ""),
                _cell(f"{r.entry:.4f}" if r.entry is not None else NA,
                      "" if r.entry is not None else WARN),
                _cell(f"{r.mark:.2f}" if r.mark is not None else NA,
                      "" if r.mark is not None else WARN),
                _cell(r.opened or NA, FAINT if not r.opened else ""),
                _cell(f"{r.stop:g}" if r.stop is not None else NA,
                      FAINT if r.stop is None else ""),
                _cell(money(r.value_native)),
                _cell(f"{arrow(r.pnl_native)} {signed(r.pnl_native)}", st),
                _cell(pct(r.pnl_pct_native), st),
                key=r.trade_id)

    def _closed_legs(self, t: DataTable) -> None:
        """Closed legs, under the open ones, GROUPED BY CURRENCY.

        NAMED `_closed_legs` AND NOT `_closed`, which cost an hour. Textual's
        MessagePump.__init__ does `self._closed: bool = False`, so an instance
        attribute shadowed the method and `self._closed(t)` raised "'bool' object is
        not callable" -- inside a message handler, where Textual swallows it. The pane
        rendered its open legs and simply stopped, with no error anywhere. A method on a
        Textual widget shares a namespace with two base classes; check before naming
        one after a state word.

        The author chose these over a fifth tab: "Closed legs on tab 2, below the open ones."
        Until now the desk had no view of a closed trade at all -- eight of them, worth
        C$2,200 of a C$1,790 return, reachable only from sqlite3.

        BY CURRENCY, AND NEVER TRANSLATED. A leg's result happened in its own currency on
        its own dates; restating it at today's rate would fold an FX move into a figure
        about a stock, and the dashboard's `USD → CAD` line is where that belongs.

        THE OPEN LEGS ABOVE ARE NOW NATIVE TOO, which is a change: they were translated,
        and this docstring used to note the difference as a thing the grouping compensated
        for. It no longer has to. Every leg row in this table, open or closed, is stated in
        its own currency and says so in `ccy`; only the bet subtotals and the pane header
        translate, because only they sum across currencies. The grouping survives on its
        own merit -- a per-currency `realised` total must not add USD to CAD.
        """
        legs = book.closed_legs(self.con)
        if not legs:
            return
        banked = book.banked_in_open_legs(self.con)
        by_ccy: dict[str, list] = {}
        for leg in legs:
            by_ccy.setdefault(leg.currency, []).append(leg)
        for ccy in sorted(by_ccy):
            self._gap(t)
            group = by_ccy[ccy]
            got = [x.realised for x in group if x.realised is not None]
            total = sum(got) if got else None
            t.add_row(
                _cell(f"CLOSED · {ccy}", "bold"),
                _cell(str(len(group)), FAINT),
                _cell(ccy, FAINT),
                _cell("realised", FAINT), _cell(""), _cell(""), _cell(""),
                # LABELS THE COLUMN FOR THIS SECTION, because it changes meaning: `value`
                # is market value on an open leg and COST on a closed one, which is the
                # denominator of the % beside it. Saying so here is cheaper than a second
                # column nothing else would use.
                _cell("at cost", FAINT),
                _cell(f"{arrow(total)} {signed(total)}" if total is not None else NA,
                      trend_style(total)),
                _cell(""),
                key=f"{self.INERT}closed:{ccy}")
            # A TRIM BANKS MONEY WITHOUT CLOSING ANYTHING, so this total can be short of
            # the dashboard's `realised` line. Saying so beats letting two figures that
            # differ by C$5.08 look like a bug in one of them.
            if ccy in banked:
                t.add_row(
                    _cell("  banked in open", FAINT), _cell(""), _cell(ccy, FAINT),
                    _cell(""), _cell(""), _cell(""), _cell(""), _cell(""),
                    _cell(signed(banked[ccy]), FAINT), _cell(""),
                    key=f"{self.INERT}banked:{ccy}")
            for leg in group:
                st = trend_style(leg.realised)
                t.add_row(
                    _cell(f"  {leg.account or '?':<4} {leg.ticker}", FAINT),
                    _cell(f"{leg.qty:g}" if leg.qty else NA, FAINT),
                    # REPEATED ON EVERY ROW even though the section heading above says it,
                    # so the column means exactly one thing wherever you look: this row's
                    # native currency. An exception ("blank means read the heading") is a
                    # rule the reader has to hold, and in a book where blank means NOT
                    # RECORDED it is the wrong rule to ask them to hold.
                    _cell(ccy, FAINT),
                    _cell(f"{leg.entry:.4f}" if leg.entry is not None else NA, FAINT),
                    _cell(f"{leg.exit:.2f}" if leg.exit is not None else NA, FAINT),
                    _cell(leg.closed or NA, FAINT),
                    # `sl` IS LEFT EMPTY, and the bet does NOT go here. A stop on a leg
                    # that is already out means nothing, and the free 6 cells are not
                    # enough for a bet id -- "ai-cap" is worse than a blank. Which bet a
                    # closed leg belonged to, and what each bet realised, is a fact about
                    # the BET; it belongs on tab 3 as a column, not clipped in here.
                    _cell(""),
                    _cell(money(leg.cost), FAINT),
                    _cell(f"{arrow(leg.realised)} {signed(leg.realised)}", st),
                    _cell(pct(leg.realised_pct), st),
                    key=f"{self.INERT}leg:{leg.trade_id}")

    def status_hint(self) -> str:
        """THE NO-STOP NAG WAS HERE, and it went with the dashboard's gaps block.

        It listed every leg with no recorded stop -- which on this book was every leg,
        including 165 SGOV and 150 CBIL, money-market funds held to park cash. The author:
        "yes i think in a good product, gaps shouldnt be there at all", and the same
        answer applies wherever the nag was rendered rather than only on tab 1. The `sl`
        column already says which legs have no stop, in the row it concerns, without
        turning it into an accusation.
        """
        rows = book.positions(self.con)
        stale = {r.mark_date for r in rows if r.mark_date}
        bits = [mark("positions", "bold")]
        if stale:
            bits.append(f"marks {min(stale)}")
        if any(r.unconfirmed_fills for r in rows):
            bits.append(mark("unconfirmed fills", WARN))
        return " · ".join(bits)

    def key_fill(self) -> None:
        self.app.prompt.open("fill")

    def key_stop(self) -> None:
        """Record the stop on the leg under the cursor.

        IT NO LONGER PREFILLS THE CURVE'S NUMBER, because there is no curve any more. The
        prefill was the required percentage and the toast graded what you typed against it
        -- useful while the tool enforced a sizing rule, and an opinion it has no standing
        to hold now. It prefills what is ALREADY RECORDED, like every other edit here, so
        the fast path is confirming the number you already chose.

        The stop is still worth recording. Where you decided to get out is a fact about the
        trade, and a recorder that could not hold it would be missing one.
        """
        tid = self._cursor_trade()
        if tid is None:
            return
        row = self.con.execute(
            "SELECT loss_limit_pct, stop FROM trades WHERE id = ?", (tid,)).fetchone()
        if row is None:
            return
        self._stopping = tid
        pre = ""
        if row["loss_limit_pct"] is not None:
            pre = f"{parse._num(row['loss_limit_pct'])}%"
            if row["stop"] is not None:
                pre += f" ={parse._num(row['stop'])}"
        self.app.prompt.open("stop", prefill=pre)

    def handle_stop(self, value: str) -> None:
        tid = self._stopping
        if tid is None:
            return
        r = parse.parse_stop(value)
        pre = book.set_stop(self.con, tid, pct=r.pct, basis=r.basis, price=r.price,
                            on_date=self.app.now().date().isoformat())
        self._stopping = None
        self.app.stack_undo_update("trades", pre)
        self.reload()
        self.app.refresh_all()
        # REPEATS WHAT WAS RECORDED, and passes no judgement on it. This used to append the
        # stop-curve's verdict -- "ok", or "LOOSER THAN THE CURVE by 20.0pp".
        self.app.notify(
            f"{tid}: stop {r.pct:g}% of {r.basis}"
            + (f", order at {r.price:g}" if r.price else "")
            + " · u to undo", timeout=6)

    def _cursor_trade(self) -> str | None:
        """The OPEN trade under the cursor, or None if the cursor is not on one.

        THE ONE GUARD THAT KEEPS THE HEADINGS SAFE. `x`, `enter` and `s` all come through
        here, and the table now holds group headings and closed legs as ordinary rows --
        DataTable has no row spans. Without this, `x` on the "CLOSED · USD" heading would
        look up a trade called that, find nothing, and the failure would be silent; on a
        closed leg it would delete a fill from a finished round trip.
        """
        t = self.query_one("#pos-table", DataTable)
        if not t.row_count or not t.is_valid_coordinate(t.cursor_coordinate):
            return None
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        return None if key is None or key.startswith(self.INERT) else key

    def _newest_fill(self, trade_id: str):
        """The last fill on a trade. A row here is a POSITION, not a fill.

        Acting on the newest is the only choice that needs no drill-down UI, and the
        confirmation names it in full so nothing is guessed on the user's behalf.
        """
        return self.con.execute(
            "SELECT f.id, f.on_date, f.shares, f.price, f.fee, f.notes, "
            "       t.ticker, t.bet_id, t.account_id "
            "FROM fills f JOIN trades t ON t.id = f.trade_id "
            "WHERE f.trade_id = ? ORDER BY f.on_date DESC, f.id DESC LIMIT 1",
            (trade_id,)).fetchone()

    @staticmethod
    def _describe(f) -> str:
        px = f"@ {f['price']:g}" if f["price"] is not None else "at no recorded price"
        return (f"{f['ticker']} {f['shares']:+g} {px}"
                f"{' on ' + f['on_date'] if f['on_date'] else ' (undated)'}")

    def key_trade_bet(self) -> None:
        """Move this leg under a bet, or out. Prefilled with where it is now."""
        tid = self._cursor_trade()
        if tid is None:
            return
        row = self.con.execute("SELECT bet_id FROM trades WHERE id=?",
                               (tid,)).fetchone()
        self._moving = tid
        # PREFILLED WITH THE CURRENT BET so the prompt shows where the leg is before it
        # asks where it should go, and so detaching is a visible act of clearing a field
        # rather than a blank line you have to know means something.
        # PREFILLED WITH THE NAME, not the slug: `tab` cycles names now, so showing an id
        # here would make the first tab look like it had replaced the field with something
        # unrelated.
        b = next((x for x in book.bets(self.con) if x.id == row["bet_id"]), None)
        self.app.prompt.open("leg bet", prefill=b.name if b else "")

    def _move_bet(self, value: str) -> None:
        tid = self._moving
        if tid is None:
            return
        bet = parse.parse_trade_bet(value, known=self.app.bets_by_name())
        pre = book.set_trade_bet(self.con, tid, bet_id=bet)
        self._moving = None
        self.app.stack_undo_update("trades", pre)
        self.reload()
        self.app.refresh_all()
        was = pre["bet_id"] or "no bet"
        self.app.notify(f"{tid}: {was} → {bet or 'no bet'} · "
                        f"every fill moved with it · u to undo", timeout=6)

    def key_delete(self) -> None:
        tid = self._cursor_trade()
        if tid is None:
            return
        f = self._newest_fill(tid)
        if f is None:
            self.app.notify(f"{tid} has no fills to delete", timeout=4)
            return
        # NAMED, not "the selected fill". A position of several fills gives no clue
        # which one this is, and a delete you cannot picture is one you should refuse.
        #
        # AND IT SAYS WHEN THE LEG ITSELF GOES. On a single-fill position this keystroke
        # removes the whole leg -- book.delete_fill takes the emptied trade with it -- and
        # that is a materially bigger act than dropping one of five fills. The confirm has
        # to distinguish them or "y" means two different things on two different rows.
        last = self.con.execute("SELECT COUNT(*) FROM fills WHERE trade_id=?",
                                (tid,)).fetchone()[0] == 1
        self.app.ask_confirm(
            (f"delete {tid} ENTIRELY — {self._describe(f)} is its only fill, "
             f"so the position goes too?   y/n" if last else
             f"delete the newest fill on {tid} — {self._describe(f)}?   y/n"),
            lambda: self._do_delete(f["id"]))

    def _do_delete(self, fill_id: int) -> None:
        # ONE TRANSACTION, because delete_fill is up to three statements -- the fill, a read
        # of the emptied trade's cascade children, and the trade -- and a failure between
        # them leaves a leg the TUI cannot repair. The edit path below already wraps its
        # delete-then-add for the same reason; this one was the bare caller.
        with book.atomic(self.con):
            pre = book.delete_fill(self.con, fill_id)
        if pre is None:
            self.app.notify("that fill is gone", timeout=3)
            return
        self.app.stack_undo("fills", pre)
        self.reload()
        self.app.refresh_all()
        self.app.notify("deleted · u to undo", timeout=5)

    def key_activate(self) -> None:
        """Edit the newest fill: prefill the prompt, replace on submit."""
        tid = self._cursor_trade()
        if tid is None:
            return
        f = self._newest_fill(tid)
        if f is None:
            self.app.notify(f"{tid} has no fills to edit", timeout=4)
            return
        if f["price"] is None:
            # render_fill needs a price to produce a line the parser accepts, and a
            # fill with none is a recorded HOLE -- editing it through this path would
            # quietly invent the missing number.
            self.app.notify(
                f"{tid}'s newest fill has no recorded price, so there is no line to "
                f"edit. Delete it with x and record it fresh.", timeout=8)
            return
        self._editing = f["id"]
        self.app.prompt.open("fill", prefill=parse.render_fill({
            "ticker": f["ticker"], "shares": f["shares"], "price": f["price"],
            "bet_id": f["bet_id"], "on_date": f["on_date"],
            "fee": f["fee"], "account": f["account_id"], "note": f["notes"]}))

    def _resolve_ticker(self, ticker: str) -> dict | None:
        """Known already? Nothing to do. Otherwise verify it and register it.

        Returns the registration when one happened, so the caller can SAY SO -- a row written
        on the user's behalf that nobody mentions is a row nobody can correct.

        SYNCHRONOUS, AND THAT IS A TRADE-OFF WORTH NAMING. It puts an HTTP request in the path
        of a fill being submitted, so a black-holed network freezes the prompt for up to three
        six-second timeouts. The alternative is completing the fill from a worker callback,
        which breaks the prompt's contract that a submitted line has either been written or
        refused by the time it closes. The request only happens for a ticker the book has
        never seen, which is a handful of times in the life of the book.
        """
        if ticker in book.known_tickers(self.con):
            return None
        # THROUGH THE APP, so a test can stub it. Without this seam every test that submits a
        # fill for an unregistered ticker makes a real HTTP request -- slow, flaky, and
        # dependent on a market data endpoint being reachable from wherever the suite runs.
        return book.register_from_quote(
            self.con, ticker, lookup=getattr(self.app, "quote_lookup", None))

    # WHAT AN AMBIGUOUS TICKER LEFT BEHIND. Cleared by cancel_edit like every other armed
    # slot, so an abandoned choice cannot replay a fill typed minutes earlier.
    _pending_fill = None
    _listings = ()
    _candidates = ()

    def _choose_listing(self, value: str) -> None:
        """They picked a listing; register it and replay the fill they already typed."""
        chosen = parse.parse_listing(value)
        match = next((c for c in self._candidates
                      if c["quote_symbol"].upper() == chosen), None)
        if match is None:
            raise parse.ParseError(
                f"{chosen} is not one of them — tab through "
                f"{', '.join(self._listings)}")
        book.add_instrument(self.con, ticker=match["ticker"], name=match["name"],
                            currency=match["currency"], kind=match["kind"],
                            quote_symbol=match["quote_symbol"])
        pending = self._pending_fill
        self._pending_fill, self._listings, self._candidates = None, (), ()
        self.app.notify(
            f"registered {match['ticker']} · {match['currency']} {match['kind']} · "
            f"quotes as {match['quote_symbol']} — {match['name'] or 'no name given'}",
            timeout=8)
        if pending is not None:
            # REPLAYED THROUGH THE SAME DOOR, so the fill gets every check it would have
            # had -- the account, the bet, the short guard -- rather than a second code path
            # that has to remember them.
            self.handle_prompt("fill", pending)

    def handle_prompt(self, label: str, value: str) -> None:
        if label == "listing":
            self._choose_listing(value)
            return
        if label == "stop":
            self.handle_stop(value)
            return
        if label == "leg bet":
            self._move_bet(value)
            return
        if label != "fill":
            return
        # PARSED WITHOUT THE REGISTRY, then the ticker is resolved below. The registry check
        # used to live in the parser, which meant a genuinely new holding was refused in
        # exactly the same words as a typo -- the author: "i run into the this symbol is not in the
        # registry problem again. why not automatically check it up and add it to the registry
        # as i enter? i mean seems weird that we dont do this."
        r = parse.parse_fill(value, now=self.app.now(),
                             known_bets=book.known_bets(self.con),
                             known_accounts=book.known_accounts(self.con))
        try:
            registered = self._resolve_ticker(r.ticker)
        except book.AmbiguousSymbol as exc:
            # NOT A REFUSAL -- A QUESTION. The author: "when you have conflicts, like COIN and
            # COIN.TO, raise a prompt or something and let me tab through the ticker name."
            #
            # The fill line is stashed and replayed once they pick, so the answer costs them a
            # tab and an enter rather than retyping the fill. It is stashed rather than
            # re-derived because the prompt is about to be reused for the choice.
            self._pending_fill = value
            self._listings = tuple(c["quote_symbol"] for c in exc.candidates)
            self._candidates = exc.candidates
            self.app.notify(exc.detail(), timeout=20, severity="warning")
            self.app.prompt.open("listing", prefill=self._listings[0])
            return
        # AN EDIT IS DELETE-THEN-ADD, IN ONE TRANSACTION. add_fill appends to an open
        # position, which is what makes registration one line, so there is no in-place
        # update -- but on an autocommit connection the delete used to land even when the
        # add was refused, so a REJECTED line destroyed a row and left a position that
        # was never held. book.atomic makes it all-or-nothing.
        #
        # `_editing` is cleared and the undo entry stacked only AFTER the write commits.
        # Clearing it first disarmed the edit on rejection, so the corrected line the
        # prompt invites was handled as a fresh ADD -- and a later `u` then restored the
        # original on top of the replacement, the exact regression superseded_by exists
        # to prevent.
        editing = self._editing
        pre = None
        with book.atomic(self.con):
            if editing is not None:
                pre = book.delete_fill(self.con, editing)
            trade_id, fill_id, created = book.add_fill(
                self.con, ticker=r.ticker, shares=r.shares, price=r.price,
                bet_id=r.bet_id, on_date=r.on_date, fee=r.fee,
                note=r.note, account=r.account)
        self._editing = None
        if pre is not None:
            self.app.stack_undo("fills", pre)
            self.app.superseded_by(fill_id)
        self.reload()
        verb = ("edited" if editing is not None else
                "opened" if created else ("added to" if r.shares > 0 else "reduced"))
        # The account is always named back, even when it came from the default.
        # A fill that silently landed in the wrong sleeve is the expensive mistake
        # here, and it is only catchable at the moment of confirmation.
        acct = r.account or book.default_account(self.con)
        self.app.notify(
            f"{verb} {trade_id} in {acct}: {r.shares:+g} {r.ticker} @ {r.price:g}"
            + (f" fee {r.fee:g}" if r.fee else ""), timeout=5)
        if registered is not None:
            # A SEPARATE TOAST, AND A LONGER ONE. The desk wrote a registry row on their behalf
            # with a currency, a kind and a quote symbol it chose -- every one of which is
            # load-bearing, and the kind is the one it can only guess at. Folding that into
            # the fill's confirmation would bury it; saying nothing would leave a row nobody
            # knows to correct.
            self.app.notify(
                f"registered {registered['ticker']} · {registered['currency']} "
                f"{registered['kind']} · quotes as {registered['quote_symbol']}"
                + (f" ({registered['exchange']})" if registered.get("exchange") else "")
                + f" — {registered['name'] or 'no name given'}. Press i to correct it.",
                timeout=12)


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
class DashboardPane(Pane):
    """The first screen: what is the account worth, and how did it get there.

    IT USED TO ASK "may i trade, and what do i owe?" -- a question a bookkeeping tool
    cannot answer, since the broker is elsewhere and the book is read afterwards. What
    is left is a scoreboard, which is the whole remaining job. The author: "it records, it
    shows that's it. nothing complicated."

    THREE BLOCKS:

      1. the scoreboard   net, contributed, return. Three numbers, no table.
      2. the sleeves       cash and positions, per currency, and the rate between them.
      3. what is not known WHY a figure above is blank -- and nothing else.

    THE GAPS BLOCK IS GONE. It listed every record the book was missing, each with the
    key that would clear it, and the author's verdict was flat: "yes i think in a good product,
    gaps shouldnt be there at all." They are right about what it was. A tool that greets you
    with a list of chores has decided its convenience is your obligation. What survives
    is narrower and earns its place: if a number on this page is blank, the reason is
    printed under it, because an unexplained blank is worse than a nag.

    NO STATEMENT TOTAL EITHER. The author: "why do you bookkeeping a number i did ... from now
    on, we dont need statements in the tool." Net is derived -- see book.net().
    """
    title_id = "dash-head"
    scope = "dashboard"
    W = 14                     # label column: the widest label is "contributed"

    def compose(self) -> ComposeResult:
        yield Static("", id="dash-head", classes="pane-title")
        # SCROLLABLE, because neither the unknown list nor the ledger has a fixed length.
        # On a short terminal the sleeve block fell off the bottom with no way to reach it,
        # and a figure you cannot scroll to is a figure the tool did not record as far as
        # you know.
        with VerticalScroll(id="dash-scroll"):
            yield Static("", id="dash-body", classes="panel-body")
            # A TABLE, NOT INDENTED TEXT. The author: "cant we have a table or something? some
            # charts? this looks ugly as hell." They were right -- it was eight ragged lines of
            # hand-spaced columns, which is what a DataTable is for.
            yield Static("", id="port-head", classes="pane-title")
            # NOT FOCUSABLE, AND THAT IS A GUARD RATHER THAN A STYLE CHOICE. Nothing acts on
            # a row here -- it is a read-only view of shares -- but this pane's `x` and
            # `enter` query `#cap-table` directly, so once a second table could take focus,
            # tabbing to THIS one and pressing `x` would delete a capital row the user never
            # selected. That is the bug this codebase has already fixed twice, arriving by a
            # third route. A table nothing edits should not be reachable by the edit keys.
            port = PaneTable(id="port-table", cursor_type="none", zebra_stripes=False)
            port.can_focus = False
            yield port
            # THE CAPITAL TAB, FOLDED IN. The author: "why not just move capital to the dashboard?
            # seems like capital would be a standlone page few info anyway." They are right --
            # it was six deposits and two offsets holding a whole tab, and the figure they
            # add up to was already on this page as `contributed`.
            yield Static("", id="cap-head", classes="pane-title")
            yield PaneTable(id="cap-table", cursor_type="row", zebra_stripes=False)

    # A NON-TRADE ROW KEY STARTS WITH THIS, same convention as the positions table: bet
    # headings and the cash line live in the same DataTable as the legs, so `enter` and `x`
    # must be able to refuse them.
    INERT = "·"

    def on_mount(self) -> None:
        t = self.query_one("#cap-table", DataTable)
        for label, width in (("date", 12), ("acct", 5), ("kind", 11), ("amount", 18),
                             ("note", 40)):
            t.add_column(label, width=width)
        # TWO WEIGHT COLUMNS, because the author named two numbers and neither substitutes for the
        # other: "1. allocated capital vs. entire portfolio 2. deployed capital vs. bet".
        # `of net` is what the row CLAIMS of the portfolio, `of bet` is a leg's share of its
        # bet's allocation. On a bet row `of bet` is therefore how full it is.
        #
        # 24+14+8+8+12 = 66, plus 10 padding = 76 -- well inside a 110-column terminal, and
        # deliberately narrower than the positions table because this view carries shares
        # rather than prices.
        pt = self.query_one("#port-table", DataTable)
        for label, width in (("holding", 24), ("value", 14), ("of net", 8),
                             ("of bet", 8), ("", 12)):
            pt.add_column(label, width=width)
        self.reload()

    def focus_default(self) -> None:
        """THE SCROLL VIEW, not the ledger table, and this was got wrong once already.

        Focusing the table looked right -- `x` and `enter` act on a row, so give the rows
        focus -- and it broke the page. Textual scrolls a focused widget into view, so the
        dashboard OPENED PINNED TO THE BOTTOM with `net` off screen, and `up` could not get
        back: the table swallowed the key to move its own cursor, and at row 0 that is
        nothing. At 100x20, an ordinary terminal, the headline figure of the whole tool was
        invisible and unreachable.

        So the container holds focus and up/down scroll the page. The trade-off is that the
        ledger's cursor sits on row 0 until `tab` moves focus to the table -- which is
        acceptable only because `x` names the row in full before deleting it and `enter`
        prefills it visibly. That is a different situation from the old `x`-deletes-row-zero
        bug, where the user believed they had selected a different row and nothing said
        otherwise.
        """
        self.query_one("#dash-scroll", VerticalScroll).focus()

    def on_key(self, event) -> None:
        """`enter` edits the ledger row under the cursor, from the SCROLL container.

        Every other pane gets `enter` as DataTable.RowSelected, which needs the table to hold
        focus. This pane's focus is deliberately its scroll container -- see focus_default --
        so no such message is ever sent and `enter` did nothing here at all.

        Keys bubble, so an `enter` the VerticalScroll does not bind reaches the pane. Stopped
        and prevented explicitly, the same way InlinePrompt claims `escape`, or it carries on
        to the app binding as well.
        """
        if event.key != "enter" or self.app.prompt.is_open:
            return
        event.stop()
        event.prevent_default()
        self.key_activate()
        self.app.refresh_footer()

    def _content_width(self) -> int:
        """The columns the BODY actually has, measured rather than assumed.

        Wrapping is done here by hand for one reason: Textual re-wraps at the pane edge
        and a wrapped line restarts at column ZERO, which shredded the label column and
        made four lines look like eight at 80 columns.

        The scrollbar's two columns are the reason this is measured off the body and not
        off the terminal: computing from the app width overflowed by exactly that much
        and one line still wrapped. Before the first layout the size is 0, which is what
        on_resize is for -- the opening render uses the fallback and is corrected as soon
        as the widget knows how wide it is.
        """
        got = self.query_one("#dash-body", Static).content_size.width
        return max(50, got if got > 0 else (self.app.size.width or 110) - 4)

    def on_resize(self, event) -> None:
        """Re-wrap when the width changes, including the first time it is known."""
        if event.size.width != getattr(self, "_last_width", None):
            self._last_width = event.size.width
            self.reload()

    def _hang(self, first_prefix: str, text: str, style: str = "") -> list[str]:
        """One logical line, wrapped under a hanging indent as wide as its prefix."""
        indent = len(Text.from_markup(first_prefix).plain)
        avail = max(20, self._content_width() - indent)
        chunks = textwrap.wrap(text, avail) or [""]
        pad = " " * indent
        return [first_prefix + (mark(chunks[0], style) if style else chunks[0])] + [
            pad + (mark(c, style) if style else c) for c in chunks[1:]]

    def _fit(self, prefix: str, plain: str, styled: str) -> list[str]:
        """The coloured one-liner when it fits, the wrapped plain text when it does not.

        Two versions of the same line, because _hang can only wrap PLAIN text -- markup
        makes the character count a lie. This keeps per-number colour at a normal width
        without letting it be the reason something overflows at a narrow one.
        """
        if len(prefix) + len(plain) <= self._content_width():
            return [prefix + styled]
        return self._hang(prefix, plain)

    # -- blocks ----------------------------------------------------------------
    def _scoreboard(self, n) -> list[str]:
        w = self.W
        out = [f"{'net':<{w}}{mark(money(n.net), 'bold')} {n.home}"]
        if n.contributed is None:
            out.append(mark(f"{'contributed':<{w}}not recorded — press 3 then c", WARN))
            return out
        out.append(f"{'contributed':<{w}}{money(n.contributed)}")
        # RETURN, NOT "P&L AGAINST A LIMIT". There is no limit any more: the author, "i have
        # that number in my heart. a number that's not enforced has [no] use anyway."
        st = trend_style(n.pnl)
        out.append(f"{'return':<{w}}{mark(arrow(n.pnl) + ' ' + signed(n.pnl), st)}"
                   f"   {mark(pct(n.pnl_pct), st)}")
        return out + self._where_from(n)

    def _where_from(self, n) -> list[str]:
        """The four things the return is made of, indented under it.

        WITHOUT THIS THE HEADLINE IS UNEXPLAINABLE. A book whose open legs are slightly
        down can still show a clearly positive return, and nothing said where the rest came
        from -- the desk has no view of a closed trade at all, so the money was simply in
        cash with no account of how it got there.

        THEY SUM TO THE RETURN EXACTLY, and `ties()` is checked before any of it prints
        rather than trusted: a split that does not add up sends the reader hunting for a
        trade that does not exist. If it does not tie, or a part is unknown, nothing here
        is shown and the headline stands alone -- which is honest, where four numbers that
        almost add up are not.
        """
        if not n.ties():
            return []
        w = self.W
        rows: list[tuple[str, float | None]] = []
        # Realised and unrealised need a cost basis and can be individually unknown; their
        # SUM never is, because it needs none. So the split degrades to one line.
        if n.realised is not None and n.unrealised is not None:
            rows += [("realised", n.realised), ("unrealised", n.unrealised)]
        else:
            rows.append(("trading", n.trading))
        rows.append(("income", n.income))
        if n.offsets:
            rows.append(("offsets", n.offsets))
        # NAMED FOR THE CURRENCY, NOT "FX", and only when it is not zero -- a single-
        # currency book has no such term and a permanent 0.00 would be furniture.
        if n.currency_effect:
            rows.append((f"{self._other(n)} → {n.home}", n.currency_effect))
        out = []
        for label, value in rows:
            out.append(f"{'  ' + label:<{w}}"
                       + mark(signed(value), trend_style(value)))
        return out

    def _other(self, n) -> str:
        """The non-home currency the effect is against. Plural if there were somehow two."""
        others = [s.currency for s in n.sleeves if s.currency != n.home]
        return "/".join(others) if others else "fx"

    def _sleeves(self, n) -> list[str]:
        """Cash and positions, per currency. The arithmetic behind the net, in two lines.

        SHOWN PER CURRENCY rather than per account, because the currency is the thing
        that needs a rate and the account is not. The author: "an account is simply an account
        with a name with whatever currencies (we can hold USD/CAD simultaneously inside
        the tfsa)" -- so an account is no longer a unit any figure here divides by.
        """
        w = self.W
        out: list[str] = []
        out += self._fit(f"{'cash':<{w}}", money(n.cash), mark(money(n.cash), ''))

        cost = n.positions_cost
        plain = styled = money(n.positions)
        if n.positions is not None and cost is not None:
            gain = n.positions - cost
            plain += f"   cost {money(cost)}   {signed(gain)}"
            styled += (f"   cost {money(cost)}   "
                       f"{mark(signed(gain), trend_style(gain))}")
        out += self._fit(f"{'positions':<{w}}", plain, styled)

        # ONE LINE PER CURRENCY, and only when there is more than one. A single-currency
        # book already had every figure above quoted in it, so repeating it is noise.
        if len(n.sleeves) > 1:
            for s in n.sleeves:
                rate = (f"@ {s.fx:,.4f}" if s.fx is not None and s.currency != n.home
                        else "")
                bits = f"cash {money(s.cash)}   positions {money(s.positions)}"
                out += self._hang(f"{'  ' + s.currency:<{w}}",
                                  f"{bits}   {rate}".rstrip())
        return out

    def _unknown(self, n) -> list[str]:
        """Only ever WHY A FIGURE ABOVE IS BLANK. Never a list of chores.

        This replaced the gaps block, and the distinction is the whole point. A gap said
        "you have not recorded a stop on this leg" -- true, and none of the tool's
        business. A line here says "no mark: SGOV", which is the tool explaining its own
        em dash. If every figure is known, this block does not exist.
        """
        if not n.unknown:
            return []
        out = self._hang(f"{'not known':<{self.W}}", n.unknown[0], WARN)
        for why in n.unknown[1:]:
            out += self._hang(" " * self.W, why, WARN)
        return out

    # -- render ----------------------------------------------------------------
    def redraw(self) -> None:
        self._draw_scoreboard()
        self._draw_portfolio()
        self._draw_capital()

    def _draw_scoreboard(self) -> None:
        n = book.net(self.con)
        head = self.query_one("#dash-head", Static)
        body = self.query_one("#dash-body", Static)

        who = config.profile()
        title = mark(who.name or "desk", "bold") + mark(f"  ·  {n.home}", FAINT)
        head.update(f"{title}{self._freshness(n)}")

        lines: list[str] = []
        for block in (self._scoreboard(n), self._sleeves(n), self._unknown(n)):
            if block:
                lines.extend(block)
                lines.append("")
        body.update("\n".join(lines[:-1]) if lines else "")

    def _draw_portfolio(self) -> None:
        """Live bets, what each claims of the portfolio, and the legs inside them.

        The author: "the dashboard is essentially the portfolio tab. we should see live bets, their
        share of the portfolio, each trades within each bet." Then, on the first version:
        "cant we have a table or something? some charts? this looks ugly as hell."

        Both were right. It was eight ragged lines of hand-spaced columns, which is exactly
        what a DataTable is for, and a column of eight percentages is unreadable without a
        bar beside it.

        BOTH WEIGHTS ON EVERY ROW, because they named two and they answer different questions:
        `of net` is what a bet has RESERVED of the portfolio, `of bet` is how much of that
        reservation is spent -- and per leg, that leg's share of the same allocation. On their
        book each bet claims 47.3% while deploying 7.9% and 23.4%, a six-fold gap one number
        cannot express.
        """
        t = self.query_one("#port-table", DataTable)
        head = self.query_one("#port-head", Static)
        n = book.net(self.con)
        groups = book.portfolio(self.con)
        with self.keeping_the_cursor(t):
            t.clear()
            if not groups:
                head.update(mark("nothing held — press n on positions", WARN))
                return
            claimed = sum(g.claim_pct for g in groups if g.claim_pct is not None)
            # THE BAR RULE, SAID ONCE. Each bar is the row's share of its PARENT -- a bet
            # against the portfolio, a leg against its bet -- which is what makes a leg's bar
            # readable against its siblings instead of a sliver against the whole book. Two
            # meanings in one column needs stating, and the head is where it costs no rows.
            head.update(
                f"portfolio   {money(n.net)} {n.home}   "
                + mark(f"{share(claimed)} claimed by bets   ·   "
                       f"bars are each row's share of its parent", FAINT))
            self._gaps = 0
            for g in groups:
                t.add_row(
                    _cell(g.name, "bold" if g.bet_id else FAINT),
                    _cell(money(g.value), "bold"),
                    # A BET ROW SHOWS ITS CLAIM, and falls back to its market share when it
                    # has declared no allocation -- `no bet` never has one, and a bet that
                    # has not been budgeted still occupies part of the portfolio.
                    _cell(share(g.claim_pct if g.claim_pct is not None else g.weight_pct)),
                    _cell(share(g.filled_pct) if g.filled_pct is not None else NA,
                          FAINT if g.filled_pct is None else ""),
                    _cell(charts.cell_bar(
                        g.claim_pct if g.claim_pct is not None else g.weight_pct, 12)),
                    key=f"{self.INERT}grp:{g.bet_id or 'none'}")
                for p in g.legs:
                    of_net = (None if p.value_base is None or not n.net
                              else 100.0 * p.value_base / n.net)
                    of_bet = g.of_bet(p)
                    t.add_row(
                        _cell(f"  {p.account or '?':<5}{p.ticker}"),
                        _cell(money(p.value_base)),
                        _cell(share(of_net), FAINT),
                        _cell(share(of_bet) if of_bet is not None else NA,
                              FAINT if of_bet is None else ""),
                        # THE BAR IS OF THE BET, not of net, so a leg's bar is readable
                        # against its siblings rather than being a sliver against the whole
                        # portfolio. `of bet` is the column it sits beside.
                        _cell(charts.cell_bar(of_bet if of_bet is not None else of_net, 12),
                              FAINT),
                        key=p.trade_id)
                if g.unvalued:
                    t.add_row(
                        _cell(mark(f"  {g.unvalued} leg(s) cannot be valued", WARN)),
                        _cell(NA), _cell(NA), _cell(NA), _cell(""),
                        key=f"{self.INERT}blind:{g.bet_id or 'none'}")
            # CASH CLOSES THE 100%. Without it the `of net` column summed to 58% on their book
            # and read as an allocation with a hole in it -- when the rest is the most
            # decision-relevant figure there: what is not invested in anything.
            if n.cash is not None:
                w = None if not n.net else 100.0 * n.cash / n.net
                t.add_row(_cell("cash", FAINT), _cell(money(n.cash)), _cell(share(w)),
                          _cell(NA, FAINT), _cell(charts.cell_bar(w, 12), FAINT),
                          key=f"{self.INERT}cash")

    def _freshness(self, n) -> str:
        """How old the prices behind these figures are, and whether a fetch is running.

        THE ONE THING THE NUMBERS CANNOT SAY ABOUT THEMSELVES. It used to report the age
        of the hand-entered statement; there is no statement now, so it reports the
        oldest MARK -- which is what the net is actually made of.
        """
        if getattr(self.app, "fetching", False):
            return mark("   fetching…", FAINT)
        if n.as_of is None:
            # NOT "press m" any more: there is no key to press. The desk fetches on
            # open, every 30 seconds, and whenever the held tickers change, so the
            # honest thing to report is that one has not landed yet.
            return mark("   no prices yet — fetching on open and every 30s", WARN)
        age = (self.app.now().date() - dt.date.fromisoformat(n.as_of)).days
        when = ("today" if age == 0 else "yesterday" if age == 1
                else f"{age} days ago" if age > 1 else "dated ahead of today")
        style = WARN if age > 7 or age < 0 else FAINT
        return mark(f"   marked {when} · {n.as_of}", style)

    def status_hint(self) -> str:
        n = book.net(self.con)
        bits = [mark("dashboard", "bold")]
        if n.net is not None:
            bits.append(f"{money(n.net)} {n.home}")
        if n.contributed is None:
            bits.append(mark("no capital recorded", WARN))
        elif n.pnl is not None:
            bits.append(f"{signed(n.pnl)} on {money(n.contributed)}")
        return " · ".join(bits)

    def key_account(self) -> None:
        """Open a sleeve. The one bootstrap a book cannot skip: every write lands in one.

        The author asked "what is new account" -- fairly, because they will press this twice in the
        life of the book. It stays because a fresh book can record NOTHING until it is
        pressed once, and the alternative is inventing an account on their behalf.
        """
        self.app.prompt.open("account")

    def _add_account(self, value: str) -> None:
        r = parse.parse_account(value, known_accounts=book.known_accounts(self.con))
        book.add_account(self.con, account_id=r.account_id,
                         name=r.name or r.account_id,
                         on_date=self.app.now().date().isoformat(),
                         make_default=r.make_default)
        self.reload()
        self.app.refresh_all()
        self.app.notify(
            f"opened {r.account_id}"
            + (" · unqualified writes now land here" if r.make_default else "")
            + " — it holds whatever currencies you put in it", timeout=8)

    def handle_prompt(self, label: str, value: str) -> None:
        if label == "account":
            self._add_account(value)
            return
        self._handle_capital(label, value)


    def _draw_capital(self) -> None:
        t = self.query_one("#cap-table", DataTable)
        with self.keeping_the_cursor(t):
            self._fill_capital(t)

    # THE TWO TABLES THIS PANE WRITES, keyed the way its rows are. `src` distinguishes
    # them because both number from 1, so a bare `4` named two different rows and `x`
    # could delete a deposit while the cursor sat on an offset.
    _TABLE_OF = {"flow": "flows", "offset": "adjustments"}

    def _fill_capital(self, t: DataTable) -> None:
        t.clear()
        rows = book.capital_ledger(self.con)
        head = self.query_one("#cap-head", Static)
        if not rows:
            head.update(mark(
                "no capital recorded — press c. Until then the book has no denominator "
                "to measure a return against", WARN))
            return
        # SUMMED PER CURRENCY, NOT ACROSS THEM. Adding a USD deposit to a CAD one gives a
        # figure in no currency at all; the dashboard is where they are translated, and it
        # says which rate it used. This pane's job is to show what was typed.
        by_ccy: dict[str, float] = {}
        for r in rows:
            by_ccy[r["currency"]] = by_ccy.get(r["currency"], 0.0) + r["amount"]
        totals = "   ".join(f"{money(v)} {c}" for c, v in sorted(by_ccy.items()))
        offsets = sum(1 for r in rows if r["src"] == "offset")
        counted = f"{len(rows) - offsets} flow(s)"
        if offsets:
            counted += f" · {offsets} offset(s)"
        # ONE PATH, BECAUSE THE SPECIAL CASE PRINTED A FALSE ZERO. There used to be a
        # `len(by_ccy) > 1` branch, and the other side of it read
        # `money(by_ccy.get(base, 0.0))` labelled with `base` -- so a book whose flows are
        # all in ONE NON-HOME currency took the single-currency branch, failed to find the
        # home currency in the dict, and printed "net in 0.00 CAD" over real money.
        # Reachable, not theoretical: `c 15000 USD` parses to a USD flow.
        #
        # `.get(k, 0.0)` on a dict of currencies is the same defect as `or 0.0` in a sum,
        # which is this project's most repeated bug -- it once advertised +11,760.18 on a
        # book that had made +255.58. A missing currency is NOT a zero balance.
        #
        # And the branch bought nothing: `totals` already labels every currency it prints,
        # so for a single currency it renders exactly the right string. Deleting it removes
        # a wrong answer and a code path at once.
        head.update(f"{counted}   net in {mark(totals, 'bold')}")
        for r in rows:
            t.add_row(
                _cell(r["on_date"]),
                _cell(r["account_id"] or NA,
                      FAINT if r["account_id"] else WARN),
                # An offset is neither a deposit nor a withdrawal and must not be
                # coloured like one -- it is a correction, and green for +0.01 of
                # dividend would read as a contribution.
                _cell(r["kind"], FAINT if r["src"] == "offset"
                      else (GOOD if r["amount"] > 0 else WARN)),
                _cell(f"{signed(r['amount'])} {r['currency']}"),
                _cell(r["notes"] or NA, FAINT if not r["notes"] else ""),
                key=f"{r['src']}:{r['id']}")

    def _cursor_row(self) -> tuple[str, int] | None:
        """(src, id) under the cursor, or None on an empty table."""
        t = self.query_one("#cap-table", DataTable)
        if not t.row_count:
            return None
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        src, _, rid = key.partition(":")
        return src, int(rid)

    def key_flow(self) -> None:
        self.app.prompt.open("capital in")

    def key_offset(self) -> None:
        """The offset. The author: "we will just add a special offset entry into the account
        balance as i gave you the number to reconcile the difference."

        It lives on THIS page rather than the dashboard because it is a cash movement
        typed by hand, which is what every other row here is. The dashboard shows the
        consequence; this is where it is entered.
        """
        self.app.prompt.open("offset")

    def key_delete(self) -> None:
        at = self._cursor_row()
        if at is None:
            return
        src, rid = at
        row = self.con.execute(
            f"SELECT on_date, amount, currency FROM {self._TABLE_OF[src]} WHERE id=?",
            (rid,)).fetchone()
        if row is None:
            return
        self.app.ask_confirm(
            f"delete {src} {signed(row['amount'])} {row['currency']} on "
            f"{row['on_date']}?   y/n",
            lambda: self._do_delete(src, rid))

    def _do_delete(self, src: str, rid: int) -> None:
        table = self._TABLE_OF[src]
        pre = book.delete_row(self.con, table, rid)
        if pre is None:
            self.app.notify("that row is gone", timeout=3)
            return
        self.app.stack_undo(table, pre)
        self.reload()
        self.app.refresh_all()
        self.app.notify("deleted · u to undo", timeout=5)

    def key_activate(self) -> None:
        """Edit the row under the cursor: prefill the prompt, replace on submit."""
        at = self._cursor_row()
        if at is None:
            return
        src, rid = at
        row = self.con.execute(
            f"SELECT id, on_date, amount, currency, notes, account_id "
            f"FROM {self._TABLE_OF[src]} WHERE id=?", (rid,)).fetchone()
        if row is None:
            return
        self._editing = (src, rid)
        self.app.prompt.open(
            "capital in" if src == "flow" else "offset",
            prefill=parse.render_flow({
                "amount": row["amount"], "on_date": row["on_date"],
                "currency": row["currency"], "account": row["account_id"],
                "note": row["notes"]}, home=book.base_currency(self.con)))

    def _handle_capital(self, label: str, value: str) -> None:
        if label not in ("capital in", "offset"):
            return
        known = book.known_accounts(self.con)
        src = "flow" if label == "capital in" else "offset"
        if src == "flow":
            r = parse.parse_flow(value, now=self.app.now(), known_accounts=known)
            write = book.add_flow
        else:
            r = parse.parse_adjustment(value, now=self.app.now(), known_accounts=known)
            write = book.add_adjustment
        # Delete-then-add in ONE TRANSACTION, same as a fill and for the same reason.
        # This half is the more dangerous of the two: resolve_account can refuse a line
        # that was never altered, so on a book with several sleeves and no default,
        # pressing enter on a flow whose account was never recorded and submitting the
        # PREFILL UNCHANGED used to delete it -- silently dropping contributed capital,
        # which is the denominator every return figure is measured against.
        #
        # AN EDIT CANNOT CHANGE WHICH TABLE THE ROW IS IN. `enter` on an offset opens the
        # offset prompt, so the pre-image and the replacement are always the same kind --
        # and the guard below says so rather than assuming it, because a mismatch would
        # delete from one table and insert into the other.
        editing = self._editing
        pre = None
        with book.atomic(self.con):
            if editing is not None:
                was_src, rid = editing
                if was_src != src:
                    raise book.DeskError(
                        f"that row is a {was_src} and this prompt writes a {src}")
                pre = book.delete_row(self.con, self._TABLE_OF[src], rid)
            new_id = write(self.con, amount=r.amount, on_date=r.on_date,
                           currency=r.currency, note=r.note, account=r.account)
        self._editing = None
        if pre is not None:
            self.app.stack_undo(self._TABLE_OF[src], pre)
            self.app.superseded_by(new_id)
        self.reload()
        self.app.refresh_all()
        acct = r.account or book.default_account(self.con)
        ccy = r.currency or book.base_currency(self.con)
        self.app.notify(
            f"{'edited: ' if editing is not None else ''}{signed(r.amount)} {ccy} "
            f"{'into' if src == 'flow' else 'offset in'} {acct} on {r.on_date} "
            f"· u to undo", timeout=5)

# --------------------------------------------------------------------------- #
class BetsPane(Pane):
    """The bets: allocation, legs, and what share of the portfolio each one is.

    A POD-LEVEL RISK VIEW, and nothing about files. Earlier versions of this pane
    created a folder in the Obsidian vault and reported whether each thesis still
    resolved. The author removed all of it: "i manually handle the thesis, i create notes
    via shortcuts in obsidian or by chatting with AI, the book tool only manages the
    [book]." A tool that scaffolds someone else's writing has an opinion about their
    workflow, and this one should not.

    So the only thing shared with wherever a thesis is written is the bet's NAME.
    Nothing here resolves that name to a path, checks whether a note exists, or
    knows where the vault is.
    """
    title_id = "bet-head"

    scope = "bets"

    def compose(self) -> ComposeResult:
        yield Static("", id="bet-head", classes="pane-title")
        # SCROLLABLE, because the charts below the table have no fixed height and a bet you
        # cannot scroll to is a bet the tool did not record, as far as you know.
        with VerticalScroll(id="bet-scroll"):
            # ABOVE THE TABLE, and this is the ordering the author asked for: "allocation per bet
            # and x finished should be moved before the bets table. like a headline of this
            # tab." Both describe the WHOLE page -- how capital is split across bets, and
            # what the finished ones came to -- so reading them after the per-bet detail
            # meant scrolling past one bet's holdings to reach a figure about all of them.
            yield Static("", id="bet-headline", classes="panel-body")
            yield PaneTable(id="bet-table", cursor_type="row", zebra_stripes=False)
            # AND EVERYTHING BELOW IS ABOUT THE ONE ROW UNDER THE CURSOR. One Static, not
            # three, because the sections have to be ordered and spaced against each other
            # -- separate widgets each rendering their own block is what made this "all
            # scrambled together" the first time.
            yield Static("", id="bet-detail", classes="panel-body")

    def focus_default(self) -> None:
        """The table, so `r`/`a`/`s` and the row cursor work. The scroll follows it."""
        self.query_one("#bet-table", DataTable).focus()

    def on_data_table_row_highlighted(self, event) -> None:
        """Show the highlighted bet's note and how full its allocation is.

        NAMED `on_data_table_row_highlighted`, AND IT WAS `on_row_highlighted` UNTIL NOW --
        which Textual never calls. `DataTable.RowHighlighted.handler_name` is
        `on_data_table_row_highlighted`, so this method sat here doing nothing and the note
        line only ever refreshed from redraw(), i.e. after a WRITE. Moving the cursor left
        it captioning whichever bet had last been touched.

        Nothing complained, and nothing could: a handler Textual does not dispatch to is an
        ordinary unused method. That is the fourth silent name collision in this codebase --
        `_closed`, `_render`, `share`, and now a message handler -- and the first one that
        was wrong from the day it was written rather than broken by a later rename. A test
        now asserts every `on_*` method here matches a real Textual handler_name.

        Scoped by `event.data_table.id`, because DataTable messages BUBBLE: without the
        check this fires for the positions and capital tables too, and Textual delivers it
        to whichever ancestor handles it. The other panes' row keys are trade ids and
        `flow:1`, so `_note_for` would look up a bet that does not exist.
        """
        if getattr(event.data_table, "id", None) != "bet-table":
            return
        # EVERYTHING BELOW THE TABLE DESCRIBES THE ROW UNDER THE CURSOR, so it all moves
        # together: leaving it behind would caption one bet with another's holdings.
        self._draw_detail()

    def _width(self) -> int:
        got = self.query_one("#bet-detail", Static).content_size.width
        return max(50, got if got > 0 else (self.app.size.width or 110) - 4)

    def on_mount(self) -> None:
        t = self.query_one("#bet-table", DataTable)
        # 9 columns, widths summing to 90, +18 padding = 108, exactly the cells a
        # 110-column terminal leaves.
        #
        # `note` AND `filled` BOTH PAID FOR P&L, which the author asked for: "on the bet page, you
        # should list out the pnl per bet." Neither is a loss worth arguing about. `note` was
        # down to 13 cells, which renders "Written 2026-" -- a column that says a note exists
        # and nothing more; it moves to the header for whichever row the cursor is on, where
        # it has a hundred cells instead of thirteen. `filled` was pure arithmetic on the two
        # columns beside it, so the eye can do it.
        #
        # `%` is 7 because "+12.62%" is 7. `weight` keeps one decimal because a 1.4% bet and
        # a 1.9% bet are different sizes and both round to 2%.
        # `status` IS 8, NOT 9. Only the live statuses reach this table -- forming, live,
        # weakened -- and the longest is 8; `abandoned` at 9 is a finished bet and never
        # rendered here. That cell plus one off `of plan` paid for the wider header.
        for label, width in (("opened", 10), ("bet", 21), ("status", 8),
                             # `spent`, NOT `deployed`: the column is entry cost, so that
                             # allocated - spent is the dry powder and spent / allocated is
                             # exactly the `filled` percentage the header below prints. It
                             # used to hold market value, which made that division give a
                             # third number. See Bet.filled_pct.
                             ("legs", 4), ("allocated", 11), ("spent", 11),
                             # `weight` SAID NOTHING ABOUT ITS DENOMINATOR, which is what
                             # made 47.3% read as arbitrary. The header now names it.
                             ("of plan", 7), ("P&L", 11), ("%", 7)):
            t.add_column(label, width=width)
        self.reload()

    def redraw(self) -> None:
        t = self.query_one("#bet-table", DataTable)
        with self.keeping_the_cursor(t):
            self._fill_table(t)
        self._draw_headline()
        # The detail follows the cursor, and a rebuild does not fire RowHighlighted for the
        # row it lands back on -- so without this it goes blank after every write.
        self._draw_detail()

    def _draw_headline(self) -> None:
        """The two figures that describe the WHOLE tab, above the table.

        The author: "allocation per bet and x finished should be moved before the bets table. like
        a headline of this tab." Both are page-level -- how capital is split across bets, and
        what the finished ones came to -- and they were sitting below the per-bet detail, so
        reading a fact about every bet meant scrolling past the holdings of one.

        THE PIE THAT WAS HERE IS GONE. The author: "i think you shouldnt use a pie chart. look into
        daylogs, for such pie chart they just stack each on top of each other." daylogs had
        already written the argument down -- a terminal has about eight distinguishable
        fills and you need a legend to read any amount, whereas one bar per row carries
        label, share, amount AND rank at once.

        Sorted LARGEST FIRST, because rank is one of the four things these rows carry and
        the table below is sorted by date.
        """
        target = self.query_one("#bet-headline", Static)
        every = book.bets(self.con)
        # ONLY THE BETS ON SCREEN. Charting a finished bet's allocation above a table that
        # does not list it invites the reader to look for a row that is not there.
        # `live` and `allocated` BOTH FED THE PER-BET ALLOCATION BARS, which were deleted
        # when they turned out to restate the `of plan` column two lines below them (see
        # the note further down). `allocated` had no reader left; ruff's F841 found it.
        done = [b for b in every if not book.is_live(b.status)]

        n = book.net(self.con)
        out: list[Text] = []
        # THE "allocation declared per bet" BARS WERE HERE AND ARE GONE. They divided each
        # allocation by the sum of the allocations, which is now exactly what the table's
        # `of plan` column prints -- 50.0% and 50.0% -- so they restated a column two lines
        # below them. The tab keeps its chart: the HOLDINGS bars under the cursor's bet, which
        # say something the table does not.
        line = self._finished(done, n)
        if line is not None:
            if out:
                out.append(Text(""))
            out.append(line)
        # A TRAILING BLANK, because this block now sits directly above the table and without
        # it the finished total reads as the table's first row. The author: "you should have some
        # breadthing air between sections."
        if out:
            out.append(Text(""))
        target.update(_join(out))

    def _finished(self, done, n) -> Text | None:
        """One line for every bet whose story is over. Not a breakdown -- a total.

        Withheld rather than partial, like every other figure here: if one finished bet
        cannot be valued then the sum of the others is not "the realised total" and must not
        print as one.
        """
        if not done:
            return None
        got = [b.realised for b in done if b.realised is not None]
        line = f"{len(done)} finished"
        if len(got) == len(done) and got:
            total = sum(got)
            return Text.from_markup(
                f"{line} · realised {mark(signed(total), trend_style(total))} {n.home}")
        if got:
            return Text.from_markup(mark(
                f"{line} · {len(done) - len(got)} of them cannot be valued, so there is "
                f"no total", WARN))
        return Text(line, style=FAINT)

    def _draw_detail(self) -> None:
        """Everything about the ONE bet under the cursor, in named sections.

        The author: "for each bet, i still want the data to be more organized down here. maybe you
        can add sections or something." It was a title line, a wrapped note, a chart heading
        and a bar list with no hierarchy between them -- four blocks that each looked equally
        like the start of something.

        So each section gets a heading, and the sections are drawn in one Static rather than
        three. Three widgets each rendering their own block is what made it "scrambled
        together": nothing could space itself against a neighbour it did not know about.
        """
        t = self.query_one("#bet-table", DataTable)
        target = self.query_one("#bet-detail", Static)
        bet_id = None
        if t.row_count and t.is_valid_coordinate(t.cursor_coordinate):
            bet_id = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        b = next((x for x in book.bets(self.con) if x.id == bet_id), None)
        if b is None:
            target.update("")
            return

        width = self._width()
        out: list[Text] = [Text(b.name, style="bold")]

        facts = []
        if b.filled_pct is not None:
            # "filled", NOT "deployed", and at cost -- this line and the HOLDINGS header
            # below it printed 60.1% and 62.0% for the same bet because one measured at
            # market and one at cost. Both are the same figure now. See Bet.filled_pct.
            facts.append(f"{share(b.filled_pct, 0)} of {money(b.allocation)} filled")
        elif b.allocation is not None and b.cost == 0:
            # ONLY WHEN THE COST IS GENUINELY ZERO. `cost` is 0.0 for a bet with no legs and
            # None when a leg cannot be translated, and this branch used to catch both -- so a
            # bet holding C$11,125 across eight legs with one missing fx rate printed
            # "allocated, nothing spent", which is a false statement about money sitting
            # directly above a HOLDINGS header quoting a percentage.
            facts.append(f"{money(b.allocation)} allocated, nothing spent")
        elif b.allocation is not None:
            facts.append(f"{money(b.allocation)} allocated · spend NOT MEASURABLE — "
                         f"{b.unpriced_legs or 'some'} leg(s) have no recorded cost")
        else:
            facts.append("no allocation declared — press a")
        facts.append(f"{b.status}")
        if b.closed_legs:
            facts.append(f"{b.closed_legs} closed leg(s)")
        if b.unvalued_closed_legs:
            facts.append(f"{b.unvalued_closed_legs} of them cannot be valued")
        out.append(Text("  ·  ".join(facts), style=FAINT))

        out += self._section("HOLDINGS", self._composition_block(min(width, 78)))
        out += self._section("THESIS", self._thesis_block(b, width))
        target.update(_join(out))

    @staticmethod
    def _section(title: str, body: list[Text]) -> list[Text]:
        """A blank line, a heading, then the body. Nothing at all when the body is empty.

        A heading over nothing is worse than no heading: it reads as a section that failed to
        load rather than one that has no content, and this pane has real reasons to have
        neither holdings nor a thesis yet.
        """
        if not body:
            return []
        return [Text(""), Text(title, style="bold")] + body

    def _thesis_block(self, b, width: int) -> list[Text]:
        """The note, wrapped to the pane rather than shortened to one line.

        IT USED TO BE ONE TRUNCATED LINE ending in an ellipsis, which said a thesis existed
        and nothing about what it claimed. Under its own heading there is room for several
        lines, and the reason to keep any of it on screen is to be able to READ it.

        Still bounded: six lines, because the 2026-q4-reverse note is three paragraphs and
        the table above must not be pushed off the top of a short terminal.
        """
        note = " ".join((b.note or "").split())
        if not note:
            # NOT AN ERROR AND NOT A NUDGE. The live-without-a-thesis gate was removed at
            # The author's request; saying it is empty is the honest remainder of that.
            return [Text("none written", style=FAINT)]
        lines = textwrap.wrap(note, width=max(40, width - 2))
        out = [Text(x, style=FAINT) for x in lines[:6]]
        if len(lines) > 6:
            out.append(Text(f"… {len(note):,} characters in all — e to read or edit",
                            style=FAINT))
        return out

    def _composition_block(self, width: int) -> list[Text]:
        """What the bet under the cursor is made of, per ticker, as a share of its budget.

        The author: "i want to view ticker composition within a bet (like it takes up
        xx%)", and they picked the DECLARED ALLOCATION as the denominator, so the bars sum
        to the bet's
        filled percentage and the rest is a `(dry)` row. That row is the point of the choice:
        "COIN is 10.7% of this bet" and "73.5% of this bet is still cash" are both answers
        the share-of-the-spend version cannot give.

        FOR THE ROW UNDER THE CURSOR, not every bet at once. Per-ticker bars for five bets
        would be twenty rows below a five-row table, and the cursor is already how `s`, `a`
        choose which bet they act on.

        `(dry)` IS AN ITEM, NOT A SUBTRACTION, which is what makes this free: ranked_bars
        derives its total from the positive values it is handed, so including the unspent
        budget as a row makes the allocation the denominator without the chart knowing.
        """
        # NO INERT CHECK, unlike the positions table: every row here is one bet and its key
        # is the bet id. `self.INERT` belongs to PositionsPane, whose table carries group
        # headings and blank spacers in the same DataTable -- reaching for it here raised
        # AttributeError, which Pane.reload caught and printed in the pane title.
        bet = self._cursor_bet()
        if bet is None:
            return []
        c = book.bet_composition(self.con, bet)
        if not c.legs and not c.unpriced:
            return []

        # NO HEADING OF ITS OWN ANY MORE -- `_section` supplies "HOLDINGS" above it, and
        # two headings for one block is what "scrambled together" looked like up close.
        head = ""
        rows: list[tuple[str, float]] = list(c.legs)
        # THE BET'S OWN WITHHELD FIGURE, not a second computation of it. This block used to do
        # its own `100.0 * c.spent / c.allocation`, and c.spent is the sum of the legs it could
        # price -- so on a bet with one unpriced leg the header printed a confident 33.1% while
        # Bet.filled_pct withheld and the detail line three rows above said "nothing spent".
        # Two answers to one word, which is the exact defect this branch set out to remove.
        b = next((x for x in book.bets(self.con) if x.id == bet), None)
        filled = b.filled_pct if b is not None else None
        if c.unpriced:
            # A DENOMINATOR WITH A HOLE IN THE NUMERATOR IS NOT A PERCENTAGE. `dry` is None
            # here by construction (see bet_composition), so there is no (dry) bar either:
            # drawing one would label an unknown spend as unspent budget.
            head += (f"   {money(c.allocation)} {c.currency} allocated · spend NOT "
                     f"MEASURABLE, so shares are of the part that is priced")
        elif c.allocation is None:
            # NO DENOMINATOR, SO NO PERCENTAGES OF IT. Handing these to ranked_bars anyway
            # would still draw shares -- of the spend -- and label them as if they
            # answered the question they asked. Say which denominator is missing instead.
            head += (f"   no allocation declared, so shares are of the "
                     f"{book.base_currency(self.con)} spent — press a")
        elif c.dry is not None and c.dry > 0:
            head += (f"{money(c.allocation)} {c.currency} allocated, "
                     f"{share(filled)} filled")
            rows.append(("(dry)", c.dry))
        else:
            # OVER THE ALLOCATION. The gap is negative, so there is no dry row to add and the
            # denominator falls back to what was actually spent -- which must be said, or
            # the same bars would silently answer a different question than they did
            # yesterday.
            over = -(c.dry or 0.0)
            head += (f"   {money(c.allocation)} {c.currency} allocated and "
                     f"{money(over)} OVER it, so shares are of the spend")
        if c.unpriced:
            head += f"   excludes {' '.join(c.unpriced)} — no cost recorded"

        bars = charts.ranked_bars(rows, width=width,
                                  unit=f" {c.currency}" if c.currency else "")
        return ([Text(head.strip(), style=FAINT)] if head.strip() else []) + bars

    def _fill_table(self, t: DataTable) -> None:
        t.clear()
        every = book.bets(self.con)
        # ONLY WHAT IS STILL IN PLAY. The author: "in the bets tab, dont show the breakdown of
        # closed/retired bets. only show live ones." Four of their five bets are finished, so
        # the page was four rows of history above the one row carrying risk -- and the
        # finished ones cannot be acted on, which is what this page is for.
        #
        # THE BREAKDOWN GOES, NOT THE TOTAL. "dont show the breakdown" is what they said, and a
        # single line of realised is not a breakdown; dropping it too would hide C$2,195 of
        # results with nothing to say where it went. Per-leg detail lives on tab 2.
        rows = [b for b in every if book.is_live(b.status)]
        done = [b for b in every if not book.is_live(b.status)]
        head = self.query_one("#bet-head", Static)
        if not rows:
            note = "no bets in play — press n"
            if done:
                note += f". {len(done)} finished, and tab 2 has their legs"
            head.update(mark(note, WARN))
            # The finished total is drawn by _draw_headline, which redraw() calls after this
            # -- it is a fact about the whole tab, so it survives an empty table.
            return
        live = sum(1 for b in rows if b.status in ("live", "weakened"))
        n = book.net(self.con)
        weights = [b.weight_pct for b in rows if b.weight_pct is not None]
        blind = {b.id: b.unpriced_legs for b in rows if b.unpriced_legs}
        unmeasured = [b.id for b in rows if b.weight_pct is None and b.legs]
        parts = [f"{len(rows)} bet(s)", f"{live} live"]
        if weights:
            parts.append(f"{sum(weights):.1f}% of {money(n.net)} {n.home} in bets"
                         + (" (SHORT)" if blind else ""))
        # THE CONTEXT THAT MAKES A 1.4% WEIGHT READABLE, and the reason a bare percentage
        # against net looked useless on this book: nearly all of it is parked. Stated as a
        # fact beside the weights rather than carved out of the denominator -- see
        # book.parked() for why the carve-out was refused.
        pk = book.parked(self.con)
        if pk:
            # NOT `share = ...`. That shadowed the imported share() two lines below and
            # raised "'str' object is not callable" -- inside a message handler, so Textual
            # swallowed it and the table rendered EMPTY with no error anywhere. Second time
            # in one session; see Pane.reload for the guard that now catches it.
            of_net = f" ({100.0 * pk / n.net:.0f}%)" if n.net else ""
            parts.append(mark(f"{money(pk)} parked in money-market{of_net}", FAINT))
        if blind:
            parts.append(mark(
                "weight understated, unpriced legs in: "
                + " ".join(f"{k}({v})" for k, v in sorted(blind.items())), WARN))
        if unmeasured:
            parts.append(mark(f"unmeasurable: {' '.join(unmeasured)}", WARN))
        head.update("   ".join(parts))
        for b in rows:
            # THE THREE FIGURES ONLY MEAN ANYTHING TOGETHER. `weight` says what is at risk
            # now, `spent` against `allocated` says how much of the plan has been bought,
            # and the plan is what the weight becomes if it fills.
            t.add_row(
                _cell(b.opened or NA, FAINT if not b.opened else ""),
                # THE NAME, NOT THE SLUG. The slug is a key the grammar needs; this column is
                # read by a person. `Bet.name` falls back to the id for anything created
                # before v13, so nothing renders blank.
                _cell(b.name),
                _cell(b.status, GOOD if b.status in ("live", "weakened") else FAINT),
                _cell(str(b.legs) if b.legs else NA, "" if b.legs else FAINT),
                _cell(money(b.allocation), FAINT if b.allocation is None else ""),
                # NOTHING OPEN MEANS NO EXPOSURE TO REPORT, not 0.0%. Four of five bets
                # here are closed or forming, and four rows of a confident "+0.0%" buried
                # the one row that is actually carrying risk. An em dash is this codebase's
                # word for not-applicable and that is what these are.
                _cell(money(b.cost) if b.legs else NA,
                      FAINT if b.cost is None or not b.legs else ""),
                # THIS BET'S BUDGET AGAINST THE OTHER BUDGETS. The author: "essentially we are
                # giving each bet x amount of cash to play out. this has nothing to do with
                # total net worth." It was market/net, then allocation/net, and their version
                # is the one that matches what an allocation IS.
                #
                # NOT GATED ON `b.legs`: a bet that has declared capital has claimed its share
                # of the plan whether or not a share has been bought. An em dash means no
                # allocation was declared, the only case with nothing to state.
                _cell(share(b.of_allocated_pct) if b.of_allocated_pct is not None else NA,
                      FAINT if b.of_allocated_pct is None else ""),
                # P&L OVER THE BET'S WHOLE LIFE: realised plus unrealised. A bet that is
                # closed still made what it made, which is the point of recording it -- and
                # until now the only place that number existed was sqlite3.
                #
                # A BET THAT NEVER HELD ANYTHING HAS NO P&L, not +0.00. debasement-real-rates
                # is a pre-registered plan with no position ever opened; printing a confident
                # zero claims it was tried and broke even.
                _cell(f"{arrow(b.pnl)} {signed(b.pnl)}"
                      if b.pnl is not None and (b.legs or b.closed_legs) else NA,
                      trend_style(b.pnl) if b.legs or b.closed_legs else FAINT),
                _cell(pct(b.pnl_pct) if b.pnl_pct is not None else NA,
                      trend_style(b.pnl) if b.legs or b.closed_legs else FAINT),
                key=b.id)

    def status_hint(self) -> str:
        """THE ALLOCATION NAG WENT TOO.

        It said "legs but no allocation", and it existed because the per-leg stop curve
        divided by the frozen allocation and could not be computed without one. There is
        no curve. So the line was a demand for a number nothing reads.
        """
        rows = book.bets(self.con)
        live = sum(1 for b in rows if b.status in ("live", "weakened"))
        bits = [mark("bets", "bold"), f"{len(rows)} recorded"]
        if live:
            bits.append(f"{live} live")
        return " · ".join(bits)

    def key_new_bet(self) -> None:
        self.app.prompt.open("bet")

    def _cursor_bet(self) -> str | None:
        t = self.query_one("#bet-table", DataTable)
        if not t.row_count:
            return None
        return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value

    def key_note(self) -> None:
        """Edit the bet's thesis in $EDITOR. The one key that leaves the desk."""
        bet = self._cursor_bet()
        if bet is None:
            return
        row = self.con.execute("SELECT notes FROM bets WHERE id=?", (bet,)).fetchone()
        edited = self.app.edit_text(row["notes"] or "")
        if edited is None:
            # UNCHANGED OR ABANDONED, and both are a no-op. Stacking an undo entry for an
            # edit that did not happen would make the next `u` revert whatever came before.
            return
        pre = book.set_bet_note(self.con, bet, note=edited)
        self.app.stack_undo_update("bets", pre)
        self.reload()
        self.app.refresh_all()
        was, now = len(row["notes"] or ""), len(edited)
        self.app.notify(f"{bet}: note {was:,} → {now:,} characters · u to undo", timeout=6)

    def key_rename(self) -> None:
        """Rename the bet under the cursor. Prefilled with the current name to EDIT."""
        bet = self._cursor_bet()
        if bet is None:
            return
        b = next((x for x in book.bets(self.con) if x.id == bet), None)
        self._editing_bet = bet
        # PREFILLED WITH THE NAME, not the slug, and this is what the author meant by not typing
        # "the entire stuff all at once": the prompt opens on the words already there, so a
        # rename is an edit rather than a re-entry.
        self.app.prompt.open("rename", prefill=b.name if b else bet)

    def _rename(self, value: str) -> None:
        bet = self._editing_bet
        if bet is None:
            return
        new = parse.parse_bet_name(value)
        pre = book.set_bet_name(self.con, bet, name=new)
        self._editing_bet = None
        self.app.stack_undo_update("bets", pre)
        self.reload()
        self.app.refresh_all()
        self.app.notify(f"renamed to {new} · the id stays {bet} · u to undo", timeout=6)

    def key_status(self) -> None:
        """Move the bet under the cursor between forming / live / closed / …

        NOT PREFILLED WITH THE CURRENT STATUS. Every other prompt here prefills what is
        recorded, because the fast path is to accept it -- but the fast path for a status
        is never "the same one", and set_bet_status refuses a no-op anyway. Prefilling it
        would mean the one keystroke this prompt makes cheap is the one that does nothing.
        """
        bet = self._cursor_bet()
        if bet is None:
            return
        row = self.con.execute(
            "SELECT status, (SELECT COUNT(*) FROM trades t WHERE t.bet_id=b.id "
            "AND t.status='open') legs FROM bets b WHERE b.id=?", (bet,)).fetchone()
        self._editing_bet = bet
        self.app.prompt.open("bet status")
        self.app.notify(
            f"{bet} is {row['status']}"
            + (f", holding {row['legs']} open leg(s)" if row["legs"] else ""), timeout=6)

    def _set_status(self, value: str) -> None:
        bet = self._editing_bet
        if bet is None:
            return
        r = parse.parse_bet_status(value, now=self.app.now())
        pre = book.set_bet_status(self.con, bet, status=r.status, on_date=r.on_date,
                                  note=r.note)
        self._editing_bet = None
        self.app.stack_undo_update("bets", pre)
        self.reload()
        self.app.refresh_all()
        self.app.notify(
            f"{bet}: {pre['status']} → {r.status} on {r.on_date}"
            + ("" if pre["opened"] or r.status == "forming"
               else f" · opened set to {r.on_date}, which the schema requires")
            + " · u to undo", timeout=8)

    def key_allocation(self) -> None:
        """The bet's FROZEN capital, which every leg's share is measured against.

        Without it v_leg_required_stop cannot compute a required stop at all, so the
        dashboard reports the legs as un-checkable and no stop on them can be validated.
        """
        bet = self._cursor_bet()
        if bet is None:
            return
        row = self.con.execute(
            "SELECT allocation_base, allocation_set_on FROM bets WHERE id=?",
            (bet,)).fetchone()
        self._editing_bet = bet
        pre = ""
        if row["allocation_base"] is not None:
            pre = parse._num(row["allocation_base"])
            if row["allocation_set_on"]:
                pre += f" @{row['allocation_set_on']}"
        self.app.prompt.open("allocation", prefill=pre)

    def _set_allocation(self, value: str) -> None:
        bet = self._editing_bet
        if bet is None:
            return
        r = parse.parse_allocation(value, now=self.app.now())
        pre = book.set_allocation(self.con, bet, amount=r.amount, on_date=r.on_date,
                                  why=r.why)
        self._editing_bet = None
        self.app.stack_undo_update("bets", pre)
        self.reload()
        self.app.refresh_all()
        was = pre["allocation_base"]
        self.app.notify(
            f"{bet} allocated {r.amount:,.2f} as of {r.on_date}"
            + (f" (re-baselined from {was:,.2f})" if was is not None else "")
            + " · every leg's required stop moves with it · u to undo", timeout=8)

    def handle_prompt(self, label: str, value: str) -> None:
        if label == "allocation":
            self._set_allocation(value)
            return
        if label == "bet status":
            self._set_status(value)
            return
        if label == "rename":
            self._rename(value)
            return
        if label != "bet":
            return
        r = parse.parse_bet(value, now=self.app.now(),
                            known_bets=frozenset(b.id for b in book.bets(self.con)))
        book.add_bet(self.con, bet_id=r.bet_id, name=r.name, note=r.note,
                     allocation=r.allocation, on_date=r.on_date)
        self.reload()
        self.app.refresh_all()
        self.app.notify(
            f"registered {r.name} (id {r.bet_id}) — write the thesis wherever you "
            f"write them, under the same name", timeout=8)
