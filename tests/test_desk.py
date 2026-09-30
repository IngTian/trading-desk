"""The desk TUI, driven through Textual's pilot with real key presses.

Asserted against the DATABASE, not against rendered text: what matters is that
pressing `n` and typing a line puts the right row in the book. Shape copied from
daylogs/tests.

Every test runs against a COPY of the real book in tmp_path, so the suite can press
destructive keys without touching book/hub.db.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import pathlib
import re
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from textual.containers import VerticalScroll
from textual.widgets import DataTable, Static, TabbedContent

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import seed  # noqa: E402

from desk import book, config, parse  # noqa: E402
from desk.tui import charts, keymap, themes  # noqa: E402
from desk.tui.app import DeskApp  # noqa: E402
from desk.tui.panes import PositionsPane  # noqa: E402
from desk.tui.widgets import NA as NA_DASH  # noqa: E402
from desk.tui.widgets import share  # noqa: E402

NOW = dt.datetime(2026, 9, 7, 9, 30)


@pytest.fixture()
def con(tmp_path):
    """A SYNTHETIC book, built from book/schema.sql. Not the author's record.

    This used to copy book/hub.db, and that coupling went red three times in one
    week — every time because the real book got MORE accurate. See tests/seed.py.
    """
    c = seed.build(tmp_path / "hub.db")
    book.set_actor(c, "test")
    yield c
    c.close()


@pytest.fixture()
def blank(con):
    """The real SCHEMA with the LEDGER emptied.

    The arithmetic tests below used to run against the live book and assert things
    like `capital_in(con) is None`. That made them fail the moment a real
    contribution was recorded -- a test that breaks when the book is used is
    testing the data, not the code. Accounts, instruments and bets stay, because
    those are the fixtures the writers validate against.
    """
    # income and fx_conversions reference accounts, so they go before the account
    # does. Enumerated rather than switching foreign_keys off -- the constraint is
    # correct and the fixture was the thing that was incomplete.
    for t in ("fills", "trades", "flows", "adjustments", "account_snapshots",
              "confirmations", "income", "fx_conversions"):
        con.execute(f"DELETE FROM {t}")
    # ONE account, so these tests are about the loss-limit arithmetic and not about
    # pooling. Pooling has its own tests below, where it is the subject rather than
    # a precondition every unrelated assertion has to satisfy.
    con.execute("DELETE FROM accounts WHERE is_default=0")
    return con


@pytest.fixture()
def app(con):
    a = DeskApp(con)
    a.now = lambda: NOW
    # NO 60-SECOND POLL UNDER TEST. It makes a live HTTP call, so leaving it on would
    # make every app test slow, flaky, and dependent on a market being open. The fetch
    # has its own tests that call it directly.
    a.POLL_DISABLED = True
    # A FAILED RENDER MUST FAIL THE TEST, not be reported politely in a title nobody
    # asserts on. Two render bugs this session were swallowed by Textual's message loop and
    # the whole suite stayed green against a pane that had drawn nothing.
    a.PROPAGATE_RENDER_ERRORS = True
    # NO NETWORK, EVER, and the default is the honest one for a test: the symbol is unknown.
    # An unstubbed lookup made every fill of an unregistered ticker issue a real HTTP request.
    # A test that wants a successful identification sets this itself.
    a.quote_lookup = lambda ticker: []
    return a


@pytest.fixture()
def type_into():
    async def _type(pilot, text):
        for ch in text:
            await pilot.press("space" if ch == " " else ch)
    return _type


@pytest.fixture()
def goto():
    """Walk to a tab by its digit. The dashboard is tab 1, so nothing else is tab 1.

    Tests used to press a positions key straight from boot, which only worked while
    positions happened to be the first tab. Going through the digit means a future
    reorder breaks this fixture and not thirty assertions.
    """
    async def _goto(pilot, scope):
        # DERIVED FROM THE KEYMAP, never a second copy of the tab order. The literal map
        # that used to be here went stale the instant capital was folded into the
        # dashboard: fourteen tests walked to tab 3 and then asserted against bets, which
        # is a fixture lying rather than a feature breaking.
        digit = next(k.key for k in keymap.KEYMAP
                     if k.scope == "app" and k.action == f"show_{scope}")
        await pilot.press(digit)
        await pilot.pause()
    return _goto


# --------------------------------------------------------------------------- #
# the keymap is data, so its invariants are testable without an app
# --------------------------------------------------------------------------- #
def test_no_duplicate_key_within_a_scope():
    seen = set()
    for k in keymap.KEYMAP:
        assert (k.key, k.scope) not in seen, f"{k.key} twice in {k.scope}"
        seen.add((k.key, k.scope))


def test_every_bindable_key_gets_exactly_one_binding():
    binds = keymap.app_bindings()
    keys = [b[0] for b in binds]
    assert len(keys) == len(set(keys))
    bindable = {k.key for k in keymap.KEYMAP if k.bind}
    assert set(keys) == bindable


def test_every_scope_is_declared():
    for k in keymap.KEYMAP:
        assert k.scope in keymap.SCOPES
        assert k.kind in keymap.KINDS


def test_every_prompt_label_has_a_hint():
    """A prompt with no hint renders three empty slots."""
    labels = {"fill", "capital in", "offset", "bet", "listing"}
    for label in labels:
        assert keymap.hint_for(label) is not None, f"no Hint for {label!r}"


def test_every_hint_example_actually_parses():
    """An example that does not parse teaches the wrong grammar."""
    for h in keymap.HINTS:
        if h.label == "fill":
            parse.parse_fill(h.example, now=NOW)
        elif h.label == "capital in":
            parse.parse_flow(h.example, now=NOW)
        elif h.label == "offset":
            r = parse.parse_adjustment(h.example, now=NOW)
            assert r.amount and r.on_date
        elif h.label == "account":
            r = parse.parse_account(h.example)
            assert r.name and "=" not in r.name, f"the name absorbed a sigil: {r.name!r}"
            assert r.account_id
        elif h.label == "bet status":
            r = parse.parse_bet_status(h.example, now=NOW)
            assert r.status in parse.BET_STATUSES
            assert r.note, "the ~why in the example was eaten"
        elif h.label == "stop":
            r = parse.parse_stop(h.example)
            assert r.price, "the =price in the example was eaten"
            assert 0 < r.pct <= 100
        elif h.label == "allocation":
            r = parse.parse_allocation(h.example, now=NOW)
            assert r.amount > 0 and r.on_date
        elif h.label == "listing":
            assert parse.parse_listing(h.example) == h.example.upper()
        elif h.label == "rename":
            # FREE TEXT, VERBATIM -- spaces and capitals included, which is the entire point
            # of separating the name from the slug.
            assert parse.parse_bet_name(h.example) == h.example
            assert " " in h.example, "the example should show that spaces are allowed"
        elif h.label == "leg bet":
            # THE EXAMPLE IS A NAME, with spaces, which is the whole change: it resolves to
            # an id through the map the app builds from the book.
            assert " " in h.example, "the example should show a name, not a slug"
            assert parse.parse_trade_bet(
                h.example, known={h.example.lower(): "an-id"}) == "an-id"
            # AND EMPTY MEANS DETACH, which is the one thing this parser does that no
            # other one does -- every other prompt refuses a blank line.
            assert parse.parse_trade_bet("") is None
            assert parse.parse_trade_bet("   ") is None
        elif h.label == "bet":
            r = parse.parse_bet(h.example, now=NOW)
            # Not merely "it parses" -- it must parse AS SHOWN. `~` absorbs everything
            # after it, so an example writing `=8000` after `~title` swallows the
            # number into the title and teaches the wrong order. This example did
            # exactly that until the test caught it.
            assert r.allocation is not None, "the =allocation in the example was eaten"
            assert "=" not in (r.note or ""), f"the note absorbed a sigil: {r.note!r}"
        else:
            pytest.fail(f"no parser asserted for hint {h.label!r}")


def test_a_name_stops_at_the_next_sigil_and_a_note_does_not():
    """`nonreg ~Non-registered =default` set no default and said nothing.

    `~` absorbed the rest of the line, and _fold_tilde joined the following tokens' VALUES
    -- which tokenise() had already stripped the sigil from. So the name became
    "Non-registered default": one character the author typed silently deleted, and the flag it
    carried silently unset. The two `"=" not in r.name` assertions above could not catch
    it, because the `=` really was gone.

    Two fixes, both checked here. A `~` field that is a NAME stops at the next sigil, so
    the order `a`'s own hint prints works. A `~` field that is a NOTE still runs to the
    end of the line -- prose contains `@` and `=` and must survive verbatim.
    """
    # `default` IS A BARE WORD NOW, so this grammar has no second sigil left to stop at --
    # `=` carried the flag when this test was written, and taking that away was the point.
    # The instrument grammar proved the rest of this rule and went with `i`; the FILL
    # note below is what still shows a `~` field running to the end of the line.
    r = parse.parse_account("nonreg ~Non-registered default")
    assert (r.name, r.make_default) == ("Non-registered", True)
    # Either side of the id, because there is nothing positional about a flag.
    assert parse.parse_account("default nonreg").make_default is True
    with pytest.raises(parse.ParseError, match="has no ="):
        parse.parse_account("nonreg =default")

    note = parse.parse_fill("ACME 10 12.34 ~bought @ the =dip", now=NOW).note
    assert note == "bought @ the =dip", note


# --------------------------------------------------------------------------- #
# the grammar
# --------------------------------------------------------------------------- #
def test_a_fill_round_trips_through_render():
    """parse(render(row)) == row, so an edit can prefill safely."""
    row = {"ticker": "USDCO", "shares": 30.0, "price": 95.8,
           "bet_id": "growth", "on_date": "2026-09-04",
           "fee": 4.95, "note": "first tranche"}
    again = parse.parse_fill(parse.render_fill(row), now=NOW)
    assert (again.ticker, again.shares, again.price) == ("USDCO", 30.0, 95.8)
    assert (again.bet_id, again.on_date) == ("growth", "2026-09-04")
    assert (again.fee, again.note) == (4.95, "first tranche")


def test_a_sigil_mid_token_is_not_a_sigil():
    """`a!b` and `50%` must not need escaping."""
    r = parse.parse_fill("USDCO 30 95.8 !growth ~up 50% a!b", now=NOW)
    assert r.note == "up 50% a!b"


def test_an_unsupported_sigil_is_rejected_not_dropped():
    with pytest.raises(parse.ParseError, match="no ="):
        parse.parse_flow("45000 =12 @2026-08-23", now=NOW)


def test_an_unknown_ticker_is_no_longer_a_parse_error(con):
    """The registry refusal LEFT THE PARSER with `i`. A ticker the book has not seen is new,
    not wrong -- the pane resolves it at the price source, which is a stronger typo check than
    a local list. `known_tickers` is still accepted, but only so `tab` can offer them.
    """
    r = parse.parse_fill("WIDGET 1 1.0", now=NOW, known_tickers=book.known_tickers(con))
    assert r.ticker == "WIDGET"
    # An /account that does not exist IS still refused here: that is a typo the book can
    # settle by itself, and a fill filed to no sleeve vanishes from every per-account total.
    with pytest.raises(parse.ParseError, match="is not an account"):
        parse.parse_fill("WIDGET 1 1.0 /nope", now=NOW,
                         known_accounts=book.known_accounts(con))


# --------------------------------------------------------------------------- #
# the app
# --------------------------------------------------------------------------- #
async def test_it_boots_on_the_dashboard(app):
    """The FIRST screen answers "may I trade", not "what do I hold".

    The author: "i believe you should move balance up to be the dashboard." Positions was
    first, and positions is a consequence of a decision rather than an input to one.
    """
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.scope == "dashboard"
        assert app.query_one("#tabs", TabbedContent).active == "tab-dashboard"


async def test_it_shows_the_open_positions(app, con, goto):
    """Asserted against the book, not a literal.

    This said `== 3` and broke the first time a real position was recorded, which
    made a correct book look like a broken table.
    """
    expected = len(book.positions(con))
    assert expected, "the fixture book should have open positions to show"
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        assert app.scope == "positions"
        t = app.query_one("#pos-table", DataTable)
        # LEG ROWS, not every row. The table also holds a heading per bet and a section of
        # closed legs, all of them ordinary DataTable rows because it has no row spans.
        legs = [k for k in (t.coordinate_to_cell_key((r, 0)).row_key.value
                            for r in range(t.row_count))
                if k and not k.startswith(PositionsPane.INERT)]
        assert len(legs) == expected
        assert set(legs) == {p.trade_id for p in book.positions(con)}


async def test_digits_switch_panes_in_tab_order(app):
    """The digit is the tab's POSITION, so 1 is always the leftmost tab.

    READ OFF THE KEYMAP, so folding a tab away cannot leave this asserting a stale order --
    which is exactly what happened when capital moved onto the dashboard and `3` became
    bets. What is being tested is that every digit reaches the pane it advertises.
    """
    digits = [(k.key, k.action.removeprefix("show_")) for k in keymap.KEYMAP
              if k.scope == "app" and k.action.startswith("show_")]
    assert [d for d, _ in digits] == sorted(d for d, _ in digits), \
        "the digits must run 1..n in tab order"
    async with app.run_test() as pilot:
        for digit, scope in reversed(digits):
            await pilot.press(digit)
            await pilot.pause()
            assert app.scope == scope, f"{digit} opened {app.scope}"


async def test_n_records_a_fill(app, con, type_into, goto):
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await pilot.press("n")
        await pilot.pause()
        assert app.prompt.is_open
        await type_into(pilot, "USDCO 1 430.00 !growth @today")
        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, "a successful submit closes the prompt"
    row = con.execute(
        "SELECT shares, price, on_date FROM fills ORDER BY id DESC LIMIT 1").fetchone()
    assert (row["shares"], row["price"], row["on_date"]) == (1.0, 430.0, "2026-09-07")


async def test_a_rejected_line_keeps_the_text_and_says_why(app, con, type_into, goto):
    before = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await pilot.press("n")
        await type_into(pilot, "USDCO 1")           # no price
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.is_open, "a rejection must not close the prompt"
        assert app.prompt.value == "USDCO 1", "the typed text must survive"
        assert "price" in app.prompt.error
        assert "error" in app.prompt.classes
    assert con.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == before


async def test_a_fill_that_would_go_short_is_refused(app, con, type_into, goto):
    """Rule 3 is a wall. The message comes from the wall, not from the UI."""
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await pilot.press("n")
        await type_into(pilot, "USDCO -99 430.00 !growth")
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.is_open
        assert "short" in app.prompt.error


async def test_capital_entry_moves_the_scoreboard(app, blank, type_into, goto):
    """A deposit typed on the capital page reaches the dashboard's three figures.

    It used to prove this against a hand-entered statement total: deposit 45,000, declare
    a balance of 46,488, expect a 1,488 gain. There is no statement now, so the gain has
    to come from somewhere real -- a leg bought at 50 and marked at 60.
    """
    assert book.capital_in(blank) is None
    _buy(blank, "t1", "CADCO", "CAD", 10.0, 50.0)
    _mark(blank, "CADCO", "CAD", 60.0)
    async with app.run_test() as pilot:
        await goto(pilot, "dashboard")
        await pilot.press("c")
        await pilot.pause()
        await type_into(pilot, "45000 @2026-08-23 ~initial funding")
        await pilot.press("enter")
        await pilot.pause()
    assert book.capital_in(blank) == 45000.0
    n = book.net(blank)
    assert n.cash == pytest.approx(45000.0 - 500.0)
    assert n.net == pytest.approx(45100.0)
    assert n.pnl == pytest.approx(100.0)
    assert n.pnl_pct == pytest.approx(0.2222, abs=1e-3)


# --------------------------------------------------------------------------- #
# NET, DERIVED
#
# The author, 2026-09-09: "the net should be computed from cash + positions right? why do you
# bookkeeping a number i did." So there is no statement total to compare against any
# more, and these tests are the only thing standing behind the headline figure.
# --------------------------------------------------------------------------- #
def _mark(con, ticker, ccy, amount, on_date=seed.AS_OF):
    con.execute("INSERT OR REPLACE INTO prices(on_date,base,quote,amount,kind,source) "
                "VALUES (?,?,?,?,'mark','test')", (on_date, ticker, ccy, amount))


def _buy(con, tid, ticker, ccy, shares, price, *, on="2026-05-04", fee=None,
         acct="tfsa"):
    con.execute("INSERT INTO trades(id,bet_id,ticker,currency,side,status,account_id,"
                "opened) VALUES (?,NULL,?,?,'long','open',?,?)",
                # `ruleset_as_of` was the second `on`; dropped in v14 with the rules.
                (tid, ticker, ccy, acct, on))
    con.execute("INSERT INTO fills(trade_id,on_date,kind,shares,price,fee,fee_source) "
                "VALUES (?,?,'open',?,?,?,?)",
                (tid, on, shares, price, fee, None if fee is None else "statement"))


def test_a_book_with_nothing_in_it_reports_no_net_rather_than_zero(blank):
    """An empty book has not made zero dollars. It has not said."""
    n = book.net(blank)
    assert n.home == "CAD"
    assert n.contributed is None
    assert n.pnl is None and n.pnl_pct is None
    # Cash IS zero: no movement recorded really is no cash. It is `contributed` that
    # cannot be zero, because dividing by it is what produces the return.
    assert n.cash == 0.0


def test_net_is_cash_plus_positions_and_the_scoreboard_follows(blank):
    """The whole page-1 figure, in one currency, with nothing missing."""
    book.add_flow(blank, amount=1000.0, on_date="2026-05-01", account="tfsa")
    _buy(blank, "t1", "CADCO", "CAD", 10.0, 50.0, fee=4.95)
    _mark(blank, "CADCO", "CAD", 60.0)

    n = book.net(blank)
    assert n.cash == pytest.approx(1000.0 - 500.0 - 4.95)
    assert n.positions == pytest.approx(600.0)
    assert n.net == pytest.approx(1095.05)
    assert n.contributed == pytest.approx(1000.0)
    # +95.05, not +100: the commission is real money gone and the net has to feel it.
    assert n.pnl == pytest.approx(95.05)
    assert n.pnl_pct == pytest.approx(9.505)


def test_a_sell_returns_the_cash_it_raised(blank):
    """`shares` is negative on a trim, so the sign does this without a special case."""
    book.add_flow(blank, amount=1000.0, on_date="2026-05-01", account="tfsa")
    _buy(blank, "t1", "CADCO", "CAD", 10.0, 50.0)
    blank.execute("INSERT INTO fills(trade_id,on_date,kind,shares,price) "
                  "VALUES ('t1','2026-06-01','trim',-4.0,55.0)")
    _mark(blank, "CADCO", "CAD", 60.0)

    n = book.net(blank)
    assert n.cash == pytest.approx(1000.0 - 500.0 + 220.0)
    assert n.positions == pytest.approx(6 * 60.0)
    assert n.net == pytest.approx(1080.0)


def test_the_fx_move_on_the_usd_principal_is_inside_the_net(blank):
    """THE REASON EACH SLEEVE IS HELD NATIVE, and the one case that proves it.

    C$14,000 became US$10,000 and bought 100 USDCO at 100. Nothing has moved but the
    loonie: the mark is still 100 and the rate has gone 1.40 -> 1.50.

    The naive alternative -- translate every past movement at today's rate -- gives
    `1000 - 10,000*1.5 + 10,000*1.5`, i.e. no change at all, because the cash that left
    and the position that arrived are the same USD figure and cancel. Holding the sleeve
    native and translating the whole of it at once keeps the C$1,000 that was actually
    made on the principal.
    """
    book.add_flow(blank, amount=15000.0, on_date="2026-05-01", account="tfsa")
    blank.execute("INSERT INTO fx_conversions(on_date,account_id,from_ccy,from_amount,"
                  "to_ccy,to_amount,to_amount_source) VALUES ('2026-05-02','tfsa',"
                  "'CAD',14000.0,'USD',10000.0,'statement')")
    _buy(blank, "t1", "USDCO", "USD", 100.0, 100.0)
    _mark(blank, "USDCO", "USD", 100.0)

    at_140 = book.net(blank)
    assert at_140.cash == pytest.approx(1000.0)          # 15,000 - 14,000, all CAD
    assert at_140.positions == pytest.approx(10_000 * 1.4)
    assert at_140.net == pytest.approx(15_000.0)
    assert at_140.pnl == pytest.approx(0.0)

    blank.execute("INSERT OR REPLACE INTO prices(on_date,base,quote,amount,kind,source) "
                  "VALUES ('2026-09-08','USD','CAD',1.5,'fx','test')")
    at_150 = book.net(blank)
    assert at_150.net == pytest.approx(16_000.0)
    assert at_150.pnl == pytest.approx(1_000.0), \
        "the gain on the USD principal was translated away"


def test_contributed_uses_the_rate_on_the_deposits_own_date(blank):
    """What they put in is history. It must not move because the loonie did.

    Revaluing contributions at today's rate makes the return change with no deposit and
    no trade -- and the return is the number the whole page is for.
    """
    blank.execute("INSERT INTO prices(on_date,base,quote,amount,kind,source) "
                  "VALUES ('2026-05-01','USD','CAD',1.30,'fx','test')")
    blank.execute("INSERT INTO flows(on_date,amount,kind,currency,account_id) "
                  "VALUES ('2026-05-01',1000.0,'deposit','USD','tfsa')")
    assert book.net(blank).contributed == pytest.approx(1300.0)


def test_one_sleeve_with_no_rate_blanks_the_total_rather_than_dropping_it(blank):
    """A net missing a whole sleeve is not conservative, it is wrong."""
    blank.execute("DELETE FROM prices WHERE kind='fx'")
    book.add_flow(blank, amount=1000.0, on_date="2026-05-01", account="tfsa")
    _buy(blank, "t1", "USDCO", "USD", 10.0, 100.0)
    _mark(blank, "USDCO", "USD", 110.0)

    n = book.net(blank)
    assert n.net is None and n.positions is None
    assert any("USD->CAD" in w for w in n.unknown), n.unknown
    # The CAD sleeve still knows its own cash. Only the pooled figure is withheld.
    cad = next(s for s in n.sleeves if s.currency == "CAD")
    assert cad.cash == pytest.approx(1000.0)


def test_a_fill_with_no_price_blanks_only_its_own_currency(blank):
    """It cost something. Treating that as free would overstate cash by the purchase."""
    book.add_flow(blank, amount=1000.0, on_date="2026-05-01", account="tfsa")
    _buy(blank, "t1", "CADCO", "CAD", 10.0, None)
    _mark(blank, "CADCO", "CAD", 60.0)
    _buy(blank, "t2", "USDCO", "USD", 1.0, 100.0)
    _mark(blank, "USDCO", "USD", 110.0)

    n = book.net(blank)
    assert n.cash is None and n.net is None
    assert any("no price" in w for w in n.unknown), n.unknown
    usd = next(s for s in n.sleeves if s.currency == "USD")
    assert usd.cash == pytest.approx(-100.0), "the USD sleeve was priced and stays known"


def test_the_offset_row_moves_cash_and_needs_no_other_table(blank):
    """The author: "we will just add a special offset entry ... if the diff is small, it's
    good enough." Dividends, fees and interest all arrive through this one door.
    """
    book.add_flow(blank, amount=1000.0, on_date="2026-05-01", account="tfsa")
    book.add_adjustment(blank, amount=12.34, on_date="2026-06-01", note="dividends")
    book.add_adjustment(blank, amount=-2.34, on_date="2026-06-02", note="a fee")
    n = book.net(blank)
    assert n.cash == pytest.approx(1010.0)
    # AND IT IS NOT CONTRIBUTED CAPITAL. An offset is money the account earned or paid,
    # so counting it as a deposit would hide exactly that much P&L.
    assert n.contributed == pytest.approx(1000.0)
    assert n.pnl == pytest.approx(10.0)


def test_the_inverse_rate_counts_so_a_usd_home_currency_works(blank, monkeypatch):
    """The fetcher records USD->CAD, because that is the pair Yahoo quotes.

    A book whose home currency is USD found no CAD->USD row and blanked its own
    dashboard. 1/rate is the same fact written the other way round.
    """
    monkeypatch.setattr(config, "profile", lambda: config.Profile(
        name="Test", home_currency="USD"))
    # SPELLED OUT, because an unqualified deposit lands in the HOME currency -- which
    # this test has just changed. `add_flow(amount=1400)` here would record US$1,400 and
    # the assertion below would pass for the wrong reason.
    book.add_flow(blank, amount=1400.0, on_date="2026-05-01", account="tfsa",
                  currency="CAD")
    n = book.net(blank)
    assert n.home == "USD"
    assert n.cash == pytest.approx(1000.0, abs=1e-6)   # C$1,400 at 1.40


async def test_o_writes_an_offset_and_it_is_not_contributed_capital(app, blank,
                                                                   type_into, goto):
    """The whole reconciliation story, through the keyboard.

    The author: "we will just add a special offset entry into the account balance as i gave you
    the number to reconcile the difference." The assertion that matters is the last one:
    an offset moves cash and NOT contributed capital, so it shows up as return. Writing it
    to `flows` instead would hide exactly its own size of P&L.
    """
    book.add_flow(blank, amount=1000.0, on_date="2026-05-01", account="tfsa")
    async with app.run_test() as pilot:
        await goto(pilot, "dashboard")
        await pilot.press("o")
        await pilot.pause()
        assert app.prompt.label == "offset"
        await type_into(pilot, "12.34 @2026-06-01 ~dividends")
        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, app.prompt.error
    row = blank.execute("SELECT amount, currency, notes FROM adjustments").fetchone()
    assert tuple(row) == (12.34, "CAD", "dividends")
    n = book.net(blank)
    assert n.cash == pytest.approx(1012.34)
    assert n.contributed == pytest.approx(1000.0), "an offset is not a deposit"
    assert n.pnl == pytest.approx(12.34)


async def test_the_capital_page_lists_flows_and_offsets_together(app, blank, goto):
    """One page, because from where the author sits both are a number and a date.

    The row KEY carries which table it came from. Both tables number from 1, so a bare id
    named two different rows -- and `x` on an offset would have deleted a deposit.
    """
    book.add_flow(blank, amount=1000.0, on_date="2026-05-01", account="tfsa")
    book.add_adjustment(blank, amount=-4.95, on_date="2026-05-02", note="a fee")
    async with app.run_test() as pilot:
        await goto(pilot, "dashboard")
        await pilot.pause()
        t = app.query_one("#cap-table", DataTable)
        keys = [t.coordinate_to_cell_key((r, 0)).row_key.value
                for r in range(t.row_count)]
    assert keys == ["flow:1", "offset:1"], keys


async def test_x_on_an_offset_deletes_the_offset_and_not_the_deposit(app, blank, goto):
    """The bug the composite key exists to prevent, asserted rather than assumed."""
    book.add_flow(blank, amount=1000.0, on_date="2026-05-01", account="tfsa")
    book.add_adjustment(blank, amount=-4.95, on_date="2026-05-02", note="a fee")
    async with app.run_test() as pilot:
        await goto(pilot, "dashboard")
        await _point_at(pilot, app, "#cap-table", "offset:1")
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
    assert blank.execute("SELECT COUNT(*) FROM adjustments").fetchone()[0] == 0
    assert blank.execute("SELECT COUNT(*) FROM flows").fetchone()[0] == 1, \
        "the deposit was collateral damage"


def test_a_bare_currency_word_says_which_pile_a_deposit_lands_in(blank):
    """`1000 USD` — a bare word, not a sigil.

    `=USD` would have been a sixth meaning for `=`, and the standing complaint is that
    there are already too many: "your entry system is way too complicated." It is
    unambiguous because neither CAD nor USD can be a number, an account or a date.
    """
    r = parse.parse_flow("1000 USD @2026-05-01 ~a USD top-up", now=NOW)
    assert (r.amount, r.currency, r.note) == (1000.0, "USD", "a USD top-up")
    # Lowercase too, and absent means the home currency rather than a guess.
    assert parse.parse_flow("1000 usd", now=NOW).currency == "USD"
    assert parse.parse_flow("1000", now=NOW).currency is None
    with pytest.raises(parse.ParseError, match="one currency"):
        parse.parse_flow("1000 USD CAD", now=NOW)


# --------------------------------------------------------------------------- #
# closed legs, and where the return came from
# --------------------------------------------------------------------------- #
def test_a_closed_round_trip_reports_what_it_made(con):
    """The fixture's CADCO leg: 100 in at 10.00, 100 out at 11.00.

    Until v11 the desk had no view of a closed trade at all -- so on a book where most of
    the return had already been banked, most of it was reachable only from sqlite3.
    """
    legs = {c.trade_id: c for c in book.closed_legs(con)}
    leg = legs["2026-03-02-cadco"]
    assert (leg.qty, leg.entry, leg.exit) == (100.0, 10.0, 11.0)
    assert leg.cost == pytest.approx(1000.0)
    assert leg.realised == pytest.approx(100.0)
    assert leg.realised_pct == pytest.approx(10.0)
    assert leg.currency == "CAD", "never translated -- a leg's result is in its own money"


def test_an_unpriced_fill_withholds_the_whole_legs_result(con):
    """Short by whatever one fill cost is not a small error. It is the entire answer."""
    tid = "2026-03-02-cadco"
    con.execute("UPDATE fills SET price=NULL WHERE trade_id=? AND shares>0", (tid,))
    leg = next(c for c in book.closed_legs(con) if c.trade_id == tid)
    assert leg.realised is None and leg.cost is None
    assert leg.realised_pct is None


def test_a_trim_banks_money_without_closing_anything(con):
    """The figure that makes the two realised totals agree.

    The fixture trims 20 USDCO at 110 against an average entry of 100 on a leg that still
    holds 40 shares. That US$200 is in the dashboard's `realised` line and in no closed
    leg, so without naming it the two look like they disagree by exactly that much.
    """
    assert book.banked_in_open_legs(con)["USD"] == pytest.approx(200.0)
    # And it is DERIVED as the remainder, so the three figures cannot drift apart.
    closed = sum(c.realised for c in book.closed_legs(con)
                 if c.currency == "USD" and c.realised is not None)
    sleeve = next(s for s in book.net(con).sleeves if s.currency == "USD")
    assert sleeve.realised == pytest.approx(closed + 200.0)


def test_the_return_splits_into_parts_that_add_back_up(con):
    """FOUR NUMBERS THAT MUST SUM TO THE HEADLINE, or they send you hunting a phantom.

    A first attempt built realised from closed trades and unrealised from cost basis, and
    missed by the size of every trim taken on a leg that is still open -- because such a trim
    belongs to neither bucket. `trading` is `traded + positions`, which needs no decision about
    which
    shares were sold, and realised is the remainder after unrealised.
    """
    n = book.net(con)
    assert n.ties(), (n.trading, n.income, n.offsets, n.currency_effect, n.pnl)
    assert n.realised + n.unrealised == pytest.approx(n.trading)


def test_the_currency_line_holds_the_gain_on_a_converted_principal(blank):
    """C$14,000 became US$10,000 at 1.40; the rate is now 1.50. That is C$1,000, and it
    belongs to the CURRENCY, not to any trade."""
    book.add_flow(blank, amount=15000.0, on_date="2026-05-01", account="tfsa")
    blank.execute("INSERT INTO fx_conversions(on_date,account_id,from_ccy,from_amount,"
                  "to_ccy,to_amount,to_amount_source) VALUES ('2026-05-02','tfsa',"
                  "'CAD',14000.0,'USD',10000.0,'statement')")
    blank.execute("INSERT OR REPLACE INTO prices(on_date,base,quote,amount,kind,source) "
                  "VALUES ('2026-09-08','USD','CAD',1.5,'fx','test')")
    n = book.net(blank)
    assert n.currency_effect == pytest.approx(1000.0)
    assert n.trading == pytest.approx(0.0), "nothing was traded"
    assert n.ties()
    # PER SLEEVE IT IS MEANINGLESS: -14,000 on one side and +15,000 on the other, because
    # a conversion is one event recorded as two halves. Only the sum is a fact.
    per = {s.currency: s.currency_effect for s in n.sleeves}
    assert per["CAD"] == pytest.approx(-14000.0)
    assert per["USD"] == pytest.approx(15000.0)


async def test_the_dashboard_shows_where_the_return_came_from(app, con):
    async with app.run_test(size=(110, 44)) as pilot:
        await pilot.pause()
        lines = _body(app)
    labels = [ln.strip().split()[0] for ln in lines if ln.strip()]
    assert "realised" in labels and "unrealised" in labels, lines
    # AND THE SPLIT IS SUPPRESSED WHEN IT WOULD NOT ADD UP. `ties()` is checked before
    # anything prints: four numbers that almost sum to the headline are worse than one.
    assert book.net(con).ties()


async def test_the_closed_section_is_rendered_at_all(app, con, goto):
    """THE TEST THAT WAS MISSING, and it cost an hour.

    The method was called `_closed`, and Textual's MessagePump.__init__ does
    `self._closed: bool = False` -- so an instance attribute shadowed it and the call
    raised "'bool' object is not callable" inside a message handler, where Textual swallows
    the error. The pane rendered its open legs, stopped, and said nothing. Every other test
    passed with a whole section of the page missing, because none of them looked for it.
    """
    expected = book.closed_legs(con)
    assert expected, "the fixture needs a closed trade for this to mean anything"
    async with app.run_test(size=(120, 44)) as pilot:
        await goto(pilot, "positions")
        t = app.query_one("#pos-table", DataTable)
        keys = [t.coordinate_to_cell_key((r, 0)).row_key.value
                for r in range(t.row_count)]
    for leg in expected:
        assert f"{PositionsPane.INERT}leg:{leg.trade_id}" in keys, leg.trade_id
    assert any(k.startswith(f"{PositionsPane.INERT}closed:") for k in keys), \
        "a heading per currency, so the realised total is unambiguous"


async def test_a_group_heading_takes_no_key(app, con, goto):
    """Headings and closed legs are ordinary rows -- DataTable has no spans.

    So `x` on the "CLOSED · USD" heading would look up a trade by that name and fail
    silently, and on a closed leg it would delete a fill out of a finished round trip.
    """
    before = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    async with app.run_test(size=(120, 44)) as pilot:
        await goto(pilot, "positions")
        t = app.query_one("#pos-table", DataTable)
        heading = next(r for r in range(t.row_count)
                       if str(t.coordinate_to_cell_key((r, 0)).row_key.value
                              ).startswith(PositionsPane.INERT))
        t.cursor_coordinate = (heading, 0)
        await pilot.pause()
        for key in ("x", "enter", "s"):
            await pilot.press(key)
            await pilot.pause()
            assert not app.prompt.is_open, f"{key} armed a prompt on a heading"
            assert app._confirm is None, f"{key} asked to delete a heading"
    assert con.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == before


async def test_the_cursor_never_opens_on_a_heading(app, goto):
    """Row 0 IS a heading now, so opening the tab used to park the cursor where `n`, `x`,
    `enter` and `s` all did nothing -- silently, which reads as the pane being broken."""
    async with app.run_test(size=(120, 44)) as pilot:
        await goto(pilot, "positions")
        t = app.query_one("#pos-table", DataTable)
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        assert not key.startswith(PositionsPane.INERT), key


async def test_the_legs_sit_under_their_bet(app, con, goto):
    """The link the author asked for: "let me link bets and trades"."""
    async with app.run_test(size=(120, 44)) as pilot:
        await goto(pilot, "positions")
        t = app.query_one("#pos-table", DataTable)
        keys = [t.coordinate_to_cell_key((r, 0)).row_key.value
                for r in range(t.row_count)]
    owner = {p.trade_id: p.bet_id for p in book.positions(con)}
    current = None
    for key in keys:
        if key.startswith(f"{PositionsPane.INERT}bet:"):
            current = key.split(":", 1)[1]
            current = None if current == "none" else current
        elif key in owner:
            assert owner[key] == current, f"{key} is under {current}, not its own bet"


# --------------------------------------------------------------------------- #
# bet exposure
#
# The author: "i do want the bet exposure percentage in the bets' tab. i dont know if it's
# against the derived net tbh." It is against the derived net, and the reason a bare
# percentage looked useless on their book is stated beside it rather than carved out of the
# denominator -- see book.parked().
# --------------------------------------------------------------------------- #
def test_a_bets_weight_is_its_market_value_over_the_derived_net(blank):
    book.add_flow(blank, amount=10_000.0, on_date="2026-05-01", account="tfsa")
    blank.execute("INSERT INTO bets(id,status,opened,thesis,main_support,"
                  "allocation_base,allocation_ccy,allocation_set_on) VALUES "
                  "('b1','forming','2026-05-01','t','m',4000.0,'CAD','2026-05-01')")
    _buy(blank, "t1", "CADCO", "CAD", 20.0, 50.0)          # 1,000 spent
    blank.execute("UPDATE trades SET bet_id='b1' WHERE id='t1'")
    _mark(blank, "CADCO", "CAD", 60.0)                     # now worth 1,200

    b = next(x for x in book.bets(blank) if x.id == "b1")
    n = book.net(blank)
    assert n.net == pytest.approx(10_200.0)                # 9,000 cash + 1,200
    # THE WEIGHT IS AT MARKET. The dropped view measured cost, which is what was committed
    # rather than what is at risk.
    assert b.value == pytest.approx(1_200.0)
    assert b.weight_pct == pytest.approx(100 * 1200 / 10200)
    # BUT `filled` IS AT COST, and the two bases on one bet is the point of this pair. The author:
    # "so entry cost/allocation." 1,000 was spent of a 4,000 budget -- 25%, not the 30% the
    # market value would give. Only cost answers "how much budget is left".
    assert b.filled_pct == pytest.approx(25.0)             # 1,000 spent of 4,000


def test_a_bet_holding_an_unpriceable_leg_withholds_its_weight(blank):
    """A bet with one leg it cannot value used to report a CONFIDENT weight that
    understated its own market value, which is the worst of the three outcomes."""
    blank.execute("DELETE FROM prices WHERE kind='fx'")
    book.add_flow(blank, amount=10_000.0, on_date="2026-05-01", account="tfsa")
    blank.execute("INSERT INTO bets(id,status,opened) VALUES ('b1','forming','2026-05-01')")
    _buy(blank, "t1", "CADCO", "CAD", 20.0, 50.0)
    _buy(blank, "t2", "USDCO", "USD", 10.0, 100.0)         # no rate: unvaluable
    blank.execute("UPDATE trades SET bet_id='b1' WHERE id IN ('t1','t2')")
    _mark(blank, "CADCO", "CAD", 60.0)
    _mark(blank, "USDCO", "USD", 110.0)

    b = next(x for x in book.bets(blank) if x.id == "b1")
    assert b.unpriced_legs == 1
    assert b.value is None and b.weight_pct is None
    # THE SPECIFIC BUG THE DROPPED VIEW HAD: `COALESCE(fx, 1.0)` valued the USD leg at par,
    # so this returned a number roughly 38% wrong and looked entirely ordinary.
    assert b.filled_pct is None


def test_parked_money_is_named_and_not_carved_out_of_the_denominator(blank):
    """The author's book is 89% money-market, which is why every bet reads ~1% of net.

    Stated as a fact beside the weights. Dividing by net-less-parking instead would revive
    the risk-free carve-out they removed -- "Drop the concept entirely" -- and would make the
    exposure figure depend on how each instrument happened to be classified.
    """
    book.add_flow(blank, amount=10_000.0, on_date="2026-05-01", account="tfsa")
    _buy(blank, "t1", "PARK", "USD", 50.0, 100.0)          # money-market in the fixture
    _mark(blank, "PARK", "USD", 100.0)
    _buy(blank, "t2", "CADCO", "CAD", 20.0, 50.0)
    _mark(blank, "CADCO", "CAD", 60.0)
    assert book.parked(blank) == pytest.approx(50 * 100 * seed.FX_USDCAD)
    # The denominator is untouched by it.
    n = book.net(blank)
    assert n.positions == pytest.approx(50 * 100 * seed.FX_USDCAD + 1200.0)


async def test_the_bets_pane_names_its_columns(app, goto):
    """The header has to name what a percentage is a share OF.

    `weight` did not, which is exactly why 47.3% read as arbitrary to the author and why the column
    was rewritten three times before the header was the thing that changed.
    """
    async with app.run_test(size=(126, 46)) as pilot:
        await goto(pilot, "bets")
        t = app.query_one("#bet-table", DataTable)
        labels = [str(getattr(c.label, "plain", c.label)) for c in t.columns.values()]
        head = " ".join(_plain(app, "#bet-head"))

    assert labels == ["opened", "bet", "status", "legs", "allocated", "spent",
                      "of plan", "P&L", "%"], labels
    assert "in bets" in head and "of" in head, head


async def test_the_of_plan_column_is_a_share_of_the_other_budgets(app, con, goto):
    """The author: "essentially we are giving each bet x amount of cash to play out. this has
    nothing
    to do with total net worth."

    That is the better argument and it is why this column changed twice. It was market/net,
    which made it a second view of the `spent` column beside it; then allocation/net, which
    answered "how much of the book is committed"; and now allocation over the total allocated,
    because an allocation is a BUDGET and the comparison that means something is against the
    other budgets.

    The net-worth question is not lost -- the dashboard's portfolio table carries `of net`, so
    "94.5% of the book is committed" still has a home. Each page's denominator matches its own
    subject.
    """
    async with app.run_test(size=(126, 46)) as pilot:
        await goto(pilot, "bets")
        t = app.query_one("#bet-table", DataTable)
        labels = [str(getattr(c.label, "plain", c.label)) for c in t.columns.values()]
        rows = {t.coordinate_to_cell_key((r, 0)).row_key.value: t.get_row_at(r)
                for r in range(t.row_count)}

    assert "of plan" in labels, labels
    assert "weight" not in labels, "`weight` named no denominator, which is what confused them"
    col = labels.index("of plan")

    live = [b for b in book.bets(con) if book.is_live(b.status) and b.allocation]
    total = sum(b.allocation for b in live)
    assert total, "fixture needs an allocated live bet"
    shown = {}
    for b in book.bets(con):
        if b.id not in rows:
            continue
        got = _cell_text(rows[b.id][col])
        if b.allocation:
            assert got == share(100.0 * b.allocation / total), (b.id, got)
            shown[b.id] = 100.0 * b.allocation / total
        else:
            # NOT GATED ON HAVING LEGS OPEN any more: an em dash means no allocation was
            # declared, which is the only case with no share of the plan to state.
            assert got == NA_DASH, (b.id, got)
    assert sum(shown.values()) == pytest.approx(100.0), (
        "the shares of the plan must sum to the whole plan")


def test_finished_bets_do_not_dilute_the_plan(con):
    """The denominator is the LIVE budgets only.

    Including finished ones would shrink every live bet's share by however much history sits
    behind it, so the figure would drift downwards as the book ages rather than when they re-plans
    -- which is the opposite of what a share of the current plan should do.
    """
    live = [b for b in book.bets(con) if book.is_live(b.status) and b.allocation]
    assert live, "fixture needs an allocated live bet"
    # Give a FINISHED bet a large allocation; the live shares must not move.
    before = {b.id: b.of_allocated_pct for b in live}
    # CREATED HERE, because the fixture has none -- both its bets are live. Straight to
    # `retired` via SQL: set_bet_status refuses to finish a bet holding open legs, and this
    # one holds nothing, so the writer is not what is under test.
    book.add_bet(con, bet_id="old-idea", name="Old Idea", on_date="2026-01-01")
    con.execute("UPDATE bets SET status='retired', allocation_base=?, "
                "allocation_ccy='CAD', allocation_set_on='2026-01-01' WHERE id='old-idea'",
                (999_999.0,))
    con.commit()
    assert any(not book.is_live(b.status) and b.allocation for b in book.bets(con))
    after = {b.id: b.of_allocated_pct
             for b in book.bets(con) if book.is_live(b.status) and b.allocation}
    assert after == before, "a finished bet's budget diluted the live plan"


async def test_a_pane_that_cannot_be_drawn_says_so_instead_of_going_blank(app,
                                                                         monkeypatch):
    """THE GUARD, and it exists because three name collisions were swallowed in one session.

    `_closed` shadowed Textual's MessagePump._closed, a local `share` shadowed the imported
    share(), and `_render` WAS Textual's Widget._render. The first two raised inside a
    message handler where Textual logs and continues, so a pane rendered nothing and the
    whole suite stayed green.
    """
    app.PROPAGATE_RENDER_ERRORS = False
    from desk.tui.panes import BetsPane
    monkeypatch.setattr(BetsPane, "redraw",
                        lambda self: (_ for _ in ()).throw(TypeError("boom")))
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.press("3")
        await pilot.pause()
        head = " ".join(_plain(app, "#bet-head"))
        assert app.is_running, "a failed render must not take the desk down"
    assert "could not be drawn" in head and "boom" in head, head


# --------------------------------------------------------------------------- #
# per-bet P&L
#
# The author: "on the bet page, you should list out the pnl per bet."
# --------------------------------------------------------------------------- #
def test_a_bets_pnl_is_realised_plus_unrealised(blank):
    blank.execute("INSERT INTO bets(id,status,opened) VALUES ('b1','forming','2026-05-01')")
    book.add_flow(blank, amount=10_000.0, on_date="2026-05-01", account="tfsa")
    # one leg closed at a profit, one still open at a loss
    _buy(blank, "t1", "CADCO", "CAD", 100.0, 10.0, on="2026-05-04")
    blank.execute("INSERT INTO fills(trade_id,on_date,kind,shares,price) "
                  "VALUES ('t1','2026-06-01','close',-100.0,11.0)")
    blank.execute("UPDATE trades SET status='closed', closed='2026-06-01' "
                  "WHERE id='t1'")
    _buy(blank, "t2", "CADCO", "CAD", 20.0, 50.0, on="2026-07-01")
    blank.execute("UPDATE trades SET bet_id='b1' WHERE id IN ('t1','t2')")
    _mark(blank, "CADCO", "CAD", 45.0)

    b = next(x for x in book.bets(blank) if x.id == "b1")
    assert b.realised == pytest.approx(100.0)        # 1,100 out for 1,000 in
    assert b.unrealised == pytest.approx(-100.0)     # 20 @ 50 now marked 45
    assert b.pnl == pytest.approx(0.0)
    assert b.invested == pytest.approx(2_000.0)      # 1,000 closed + 1,000 open
    assert b.pnl_pct == pytest.approx(0.0)
    assert (b.legs, b.closed_legs) == (1, 1)


def test_a_bets_pnl_is_withheld_when_only_half_of_it_is_known(blank):
    """Half a P&L is not a P&L. A bet whose realised side cannot be valued must not print
    its unrealised side as though that were the answer."""
    blank.execute("DELETE FROM prices WHERE kind='fx'")
    blank.execute("INSERT INTO bets(id,status,opened) VALUES ('b1','forming','2026-05-01')")
    _buy(blank, "t1", "USDCO", "USD", 10.0, 100.0, on="2026-05-04")
    blank.execute("INSERT INTO fills(trade_id,on_date,kind,shares,price) "
                  "VALUES ('t1','2026-06-01','close',-10.0,120.0)")
    blank.execute("UPDATE trades SET status='closed', closed='2026-06-01', "
                  "bet_id='b1' WHERE id='t1'")
    b = next(x for x in book.bets(blank) if x.id == "b1")
    assert b.unvalued_closed_legs == 1
    assert b.realised is None and b.pnl is None and b.pnl_pct is None


def test_every_bets_realised_sums_to_the_dashboards(con):
    """ONE CONVENTION FOR "REALISED", and this is what enforces it.

    Per-bet realised was first built at each leg's CLOSE-DATE rate -- more honest
    historically, but it made the sum over bets disagree with the dashboard by the currency
    drift since, which on a multi-currency bet is a few dollars. Two answers to one word,
    differing by an amount
    nobody can attribute, is what erodes trust in a derived figure. So both use today's
    rate, and this asserts they meet.
    """
    n = book.net(con)
    per_bet = sum(b.realised for b in book.bets(con) if b.realised is not None)
    loose = 0.0
    for leg in book.closed_legs(con):
        if leg.bet_id is None and leg.realised is not None:
            rate, _ = book.fx_rate(con, leg.currency, n.home)
            loose += leg.realised * rate
    # A trim banks money inside a leg that is still open; it belongs to no closed leg.
    for ccy, amount in book.banked_in_open_legs(con).items():
        rate, _ = book.fx_rate(con, ccy, n.home)
        loose += amount * rate
    assert per_bet + loose == pytest.approx(n.realised, abs=0.01)


async def test_the_thesis_is_readable_under_its_own_heading(app, con, goto):
    """IT USED TO BE ONE SHORTENED LINE ending in an ellipsis, which said a thesis existed
    and nothing about what it claimed.

    The author: "for each bet, i still want the data to be more organized down here. maybe you can
    add sections or something." Under a heading there is room to WRAP it, which is the only
    reason to keep prose on screen at all. Still bounded at six lines so a three-paragraph
    note cannot push the table off a short terminal.
    """
    long_note = ("First sentence of the thesis. " * 40).strip()
    con.execute("UPDATE bets SET notes=? WHERE id='growth'", (long_note,))
    con.commit()

    async with app.run_test(size=(126, 46)) as pilot:
        await goto(pilot, "bets")
        await _point_at(pilot, app, "#bet-table", "growth")
        lines = _plain(app, "#bet-detail")

    assert any("THESIS" in x for x in lines), lines
    body = [x for x in lines if x.strip().startswith("First sentence")]
    assert len(body) >= 2, f"the thesis was not wrapped over several lines: {body}"
    assert len(body) <= 6, f"more than six lines of prose: {len(body)}"
    assert any("characters in all" in x for x in lines), (
        "a truncated thesis must say how much is left and how to read it")
    assert any(x.strip() == "HOLDINGS" for x in lines), "the sections are named"


# --------------------------------------------------------------------------- #
# `t`: switching themes
#
# The author: "can i switch themes, just like daylogs?"
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# an unknown ticker identifies itself
#
# The author: "i run into the this symbol is not in the registry problem again. why not
# automatically check it up and add it to the registry as i enter? i mean seems weird that
# we dont do this."
# --------------------------------------------------------------------------- #
def _found(symbol, currency, kind="etf", name="A Fund", exchange="Toronto", ticker=None):
    return {"ticker": ticker or symbol.split(".")[0], "quote_symbol": symbol,
            "currency": currency, "kind": kind, "name": name, "exchange": exchange}


async def test_an_unknown_ticker_registers_itself_from_the_quote(app, con, type_into,
                                                                goto):
    """The wall becomes a CHECK. It refused a genuinely new holding in exactly the same words
    as a typo, because it only ever asked whether the book had SEEN the ticker."""
    assert "ZGLD" not in book.known_tickers(con)
    # `.TO` is what makes this worth doing: the author types the name the book uses and Yahoo only
    # knows the suffixed form, which is the entire reason quote_symbol exists.
    app.quote_lookup = lambda t: [_found("ZGLD.TO", "CAD",
                                         name="BMO Gold Bullion ETF (CAD Units)")]
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await pilot.press("n")
        await pilot.pause()
        await type_into(pilot, "ZGLD 10 65 !growth /tfsa")
        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, app.prompt.error

    row = con.execute("SELECT currency, kind, quote_symbol, name FROM instruments "
                      "WHERE ticker='ZGLD'").fetchone()
    assert tuple(row) == ("CAD", "etf", "ZGLD.TO",
                          "BMO Gold Bullion ETF (CAD Units)")
    held = next(p for p in book.positions(con) if p.ticker == "ZGLD")
    assert (held.qty, held.entry, held.account) == (10.0, 65.0, "tfsa")


async def test_two_listings_open_a_choice_rather_than_a_refusal(app, con, type_into,
                                                                goto):
    """The author: "when you have conflicts, like COIN and COIN.TO, raise a prompt or something and
    let me tab through the ticker name."

    NOT HYPOTHETICAL, which is why the lookup returns a list at all:

        CBIL      Corgi 3-12 Month T-Bill ETF     USD, Cboe US
        CBIL.TO   Global X 0-3 Month T-Bill ETF   CAD, Toronto

    They hold the Canadian one. Taking the first hit would have priced their holding off an
    American fund forever -- a wrong number that looks entirely ordinary. INTC and TQQQ have
    the same shape, so this is the common case rather than an edge.
    """
    app.quote_lookup = lambda t: [
        _found("CBIL", "USD", name="Corgi 3-12 Month T-Bill ETF", exchange="Cboe US"),
        _found("CBIL.TO", "CAD", name="Global X 0-3 Month T-Bill ETF CAD"),
    ]
    said = []
    async with app.run_test() as pilot:
        app.notify = lambda msg, **kw: said.append(str(msg))
        await goto(pilot, "positions")
        await pilot.press("n")
        await pilot.pause()
        await type_into(pilot, "CBIL 150 50.04 /tfsa")
        await pilot.press("enter")
        await pilot.pause()

        # A QUESTION, NOT A REFUSAL. The prompt becomes the choice, prefilled with the first
        # candidate, and nothing has been written yet.
        assert app.prompt.is_open and app.prompt.label == "listing", app.prompt.label
        assert app.prompt.value == "CBIL"
        assert not [p for p in book.positions(con) if p.ticker == "CBIL"]

        # `tab` cycles the candidates -- the pool comes from the pane, not the book, because
        # they are listings a lookup just found and none of them is recorded.
        await pilot.press("tab")
        await pilot.pause()
        assert app.prompt.value == "CBIL.TO", app.prompt.value

        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, app.prompt.error

    # THE CANADIAN ONE, because that is what they picked, and the fill they already typed is
    # replayed rather than retyped.
    row = con.execute("SELECT currency, kind, quote_symbol, name FROM instruments "
                      "WHERE ticker='CBIL'").fetchone()
    assert tuple(row) == ("CAD", "etf", "CBIL.TO", "Global X 0-3 Month T-Bill ETF CAD")
    held = next(p for p in book.positions(con) if p.ticker == "CBIL")
    assert (held.qty, held.entry, held.account) == (150.0, 50.04, "tfsa")

    # The toast said WHICH SECURITY each candidate is -- the part a symbol cannot tell you --
    # and offered no default: for CBIL the first candidate is the American fund, so
    # recommending it would recommend the wrong one.
    note = " | ".join(said)
    assert "Corgi" in note and "Global X" in note, note


async def test_an_abandoned_listing_choice_does_not_replay_the_fill_later(app, con,
                                                                         type_into, goto):
    """`_pending_fill` is armed state, and armed state that outlives its prompt is how a
    later keystroke writes a row from a line typed minutes earlier.

    It is listed in Pane._ARMED with `_editing` and `_stopping`, so cancel_edit clears it --
    the same fix those two already have, applied before it could bite.
    """
    app.quote_lookup = lambda t: [_found("CBIL", "USD", exchange="Cboe US"),
                                  _found("CBIL.TO", "CAD")]
    before = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await pilot.press("n")
        await pilot.pause()
        await type_into(pilot, "CBIL 150 50.04 /tfsa")
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.label == "listing"
        await pilot.press("escape")
        await pilot.pause()
        pane = app.query_one("#positions")
        assert pane._pending_fill is None, "an abandoned choice kept the fill armed"
        # A fresh fill must be a fresh fill, not the stashed one.
        await pilot.press("n")
        await pilot.pause()
        await type_into(pilot, "USDCO 1 430.00 !growth")
        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, app.prompt.error
    assert con.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == before + 1
    assert not [p for p in book.positions(con) if p.ticker == "CBIL"]


def test_there_is_no_instrument_key_at_all(con):
    """The author: "Dont ever let me use that instrument button. that's not a good UX."

    The registry is a fact the TOOL needs -- a currency to translate with, a symbol to fetch
    by -- and none of it is a decision about trading. Every field now comes from the quote.
    """
    assert not any(k.key == "i" for k in keymap.KEYMAP), "the `i` key is back"
    assert not any(h.label == "instrument" for h in keymap.HINTS)
    assert not hasattr(parse, "parse_instrument")
    from desk.tui import panes
    assert not hasattr(panes.PositionsPane, "key_instrument")


async def test_a_symbol_nobody_quotes_is_still_refused(app, con, type_into, goto):
    """The typo check the registry was standing in for, done properly: a made-up symbol
    404s at the price source, which is a stronger statement than "this book has not seen
    it"."""
    app.quote_lookup = lambda t: []
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await pilot.press("n")
        await pilot.pause()
        await type_into(pilot, "NOSUCHTHING 1 1 /tfsa")
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.is_open
        assert "not a symbol" in app.prompt.error, app.prompt.error
    assert "NOSUCHTHING" not in book.known_tickers(con)


async def test_a_known_ticker_never_asks_the_network(app, con, type_into, goto):
    """The lookup is for a ticker the book has never seen, which is a handful of times in its
    life. Asking on every fill would put an HTTP request in the path of the commonest write.
    """
    calls = []
    app.quote_lookup = lambda t: calls.append(t) or []
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await pilot.press("n")
        await pilot.pause()
        await type_into(pilot, "USDCO 1 430.00 !growth")
        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, app.prompt.error
    assert calls == [], f"looked up a ticker already in the registry: {calls}"


def test_a_leveraged_fund_is_never_auto_registered_as_one(con):
    """Yahoo reports SOXL as a plain 'ETF' -- leverage is not in the metadata, and the schema
    refuses a leveraged-etf without a factor. So auto-registration never claims one, and a
    leveraged fund still needs `i` and its factor typed."""
    from desk import prices as bookprices
    assert "leveraged-etf" not in bookprices._KIND_OF.values()
    got = book.register_from_quote(
        con, "TQQQ", lookup=lambda t: [_found("TQQQ", "USD", name="ProShares UltraPro QQQ",
                                              exchange="NasdaqGM")])
    assert got["kind"] == "etf"
    row = con.execute("SELECT kind, leverage_factor FROM instruments "
                      "WHERE ticker='TQQQ'").fetchone()
    assert tuple(row) == ("etf", None)


def test_a_third_currency_is_refused_at_the_lookup(con):
    """A currency with no rate anybody fetches would book every figure at par. Refused where
    the cause is visible, not at the FX lookup where the symptom is a blank column."""
    with pytest.raises(book.DeskError, match="not a symbol"):
        book.register_from_quote(con, "NESN", lookup=lambda t: [])


async def test_the_registration_is_announced_so_it_can_be_corrected(app, con, type_into,
                                                                   goto):
    """The desk wrote a row on their behalf, choosing a currency, a kind and a quote symbol --
    all load-bearing, and the kind is the one it can only guess. A row nobody mentions is a
    row nobody knows to correct."""
    app.quote_lookup = lambda t: [_found("ZGLD.TO", "CAD", name="BMO Gold Bullion ETF")]
    seen = []
    async with app.run_test() as pilot:
        app.notify = lambda msg, **kw: seen.append(str(msg))
        await goto(pilot, "positions")
        await pilot.press("n")
        await pilot.pause()
        await type_into(pilot, "ZGLD 10 65 !growth /tfsa")
        await pilot.press("enter")
        await pilot.pause()
    said = " | ".join(seen)
    assert "registered ZGLD" in said and "CAD" in said and "ZGLD.TO" in said, said
    assert "press i to correct" in said.lower(), said


# --------------------------------------------------------------------------- #
# charts
#
# The author: "on the bet page, probably allocation per bet. this is a pie chart."
# --------------------------------------------------------------------------- #
def test_ranked_bars_carry_label_share_amount_and_rank(caplog):
    """ONE BAR PER ROW, which is what replaced the pie. The author: "for such pie chart they just
    stack each on top of each other."

    Every row is self-describing, so there is no legend and no colour vocabulary -- which is
    also why this needs no palette and the pie did.
    """
    rows = charts.ranked_bars([("bigger-bet", 20000.0),
                               ("smaller-bet", 10000.0)], width=78)
    assert len(rows) == 2
    first, second = rows[0].plain, rows[1].plain
    assert "bigger-bet" in first and "20,000.00" in first
    assert "66.7%" in first and "33.3%" in second
    # The larger share gets the longer bar, which is the whole visual claim.
    assert first.count("█") > second.count("█")
    # Every row is the same width, or the columns stop lining up.
    assert len({len(r.plain) for r in rows}) == 1


def test_a_ranked_share_is_of_the_positive_total_only():
    """Dividing by a SIGNED sum inflates every other share past 100%, because the
    denominator shrank by the loss. A part-to-whole has no negative slice, so such a row
    keeps its amount and shows none."""
    rows = charts.ranked_bars([("won", 100.0), ("lost", -50.0)], width=78)
    assert "100.0%" in rows[0].plain, rows[0].plain
    assert "—" in rows[1].plain and "-50.00" in rows[1].plain
    assert rows[1].plain.count("█") == 0, "a loss gets no part-to-whole bar"


def test_a_real_holding_is_never_drawn_as_nothing():
    """A share too small for one cell still gets one: a holding rounded away reads as a
    holding the book does not have."""
    rows = charts.ranked_bars([("huge", 100_000.0), ("tiny", 1.0)], width=78)
    assert rows[1].plain.count("█") == 1, rows[1].plain


def test_the_bar_yields_to_the_numbers_when_the_panel_is_narrow():
    """Truncating the LINE instead cut the amount column off, leaving a bar and a
    percentage -- the two things estimable by eye -- and dropping the one that is not.

    Returns no bar at all rather than a stub: two cells carry no information and steal
    columns from the figures.
    """
    wide = charts.ranked_bars([("a", 60.0), ("b", 40.0)], width=100)
    narrow = charts.ranked_bars([("a", 60.0), ("b", 40.0)], width=44)
    assert wide[0].plain.count("█") > 0
    assert narrow[0].plain.count("█") == 0, narrow[0].plain
    for rows in (wide, narrow):
        assert "60.00" in rows[0].plain and "60.0%" in rows[0].plain


def test_ranked_bars_of_nothing_is_nothing():
    assert charts.ranked_bars([], width=78) == []
    # All-zero: no shares to compute, but the amounts are still facts worth printing.
    rows = charts.ranked_bars([("a", 0.0)], width=78)
    assert "0.00" in rows[0].plain and rows[0].plain.count("█") == 0


async def test_the_charts_vanish_rather_than_draw_an_empty_frame(app, blank, goto):
    """A book with no allocations and no P&L has nothing to chart, and an empty axis is
    worse than the space it takes."""
    # The `blank` fixture keeps the fixture BETS, and `growth` carries an allocation -- so
    # without this the pie has a slice and the test asserted the opposite of the truth.
    blank.execute("UPDATE bets SET allocation_base=NULL, allocation_ccy=NULL, "
                  "allocation_set_on=NULL")
    async with app.run_test(size=(132, 52)) as pilot:
        await goto(pilot, "bets")
        await pilot.pause()
        drawn = "".join(_plain(app, "#bet-headline")).strip()
    assert drawn == "", drawn


# --------------------------------------------------------------------------- #
# air between sections
#
# The author: "you should have some breadthing air between sections. now everything is scrambled
# together." Between WIDGETS that is a CSS margin; inside a DataTable it has to be a row.
# --------------------------------------------------------------------------- #
def _rows(app) -> list[str]:
    """Every screen row as the compositor lays it out, so a GAP is visible.

    Reading a widget's own content cannot see this: the gaps that were missing are the
    boundaries BETWEEN widgets, which only the compositor puts together.
    """
    return ["".join(seg.text for seg in strip).rstrip()
            for strip in app.screen._compositor.render_strips()]


async def test_every_section_boundary_has_a_blank_line(app, con, goto):
    async with app.run_test(size=(126, 46)) as pilot:
        await pilot.pause()
        rows = _rows(app)
        # The pane title must not sit straight on the first figure.
        head = next(i for i, r in enumerate(rows) if "CAD" in r and "·" in r)
        assert rows[head + 1].strip() == "", rows[head:head + 3]
        # The capital ledger is a section, not a continuation of the sleeves.
        cap = next(i for i, r in enumerate(rows) if "flow(s)" in r)
        assert rows[cap - 1].strip() == "", rows[cap - 2:cap + 1]
        assert rows[cap + 1].strip() == "", rows[cap:cap + 3]

        await goto(pilot, "bets")
        await pilot.pause()
        rows = _rows(app)
        title = next(i for i, r in enumerate(rows) if "bet(s)" in r)
        assert rows[title + 1].strip() == ""
        # THE HEADLINE IS THE FINISHED TOTAL NOW -- the "allocation declared per bet" bars
        # were deleted once `of plan` printed the same shares in the table. When there IS one
        # it must not sit straight on the table's header row beneath it. Conditional because
        # the synthetic book has no finished bet, and asserting on a line the fixture never
        # draws is how a test comes to fail for a reason that has nothing to do with spacing.
        done = next((i for i, r in enumerate(rows) if "finished" in r), None)
        if done is not None:
            assert rows[done + 1].strip() == "", rows[done:done + 3]
        # And each named section below the table keeps a blank line above its heading.
        for name in ("HOLDINGS", "THESIS"):
            at = next(i for i, r in enumerate(rows) if r.strip() == name)
            assert rows[at - 1].strip() == "", (name, rows[at - 2:at + 1])


async def test_a_group_heading_never_sits_on_the_previous_groups_last_leg(app, con, goto):
    """A DataTable has no row spans and no way to space groups, so the gap is a ROW --
    keyed inert, so `x`, `enter` and `s` all refuse it like any other heading."""
    async with app.run_test(size=(126, 46)) as pilot:
        await goto(pilot, "positions")
        await pilot.pause()
        t = app.query_one("#pos-table", DataTable)
        keys = [t.coordinate_to_cell_key((r, 0)).row_key.value
                for r in range(t.row_count)]
        gaps = [k for k in keys if k.startswith(f"{PositionsPane.INERT}gap:")]
        assert gaps, "the groups run together"
        assert len(set(gaps)) == len(gaps), "every row key must be unique"
        # A gap precedes every heading except the very first.
        headings = [i for i, k in enumerate(keys)
                    if k.startswith(f"{PositionsPane.INERT}bet:")
                    or k.startswith(f"{PositionsPane.INERT}closed:")]
        for i in headings[1:]:
            assert keys[i - 1].startswith(f"{PositionsPane.INERT}gap:"), keys[i - 2:i + 1]
        assert not keys[0].startswith(f"{PositionsPane.INERT}gap:"), \
            "no blank row above the first group -- the title already provides that air"


async def test_a_blank_row_takes_no_key(app, con, goto):
    before = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    async with app.run_test(size=(126, 46)) as pilot:
        await goto(pilot, "positions")
        t = app.query_one("#pos-table", DataTable)
        gap = next(r for r in range(t.row_count)
                   if str(t.coordinate_to_cell_key((r, 0)).row_key.value
                          ).startswith(f"{PositionsPane.INERT}gap:"))
        t.cursor_coordinate = (gap, 0)
        await pilot.pause()
        for key in ("x", "enter", "s"):
            await pilot.press(key)
            await pilot.pause()
            assert not app.prompt.is_open and app._confirm is None, key
    assert con.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == before


async def test_the_bottom_is_still_reachable_now_the_pages_are_taller(app, con):
    """Air costs rows, and a figure you cannot scroll to is one the tool did not record as
    far as you know. 62x12 is the smallest size any test uses.

    100x20 IS THE SIZE THAT CAUGHT THE REAL BUG, and it is an ordinary terminal. Focusing
    the ledger table made Textual scroll it into view, so the dashboard opened pinned to the
    bottom with `net` off screen and no key that could reach it -- `up` went to the table,
    whose cursor was already at row 0.

    ONE run_test, ONE SIZE. The `app` fixture hands out a single DeskApp, and running it a
    second time inside one test times out waiting for the first run's messages to settle.
    62x12 is covered by test_the_dashboard_scrolls_and_still_changes_tabs.
    """
    async with app.run_test(size=(100, 20)) as pilot:
        await pilot.pause()
        sc = app.query_one("#dash-scroll", VerticalScroll)
        assert sc.max_scroll_y > 0, "this size should overflow, or this proves nothing"

        def on_screen(word):
            return any(word in "".join(seg.text for seg in strip)
                       for strip in app.screen._compositor.render_strips())

        assert on_screen("net "), "the headline is not on screen at open"
        # A FEW PRESSES TO PROVE THE KEY DRIVES IT, then scrolled the rest of the way
        # directly: twenty-odd pilot presses inside one run_test also exceed the settling
        # timeout, and fail for a reason unrelated to scrolling.
        for _ in range(3):
            await pilot.press("down")
        await pilot.pause()
        assert sc.scroll_y > 0, "down does not scroll the dashboard"
        sc.scroll_end(animate=False)
        await pilot.pause()
        assert sc.scroll_y >= sc.max_scroll_y - 0.5, (
            f"stuck at {sc.scroll_y} of {sc.max_scroll_y}")
        assert on_screen("deposit"), "the ledger is unreachable"
        # `tab` is how a row gets edited, since the container holds focus by default.
        await pilot.press("tab")
        await pilot.pause()
        # STILL THE CAPITAL TABLE, with a read-only portfolio table now sitting above it:
        # `x` and `enter` on this pane act on `#cap-table` by id, so a focusable second table
        # would let `x` delete a row the user never selected. #port-table sets
        # can_focus = False for exactly that reason, and this is the assertion that catches
        # it being made focusable again.
        assert getattr(app.focused, "id", None) == "cap-table"


# --------------------------------------------------------------------------- #
# sparklines, and reaching what is below a table
#
# The author: "i also want to add graphs to positions and to bets."
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("digit,scroll_id,word", [
    # positions lost its chart block with the sparklines, so bets is the only pane left
    # with anything below its table.
    ("3", "#bet-scroll", "allocation declared"),
])
async def test_down_reaches_what_is_below_the_table(app, digit, scroll_id, word):
    """THE BUG PaneTable EXISTS FOR, and it shipped twice before being named.

    DataTable binds up/down and its bindings consume the key whether or not the cursor moves.
    So a pane whose focus is a table could not reach anything below it: at 100x20 the bets
    page had six rows of P&L bars past the fold and no keystroke that would show them.
    """
    # 100x12, not 100x20: the SYNTHETIC book has three open legs and one line of chart, so a
    # taller terminal fits it all and the test would assert nothing. The real book overflows
    # 100x20 comfortably -- that is the size the bug was found at.
    async with app.run_test(size=(100, 12)) as pilot:
        await pilot.press(digit)
        await pilot.pause()
        sc = app.query_one(scroll_id, VerticalScroll)
        assert sc.max_scroll_y > 0, "this size should overflow, or this proves nothing"

        # THE SCROLL POSITION IS THE WHOLE PROPERTY. What is visible at the bottom depends
        # on how much history the fixture happens to hold -- the synthetic book marks one
        # date, so its chart block is the "no history yet" line -- and that is asserted by
        # the two tests that draw the charts. Here, without PaneTable handing the key back,
        # scroll_y simply never leaves the top.
        #
        # SET TO THE TOP, NOT ASSERTED TO BE THERE. This read `assert sc.scroll_y == 0` and
        # failed on CI's 3.13 runner while passing on its 3.11 runner in the same run, and
        # passing on 3.13 locally -- so it was nondeterminism, not a version difference:
        # giving the table focus scrolls it into view, and whether that has settled by now
        # depends on how fast the machine is. The starting position was never the property,
        # it was the setup for the property asserted below, and establishing it makes that
        # assertion STRONGER -- a pane that began at 1 because something auto-scrolled it
        # proves less than one that began at 0 because it was told to.
        sc.scroll_home(animate=False)
        await pilot.pause()
        assert sc.scroll_y == 0, "the pane did not go to the top when told to"
        for _ in range(30):
            await pilot.press("down")
        await pilot.pause()
        assert sc.scroll_y >= sc.max_scroll_y - 0.5, (
            f"{word}: stuck at {sc.scroll_y} of {sc.max_scroll_y} -- the table ate the key")


async def test_the_cursor_keeps_the_arrows_everywhere_but_the_edges(app, goto):
    """PaneTable hands the key back ONLY at the boundary. Anywhere else the cursor must move,
    or `x`, `enter` and `s` stop acting on a row the user chose."""
    async with app.run_test(size=(126, 46)) as pilot:
        await goto(pilot, "positions")
        t = app.query_one("#pos-table", DataTable)
        assert t.row_count > 3
        start = t.cursor_coordinate.row
        await pilot.press("down")
        await pilot.pause()
        assert t.cursor_coordinate.row == start + 1, "the cursor must still move"
        await pilot.press("up")
        await pilot.pause()
        assert t.cursor_coordinate.row == start


def test_every_message_handler_is_one_textual_will_actually_call():
    """A MISNAMED HANDLER IS AN UNUSED METHOD, and nothing anywhere complains.

    `BetsPane.on_row_highlighted` sat in this codebase never being called: Textual dispatches
    `DataTable.RowHighlighted` to `on_data_table_row_highlighted`, derived from the message
    class, so the shorter name was simply a method no one invoked. The bets note line
    therefore only refreshed after a WRITE, and moving the cursor left it captioning whichever
    bet had last been touched -- while a comment above it described the scoping logic that
    protected behaviour which was never happening.

    This is the fourth silent name collision here (`_closed` shadowed MessagePump's attribute,
    `_render` overrode Widget's, a local `share` shadowed the import) and the only one that was
    wrong from the moment it was written. The others at least raised or rendered blank; this
    one is invisible by construction, which is why it gets a test rather than a comment.

    Collected from the Textual classes the panes actually receive messages from, so it needs no
    hand-maintained list of message names.
    """
    import inspect

    from textual.containers import VerticalScroll
    from textual.message import Message
    from textual.widget import Widget
    from textual.widgets import DataTable, Input, Static

    from desk.tui import panes
    from desk.tui import prompt as prompt_mod

    legal = set()
    for cls in (DataTable, Input, Static, VerticalScroll, Widget,
                prompt_mod.InlinePrompt):
        for _, member in inspect.getmembers(cls):
            if inspect.isclass(member) and issubclass(member, Message):
                legal.add(member.handler_name)
    assert "on_data_table_row_highlighted" in legal, "the collector found no DataTable messages"

    offenders = []
    for _, cls in inspect.getmembers(panes, inspect.isclass):
        if cls.__module__ != panes.__name__:
            continue
        for name, fn in vars(cls).items():
            if not name.startswith("on_") or not callable(fn):
                continue
            # on_mount / on_key / on_show and friends are Widget lifecycle hooks, which the
            # collector above picks up from Widget itself where they are messages, and which
            # are legitimate overrides where they are not.
            if name in ("on_mount", "on_key", "on_show", "on_hide", "on_resize",
                        "on_unmount", "on_focus", "on_blur", "on_click"):
                continue
            if name not in legal:
                offenders.append(f"{cls.__name__}.{name}")
    assert not offenders, (
        "Textual will never call these — check Message.handler_name for the message you "
        f"mean: {offenders}")


# --------------------------------------------------------------------------- #
# editing a bet: its thesis, and its name
#
# The author: "i want to be able to edit bets" -> the note/thesis text and renaming the id.
# (Allocated capital was on their list too and already has a key, `a`, with ~why
# re-baselining.)
# --------------------------------------------------------------------------- #
def test_a_bet_is_named_in_words_and_keyed_by_a_derived_slug(con):
    """The author: "i dont want to write bets big-techs-and-semis. i just want to write Big Techs
    and Semis."

    daylogs' rule, applied: whatever is not sigiled is FREE TEXT. The slug exists only
    because `!bet` must survive tokenising as one whitespace-free word, which is a
    constraint of the grammar and no business of the person naming a thing.
    """
    r = parse.parse_bet("Big Techs and Semis =5000", now=NOW)
    assert r.name == "Big Techs and Semis"
    assert r.bet_id == "big-techs-and-semis"
    assert r.allocation == 5000.0

    book.add_bet(con, bet_id=r.bet_id, name=r.name, allocation=r.allocation,
                 on_date=r.on_date)
    b = next(x for x in book.bets(con) if x.id == "big-techs-and-semis")
    assert b.name == "Big Techs and Semis"


def test_a_bet_written_before_names_existed_reads_as_its_slug(con):
    """`name` is nullable, so every bet in their book predates it. COALESCE is what keeps the
    table from rendering a column of blanks on the first run after the migration."""
    con.execute("UPDATE bets SET name=NULL WHERE id='growth'")
    con.commit()
    assert next(x for x in book.bets(con) if x.id == "growth").name == "growth"


def test_renaming_edits_the_name_and_leaves_the_id_alone(con):
    """THE WHOLE POINT OF SPLITTING THEM. Renaming used to mean re-pointing nine foreign-key
    columns under `PRAGMA defer_foreign_keys`, and could not be undone with `u`. Now it is
    one UPDATE of free text, and every trade under the bet is untouched because the key it
    references never moved.
    """
    legs = [r[0] for r in con.execute(
        "SELECT id FROM trades WHERE bet_id='growth'")]
    assert legs, "fixture needs legs under growth"

    pre = book.set_bet_name(con, "growth", name="Growth, Renamed")
    b = next(x for x in book.bets(con) if x.id == "growth")
    assert b.name == "Growth, Renamed" and b.id == "growth"
    assert [r[0] for r in con.execute(
        "SELECT id FROM trades WHERE bet_id='growth'")] == legs

    book.restore_update(con, "bets", pre)
    # The fixture's bets predate `name`, so the pre-image holds NULL and Bet.name falls back
    # to the id -- which is the behaviour, not a wrinkle in the test.
    assert next(x for x in book.bets(con) if x.id == "growth").name == (
        pre["name"] or "growth")

    with pytest.raises(book.DeskError):
        book.set_bet_name(con, "growth", name="   ")
    with pytest.raises(book.DeskError):
        book.set_bet_name(con, "no-such-bet", name="x")


def test_the_nine_table_rename_is_gone(con):
    """Deleted the day after it was written, and that is the right outcome.

    Once the name is free text and the slug is derived once, nothing wants the id to change
    -- so an operation no key reaches is furniture, the same as `tp` and the instrument key.
    This test exists so it is not quietly reintroduced without a caller.
    """
    assert not hasattr(book, "rename_bet")
    assert not hasattr(book, "bets_referrers")
    assert not hasattr(parse, "parse_bet_id")


def test_a_bet_can_go_live_with_nothing_written(con):
    """The author: "i cant put semis-and-big-techs to live bc of this. i think we are still
    enforcing rules which shouldnt be at this stage."

    THREE WALLS GUARDED ONE ACT and all three are gone in v13: a Python gate naming what was
    missing, two triggers demanding a main-support falsifier, and a table CHECK requiring
    thesis and main_support. Removing the first two was not enough -- a test is what found
    the CHECK still standing.

    Nothing in the desk can write a thesis, a main_support or a falsifier, so every one of
    them demanded a field the tool could not fill. A status is an observation; a bet they are
    running is live whether the prose has caught up or not.
    """
    # A BET THAT NEVER HAD ANY OF IT, rather than one stripped of it: removing the last
    # main-support falsifier from a bet that is ALREADY live is a different rule
    # (`falsifier_delete_would_unmoor_a_live_bet`), it did not block the author, and it is not in
    # scope here.
    r = parse.parse_bet("Semis and Big Techs", now=NOW)
    book.add_bet(con, bet_id=r.bet_id, name=r.name, on_date=r.on_date)
    assert not con.execute("SELECT 1 FROM falsifiers WHERE bet_id=?",
                           (r.bet_id,)).fetchone()

    book.set_bet_status(con, r.bet_id, status="live", on_date="2026-09-24")
    row = con.execute("SELECT status, thesis, main_support FROM bets WHERE id=?",
                      (r.bet_id,)).fetchone()
    assert row["status"] == "live"
    assert row["thesis"] is None and row["main_support"] is None


def test_setting_a_bets_note_keeps_the_previous_one_for_undo(con):
    """A thesis is the most expensive text in the book, so it is replaced through a
    pre-image like every other write."""
    pre = book.set_bet_note(con, "growth", note="a new thesis")
    assert con.execute("SELECT notes FROM bets WHERE id='growth'"
                       ).fetchone()["notes"] == "a new thesis"
    book.restore_update(con, "bets", pre)
    assert con.execute("SELECT notes FROM bets WHERE id='growth'"
                       ).fetchone()["notes"] == pre["notes"]


async def test_an_editor_that_changes_nothing_writes_nothing(app, con, goto):
    """QUITTING WITHOUT SAVING MUST BE A NO-OP, including the undo stack.

    edit_text returns None for unchanged as well as for abandoned, precisely so this path
    cannot stack an entry -- a `u` afterwards would otherwise revert whatever came before,
    which is the worst kind of undo: one that works on the wrong thing.
    """
    before = con.execute("SELECT notes FROM bets WHERE id='growth'").fetchone()["notes"]
    async with app.run_test(size=(126, 46)) as pilot:
        await goto(pilot, "bets")
        app.edit_text = lambda initial, **kw: None       # the editor quit unchanged
        depth = len(app.undo)
        app.query_one("#bet-table", DataTable).move_cursor(row=0)
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert len(app.undo) == depth, "an unchanged edit stacked an undo entry"
    assert con.execute("SELECT notes FROM bets WHERE id='growth'"
                       ).fetchone()["notes"] == before


async def test_e_writes_what_the_editor_returned(app, con, goto):
    """And the whole text, newlines included -- which is the reason for the editor."""
    written = "line one\n\nline two, a paragraph later"
    async with app.run_test(size=(126, 46)) as pilot:
        await goto(pilot, "bets")
        app.edit_text = lambda initial, **kw: written
        t = app.query_one("#bet-table", DataTable)
        t.move_cursor(row=0)
        await pilot.pause()
        bet = t.coordinate_to_cell_key((0, 0)).row_key.value
        await pilot.press("e")
        await pilot.pause()
    assert con.execute("SELECT notes FROM bets WHERE id=?",
                       (bet,)).fetchone()["notes"] == written


def test_no_editor_configured_still_opens_an_editor(app, monkeypatch):
    """The author: "why do i need to export EDITOR?" They do not.

    This REFUSED when $EDITOR was unset and told them to export one, which made `e` a dead key
    on arrival -- their shell sets neither $EDITOR nor $VISUAL, and nothing in their .zshrc does.
    A tool that works only after you configure your shell for it has an unannounced setup
    step, and this project has already shipped three dead keys without adding a fourth on
    purpose.

    `vi` is the fallback because POSIX requires it, so the fallback cannot itself be missing.
    """

    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    seen = {}
    monkeypatch.setattr("desk.tui.app.subprocess.call",
                        lambda argv: seen.setdefault("argv", argv))
    monkeypatch.setattr(type(app), "suspend", lambda self: contextlib.nullcontext())

    app.edit_text("unchanged")
    assert seen["argv"][0] == "vi", seen
    assert shutil.which(seen["argv"][0]), f"{seen['argv'][0]} is not on PATH"


def test_the_configured_editor_wins_over_the_fallback(app, monkeypatch):
    """$VISUAL over $EDITOR over `vi`, which is the usual convention -- VISUAL names the
    full-screen editor, and prose is what this is for."""
    import contextlib as _ctx

    monkeypatch.setattr(type(app), "suspend", lambda self: _ctx.nullcontext())
    calls = []
    monkeypatch.setattr("desk.tui.app.subprocess.call", lambda argv: calls.append(argv))

    monkeypatch.setenv("EDITOR", "nano")
    monkeypatch.delenv("VISUAL", raising=False)
    app.edit_text("x")
    assert calls[-1][0] == "nano"

    monkeypatch.setenv("VISUAL", "vim")
    app.edit_text("x")
    assert calls[-1][0] == "vim"

    # AND AN EDITOR WITH ARGUMENTS SURVIVES, e.g. `code --wait`, which is one string in the
    # environment and must become two in argv.
    monkeypatch.setenv("VISUAL", "code --wait")
    app.edit_text("x")
    assert calls[-1][:2] == ["code", "--wait"]


# --------------------------------------------------------------------------- #
# the price poll must not lock the book
#
# The author, 2026-09-25, recording a fill: "it tells me database is locked".
# `OperationalError: database is locked` on `GOOG 4 339.68 @2026-09-25 /tfsa !semis-and-
# big-techs`. The fill had in fact landed; the error was still real, and the cause was that
# the 60-second price poll held a write lock across its entire fetch run.
# --------------------------------------------------------------------------- #
def test_the_price_fetch_never_holds_a_write_lock_across_the_network(con, monkeypatch,
                                                                    tmp_path):
    """THE ACTUAL BUG, reproduced: a write DURING a fetch used to fail.

    `prices.main()` opened an implicit transaction on its first write -- `UPDATE session SET
    actor` -- and committed after the whole loop, so the lock was held across every HTTP
    request, retry and pause. Fourteen instruments at up to three 30-second retries each is
    minutes, and the desk runs this every 60 seconds on a timer.

    The fetch here is stubbed to WRITE FIRST and then block, which is the shape that broke:
    while it is inside the network call, this test writes a fill on another connection. On the
    old code that raised; on the fixed code the poll holds no lock while fetching, so it
    succeeds.
    """
    import sqlite3
    import threading

    from desk import prices

    # A SEPARATE FILE from the fixture's own, which also lives under tmp_path -- copying onto
    # it raised SameFileError. prices.main reads a module-level DB path, so it needs a real
    # file rather than the open connection.
    db = tmp_path / "poll.db"
    shutil.copy(str(con.execute("PRAGMA database_list").fetchone()[2]), db)

    in_fetch = threading.Event()
    may_finish = threading.Event()

    def slow_fetch(symbol, since_epoch):
        in_fetch.set()
        # Stands in for an HTTP request. On the old code the caller already held a write
        # lock by this point, and it held it here.
        may_finish.wait(timeout=10)
        return [{"date": "2026-09-09", "close": 1.0, "currency": "CAD"}]

    monkeypatch.setattr(prices, "DB", str(db))
    monkeypatch.setattr(prices, "fetch_one", slow_fetch)
    monkeypatch.setattr(prices, "PAUSE_S", 0)

    result = {}
    poll = threading.Thread(target=lambda: result.setdefault(
        "rc", prices.main(dry=False, days=7)), daemon=True)
    poll.start()
    assert in_fetch.wait(timeout=10), "the stubbed fetch never ran"

    # THE WRITE THAT USED TO FAIL. A separate connection, exactly as the desk's is, while the
    # poll sits in its "network call".
    writer = sqlite3.connect(db, isolation_level=None)
    writer.execute("PRAGMA busy_timeout = 2000")
    try:
        writer.execute("UPDATE session SET actor = 'a fill being recorded'")
    except sqlite3.OperationalError as exc:      # pragma: no cover - the bug
        may_finish.set()
        poll.join(timeout=10)
        raise AssertionError(
            f"the poll locked the book while fetching: {exc}") from exc
    finally:
        writer.close()
        may_finish.set()
        poll.join(timeout=10)


def test_the_book_waits_for_another_writer_instead_of_refusing(con):
    """Python's default is 5 seconds and that was not enough.

    The desk holds its own locks for milliseconds, so a long wait can only mean another
    process -- and failing the user's fill because something else was mid-write is the one
    outcome this tool must not produce. Belt to prices.py's braces.
    """
    # THROUGH book.connect(), which is what the app uses. The `con` fixture builds its own
    # connection straight from the schema, so it never sees these pragmas -- asserting on it
    # measured Python's 5-second default and said nothing about the desk.
    copy = pathlib.Path(str(con.execute("PRAGMA database_list").fetchone()[2]))
    opened = book.connect(str(copy))
    try:
        got = opened.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        opened.close()
    assert got >= 10_000, f"busy_timeout is {got}ms, so a concurrent writer fails the fill"


# --------------------------------------------------------------------------- #
# the dashboard as a portfolio
#
# The author, 2026-09-24: "the dashboard is essentially the portfolio tab. we should see live bets,
# their share of the portfolio, each trades within each bet. i need this knowledge to know
# what more to add to each trade."
# --------------------------------------------------------------------------- #
def test_the_portfolio_groups_every_leg_under_its_bet_largest_first(con):
    """Including the legs under NO bet, which is a group and not a gap.

    On the author's book that group is 58% of net -- money-market parking -- so a view that listed
    only bets would omit the largest thing in the portfolio.
    """
    groups = book.portfolio(con)
    assert groups, "no positions to group"
    seen = sum(len(g.legs) for g in groups)
    assert seen == len(book.positions(con)), "a leg went missing between the two views"
    # Largest first, with unvaluable groups last rather than sorted as zero.
    valued = [g.value for g in groups if g.value is not None]
    assert valued == sorted(valued, reverse=True)
    assert all(g.value is not None for g in groups[:len(valued)])


def test_filled_means_one_thing_on_every_screen(con):
    """THE BETS TAB PRINTED 60.1% AND 62.0% FOR THE SAME BET, three rows apart.

    The detail line divided market value by the allocation; the HOLDINGS header divided entry
    cost by it. Both were labelled "filled" and both were true, and nothing on screen let a
    reader work out why they differed -- which is worse than either being wrong, because the
    tool looked like it disagreed with itself about a number it had just computed.

    The author chose cost: "i think the standard practice for this is filled. bc that's what
    important for the portfolio decision right? so entry cost/allocation."

    Three independent code paths compute it -- Bet.filled_pct, PortfolioGroup.filled_pct and
    the bars in BetComposition -- so this asserts they AGREE rather than asserting any one of
    them, which is the property that was missing rather than the arithmetic.
    """
    groups = {g.bet_id: g for g in book.portfolio(con)}
    checked = 0
    for b in book.bets(con):
        if b.filled_pct is None:
            continue
        checked += 1
        c = book.bet_composition(con, b.id)
        if c.allocation and not c.unpriced:
            assert 100.0 * c.spent / c.allocation == pytest.approx(b.filled_pct), (
                f"{b.id}: the HOLDINGS bars and the detail line disagree")
        g = groups.get(b.id)
        if g is not None and g.filled_pct is not None:
            assert g.filled_pct == pytest.approx(b.filled_pct), (
                f"{b.id}: the dashboard and the bets tab disagree")
            # AND THE LEG SHARES SUM TO IT. That is the whole reason the allocation is the
            # denominator: the bars sum to `filled` and the gap is the (dry) row.
            shares = [g.of_bet(leg) for leg in g.legs]
            if all(x is not None for x in shares):
                assert sum(shares) == pytest.approx(g.filled_pct), (
                    f"{b.id}: the per-leg shares do not sum to filled")
    if not checked:
        pytest.skip("no bet has both an allocation and a cost")


def test_a_portfolio_weight_is_a_share_of_net_not_of_deployed(con):
    """THE DENOMINATOR IS THE DECISION. 58% of the author's net is parked, so share-of-deployed
    would show their equity bets at roughly four times their real weight and hide the single
    biggest fact about the portfolio.
    """
    n = book.net(con)
    for g in book.portfolio(con):
        if g.value is None:
            assert g.weight_pct is None
            continue
        assert g.weight_pct == pytest.approx(100.0 * g.value / n.net)
    # And the groups plus cash account for the WHOLE of net -- that is what makes it a
    # portfolio view rather than a list of holdings.
    total = sum(g.value for g in book.portfolio(con) if g.value is not None)
    assert total + n.cash == pytest.approx(n.net, rel=1e-6)


def test_a_group_with_an_unvaluable_leg_reports_no_total(con):
    """Summing the legs that CAN be valued and printing it as the group's worth is the
    `or 0.0` mistake -- confidently short by whatever it skipped."""
    tid = "2026-05-04-park"
    con.execute("DELETE FROM prices WHERE kind='mark' AND base="
                "(SELECT ticker FROM trades WHERE id=?)", (tid,))
    con.commit()
    grp = next(g for g in book.portfolio(con)
               if any(x.trade_id == tid for x in g.legs))
    assert grp.value is None and grp.weight_pct is None
    assert grp.unvalued >= 1


async def test_the_dashboard_shows_bets_their_share_and_their_legs(app, con, goto):
    """A TABLE, not indented text. The author: "cant we have a table or something? some charts? this
    looks ugly as hell."

    Both weights on every row, because they named two and they answer different questions: what
    a bet has RESERVED of the portfolio, and how much of that reservation is spent.
    """
    async with app.run_test(size=(126, 60)) as pilot:
        await goto(pilot, "dashboard")
        t = app.query_one("#port-table", DataTable)
        labels = [str(getattr(c.label, "plain", c.label)) for c in t.columns.values()]
        rows = {t.coordinate_to_cell_key((r, 0)).row_key.value: t.get_row_at(r)
                for r in range(t.row_count)}
        head = " ".join(_plain(app, "#port-head"))

    assert labels[:4] == ["holding", "value", "of net", "of bet"], labels
    for g in book.portfolio(con):
        row = rows[f"·grp:{g.bet_id or 'none'}"]
        assert _cell_text(row[0]) == g.name
        # A BET ROW CARRIES ITS CLAIM; `no bet` has no allocation so it falls back to its
        # market share, which is the only honest share it has.
        want = g.claim_pct if g.claim_pct is not None else g.weight_pct
        assert _cell_text(row[2]) == share(want), (g.name, _cell_text(row[2]))
        for leg in g.legs:
            assert leg.trade_id in rows, f"{leg.ticker} is not under {g.name}"
            got = _cell_text(rows[leg.trade_id][3])
            assert got == (share(g.of_bet(leg)) if g.of_bet(leg) is not None
                           else NA_DASH), (leg.ticker, got)
    # CASH CLOSES THE ALLOCATION, and the bar rule is stated once rather than per row.
    assert "·cash" in rows
    assert "share of its parent" in head, head


async def test_the_portfolio_bars_scale_with_the_share(app, goto):
    """`cell_bar` resolves to an EIGHTH of a column, because these shares are small.

    At 12 columns for 100%, whole-cell rounding puts a 1.4% holding and a 6.0% one at nothing
    and one cell -- the same rounding ranked_bars had to work around by bumping anything real
    up to a full cell. Partial blocks give about ninety steps in twelve columns.
    """
    from desk.tui import charts

    assert charts.cell_bar(None, 12) == "", "an unrecorded share must not draw as zero"
    assert charts.cell_bar(0.0, 12).strip() == ""
    small, big = charts.cell_bar(1.4, 12), charts.cell_bar(6.0, 12)
    assert small.strip() and big.strip(), (repr(small), repr(big))
    assert len(small.strip()) <= len(big.strip())
    assert small != big, "1.4% and 6.0% drew the same bar"
    assert len(charts.cell_bar(100.0, 12)) == 12
    assert charts.cell_bar(140.0, 12) == charts.cell_bar(100.0, 12), "must clamp"


# --------------------------------------------------------------------------- #
# what a bet is made of
#
# The author: "i want to view ticker composition within a bet (like it takes up xx%)", and asked
# for it as a share of the DECLARED ALLOCATION rather than of deployed capital -- so the
# bars sum to the bet's filled percentage and the gap is a `(dry)` row.
# --------------------------------------------------------------------------- #
def test_composition_shares_the_declared_allocation_and_names_the_unspent_part(con):
    """The denominator is the allocation, which is what makes `(dry)` meaningful.

    Share-of-the-spend always sums to 100% and so can never say "most of this bet is still
    cash" -- which for a bet that is 5% filled is the most important thing about it.
    """
    c = book.bet_composition(con, "growth")
    assert c.allocation is not None, "fixture changed: `growth` needs an allocation"
    assert c.legs, "no legs to compose"
    # Descending, because rank is one of the things a bar carries.
    assert list(c.legs) == sorted(c.legs, key=lambda kv: kv[1], reverse=True)
    # dry + spent IS the allocation, exactly -- that identity is the whole chart, and it
    # only holds at cost, which is why `filled` is measured there.
    assert c.spent + c.dry == pytest.approx(c.allocation)


def test_composition_aggregates_a_ticker_held_in_two_accounts(con):
    """The question is about TICKERS, so one instrument in two sleeves is one row.

    The author holds AEM.TO in both rrsp and tfsa under one bet. Two rows for it would make the
    chart answer "what legs is this bet made of", which the table above already does.
    """
    book.add_fill(con, ticker="CADCO", shares=3, price=10.0, bet_id="growth",
                  on_date="2026-06-01", account="rrsp")
    book.add_fill(con, ticker="CADCO", shares=4, price=10.0, bet_id="growth",
                  on_date="2026-06-01", account="tfsa")
    legs = [p for p in book.positions(con)
            if p.bet_id == "growth" and p.ticker == "CADCO"]
    assert len({p.account for p in legs}) == 2, "needs the same ticker in two accounts"

    c = book.bet_composition(con, "growth")
    tickers = [t for t, _ in c.legs]
    assert tickers.count("CADCO") == 1, f"CADCO appears {tickers.count('CADCO')} times"
    combined = dict(c.legs)["CADCO"]
    assert combined == pytest.approx(sum(p.cost_base for p in legs))


def test_a_leg_with_no_cost_is_named_rather_than_counted_as_zero(con):
    """Zeroing it would shrink every other share while the bars still summed to 100%.

    The same `or 0.0` hazard that once advertised +11,760.18 on a book that had made
    +255.58, in a chart instead of a header.
    """
    tid = "2026-05-04-usdco"
    con.execute("UPDATE trades SET bet_id='growth' WHERE id=?", (tid,))
    con.execute("UPDATE fills SET price=NULL WHERE trade_id=?", (tid,))
    con.commit()
    c = book.bet_composition(con, "growth")
    assert "USDCO" in c.unpriced, c.unpriced
    assert "USDCO" not in dict(c.legs), "a leg with no cost was given a share anyway"


def test_a_bet_with_no_allocation_has_no_dry_figure_at_all(con):
    """None, not zero: there is no denominator, so there is no unspent remainder.

    The pane says which denominator is missing instead of quietly switching to the spend and
    labelling the result as if it answered the question that was asked.
    """
    con.execute("UPDATE bets SET allocation_base=NULL, allocation_ccy=NULL, "
                "allocation_set_on=NULL WHERE id='growth'")
    con.commit()
    c = book.bet_composition(con, "growth")
    assert c.allocation is None and c.dry is None
    assert c.legs, "the legs are still worth breaking down without a budget"


async def test_the_composition_block_follows_the_cursor(app, con, goto):
    """It captions the bet under the cursor, so it has to move when the cursor does.

    A rebuild does not fire RowHighlighted for the row it lands back on, which is why the
    note line already redraws explicitly -- the bars hang off the same two places.
    """
    async with app.run_test(size=(126, 46)) as pilot:
        await goto(pilot, "bets")
        t = app.query_one("#bet-table", DataTable)
        assert t.row_count >= 2, "need two bets to move between"
        # THE DETAIL follows the cursor. The headline above the table deliberately does
        # NOT -- it describes every bet, so a cursor move must leave it alone.
        head_before = " ".join(_plain(app, "#bet-headline"))
        first = " ".join(_plain(app, "#bet-detail"))
        assert t.coordinate_to_cell_key((0, 0)).row_key.value in first, first
        # THE CURSOR IS MOVED DIRECTLY, not with a keypress. `down` depends on which widget
        # holds focus, and this test is about what the bars do once the highlight moves --
        # keyboard reachability has its own tests.
        t.move_cursor(row=1)
        await pilot.pause()
        second = " ".join(_plain(app, "#bet-detail"))
        assert " ".join(_plain(app, "#bet-headline")) == head_before, (
            "the page-level headline moved with the cursor")
    assert second != first, "the detail stayed on the previous bet"
    assert t.coordinate_to_cell_key((1, 0)).row_key.value in second, second


# --------------------------------------------------------------------------- #
# moving a leg between bets, and the phantom that used to leave behind
#
# The author: "i want to be able to edit a trade, delete a position. currently i moved the GOOG
# to 2026 q4 reverse bet, and it left 0 quantity in no bets."
#
# They had done nothing wrong. `enter` edits a fill by delete-then-add, add_fill matches on
# (bet, ticker, account), so changing `!bet` moved the fill to a NEW trade and left the old
# one open with no fills -- which check.py calls a FAILURE twice over, and which `x` could
# not clean up ("has no fills to delete"). Their live book was failing its own checker.
# --------------------------------------------------------------------------- #
def test_deleting_the_last_fill_takes_the_empty_position_with_it(con):
    """An open trade with no fills is a position made of nothing.

    Both invariants in check.py say so -- "an open trade with no fills" and "an open trade
    holding nothing" -- so leaving one behind was writing state the project's own checker
    rejects.
    """
    tid = "2026-05-04-park"
    fill = con.execute("SELECT id FROM fills WHERE trade_id=?", (tid,)).fetchone()["id"]
    assert con.execute("SELECT COUNT(*) FROM fills WHERE trade_id=?",
                       (tid,)).fetchone()[0] == 1, "fixture changed: needs a 1-fill leg"

    pre = book.delete_fill(con, fill)
    assert con.execute("SELECT 1 FROM trades WHERE id=?", (tid,)).fetchone() is None, (
        "the emptied position survived its last fill")
    assert not [p for p in book.positions(con) if p.trade_id == tid]

    # ONE `u` PUTS BACK BOTH. The fill cannot be re-inserted without its trade --
    # fills.trade_id is NOT NULL and a foreign key -- so a pre-image that carried only the
    # fill would make undo raise, which is undo doing nothing.
    book.restore(con, "fills", pre)
    assert con.execute("SELECT 1 FROM trades WHERE id=?", (tid,)).fetchone() is not None
    assert con.execute("SELECT COUNT(*) FROM fills WHERE trade_id=?",
                       (tid,)).fetchone()[0] == 1
    back = next(p for p in book.positions(con) if p.trade_id == tid)
    assert back.qty > 0 and back.bet_id == "parking"


def test_deleting_one_fill_of_several_leaves_the_position_alone(con):
    """The guard is "no fills LEFT", not "a fill was deleted"."""
    tid = "2026-05-04-usdco"
    assert con.execute("SELECT COUNT(*) FROM fills WHERE trade_id=?",
                       (tid,)).fetchone()[0] == 2, "fixture changed: needs a 2-fill leg"
    newest = con.execute("SELECT id FROM fills WHERE trade_id=? "
                         "ORDER BY on_date DESC, id DESC LIMIT 1", (tid,)).fetchone()["id"]
    pre = book.delete_fill(con, newest)
    assert con.execute("SELECT 1 FROM trades WHERE id=?", (tid,)).fetchone() is not None
    assert "__trade__" not in pre, "carried a trade pre-image it never deleted"


def test_a_closed_position_is_not_deleted_when_its_last_fill_is(con):
    """History must not evaporate because a figure in it was corrected.

    `status='open'` is in the guard for this reason: a closed leg's fills are the record of
    a trade that happened, and deleting the newest to retype it must not take the trade.
    A `planned` trade is excluded for the mirror-image reason -- having no fills is what
    planned MEANS, so emptiness there is not a defect to clean up.
    """
    tid = "2026-03-02-cadco"
    assert con.execute("SELECT status FROM trades WHERE id=?",
                       (tid,)).fetchone()["status"] == "closed"
    for f in con.execute("SELECT id FROM fills WHERE trade_id=?", (tid,)).fetchall():
        book.delete_fill(con, f["id"])
    assert con.execute("SELECT 1 FROM trades WHERE id=?", (tid,)).fetchone() is not None, (
        "a closed trade was deleted along with its fills")


def test_moving_a_leg_between_bets_moves_every_fill_with_it(con):
    """THE POINT OF set_trade_bet, and what editing a fill could never do.

    A leg of two fills, re-pointed. Editing `!bet` on the newest would have moved that one
    fill to a new trade and stranded the older one under the old bet -- one position
    silently becoming two, which is the shape of the author's GOOG problem on a bigger leg.
    """
    tid = "2026-05-04-usdco"
    before = [r["id"] for r in con.execute("SELECT id FROM fills WHERE trade_id=?", (tid,))]
    assert len(before) == 2

    pre = book.set_trade_bet(con, tid, bet_id="parking")
    assert pre["bet_id"] == "growth"
    assert con.execute("SELECT bet_id FROM trades WHERE id=?",
                       (tid,)).fetchone()["bet_id"] == "parking"
    after = [r["id"] for r in con.execute("SELECT id FROM fills WHERE trade_id=?", (tid,))]
    assert after == before, "the fills were rewritten instead of the leg being re-pointed"
    assert not con.execute("SELECT 1 FROM trades t WHERE t.status='open' AND NOT EXISTS "
                           "(SELECT 1 FROM fills f WHERE f.trade_id=t.id)").fetchone(), (
        "moving a leg left an empty position behind")

    # UNDOABLE THROUGH THE UPDATE PATH, because the row still exists -- pushing this
    # pre-image through the insert path would fail on the primary key.
    book.restore_update(con, "trades", pre)
    assert con.execute("SELECT bet_id FROM trades WHERE id=?",
                       (tid,)).fetchone()["bet_id"] == "growth"


def test_a_leg_can_be_detached_from_every_bet(con):
    """"no bet" is a destination, not a refusal.

    The author parks principal in money-market funds: "this has nothing to do with any bet." So
    the only honest way to say it -- an empty prompt -- has to mean something.
    """
    tid = "2026-05-04-park"
    book.set_trade_bet(con, tid, bet_id=None)
    assert con.execute("SELECT bet_id FROM trades WHERE id=?",
                       (tid,)).fetchone()["bet_id"] is None
    assert next(p for p in book.positions(con) if p.trade_id == tid).bet_id is None
    assert parse.parse_trade_bet("") is None


def test_moving_a_leg_nowhere_or_to_a_bet_that_does_not_exist_is_refused(con):
    """A typo'd bet id must not silently create one, and a no-op must not read as a move.

    The no-op guard is not pedantry: the prompt is PREFILLED with the leg's current bet, so
    pressing `b` and enter without editing is the single most likely keystroke sequence, and
    it has to say "nothing to do" rather than report a move that did not happen.
    """
    with pytest.raises(book.DeskError) as e:
        book.set_trade_bet(con, "2026-05-04-park", bet_id="no-such-bet")
    assert "no bet no-such-bet" in str(e.value)

    with pytest.raises(book.DeskError) as e:
        book.set_trade_bet(con, "2026-05-04-park", bet_id="parking")
    assert "already under parking" in str(e.value)

    with pytest.raises(book.DeskError):
        book.set_trade_bet(con, "no-such-trade", bet_id="growth")


async def test_b_moves_the_leg_under_the_cursor_and_leaves_no_phantom(app, con, goto,
                                                                     type_into):
    """End to end, the keystroke the author needed.

    Asserts the ABSENCE of the phantom as well as the move, because the move alone was
    never the hard part -- `enter` moved GOOG too, and left the wreckage.
    """
    tid = "2026-05-04-park"
    async with app.run_test(size=(126, 46)) as pilot:
        await goto(pilot, "positions")
        t = await _point_at(pilot, app, "#pos-table", tid)
        assert t is not None
        await pilot.press("b")
        await pilot.pause()
        assert app.prompt.is_open and app.prompt.label == "leg bet", app.prompt.label
        # PREFILLED WITH WHERE IT IS NOW, so the prompt answers "where is this leg?"
        # before asking where it should go -- and as the NAME, because that is what tab
        # cycles. The fixture's bets predate `name`, so it falls back to the id.
        assert app.prompt.value == "parking", app.prompt.value
        app.prompt.value = ""
        await type_into(pilot, "growth")
        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, app.prompt.error

    assert con.execute("SELECT bet_id FROM trades WHERE id=?",
                       (tid,)).fetchone()["bet_id"] == "growth"
    assert not con.execute("SELECT 1 FROM trades t WHERE t.status='open' AND NOT EXISTS "
                           "(SELECT 1 FROM fills f WHERE f.trade_id=t.id)").fetchone()


# --------------------------------------------------------------------------- #
# which currency a row is in
#
# The author: "also, i think you need to note down which currencies these positions are in
# right?" -- looking at a row that read `1  815.5100  815.56  ...  1,126.04`, where the
# prices are USD and the value was CAD and nothing said so.
#
# THE TABLE SWITCHED FROM TRANSLATED TO NATIVE ROWS AND THE WHOLE SUITE STAYED GREEN,
# which is the reason this section exists: not one test read the value or P&L cell of a
# positions row. Every test here would have failed on the old behaviour.
# --------------------------------------------------------------------------- #
POS = {"leg": 0, "qty": 1, "ccy": 2, "entry": 3, "last": 4, "date": 5, "sl": 6,
       "value": 7, "pnl": 8, "pct": 9}


def _cell_text(cell) -> str:
    """A DataTable cell is a rich.Text here, and its `.plain` is the string.

    NOT NAMED `_plain`: this module already has a `_plain(app, selector)` that reads a
    Static's rendered text, and shadowing it made three tests die with "missing 1 required
    positional argument". Same family as the Textual method collisions -- a short name for
    a general idea gets taken.
    """
    return str(getattr(cell, "plain", cell))


async def test_every_open_leg_states_its_currency_and_its_row_adds_up(app, con, goto):
    """qty x last IS the value shown, on every row, in the currency `ccy` names.

    THE COMPLAINT IN ONE ASSERTION. Translated, a USD leg read `1 x 815.56 = 1,126.04`,
    and no amount of squinting gets you from one to the other -- the value was CAD, the
    prices USD. Native, the row is checkable by hand, which is the only kind of number a
    bookkeeping tool should print.
    """
    async with app.run_test(size=(126, 46)) as pilot:
        await goto(pilot, "positions")
        t = app.query_one("#pos-table", DataTable)
        rows = {t.coordinate_to_cell_key((r, 0)).row_key.value: t.get_row_at(r)
                for r in range(t.row_count)}

    legs = book.positions(con)
    assert legs, "fixture has no open legs, so this test proves nothing"
    for leg in legs:
        row = rows[leg.trade_id]
        assert _cell_text(row[POS["ccy"]]) == leg.currency, leg.ticker
        if leg.mark is None:
            continue
        shown = _cell_text(row[POS["value"]]).replace(",", "")
        assert shown == f"{leg.qty * leg.mark:,.2f}".replace(",", ""), (
            f"{leg.ticker}: {leg.qty:g} x {leg.mark} should be the value, got {shown}")

    # NOT VACUOUS. Every assertion above passes trivially on a single-currency book, and
    # this whole section exists because of a foreign leg, so the fixture must have one.
    base = book.base_currency(con)
    assert any(x.currency != base for x in legs), (
        "fixture holds only base-currency legs -- the translation this guards cannot occur")


async def test_a_foreign_leg_is_not_shown_in_the_home_currency(app, con, goto):
    """The specific regression, named so it cannot come back by accident.

    Pinned to the ARITHMETIC rather than to a literal, because the mark moves: what must
    never happen again is the value cell matching qty x mark x fx.
    """
    base = book.base_currency(con)
    foreign = [x for x in book.positions(con)
               if x.currency != base and x.mark is not None and x.fx not in (None, 1.0)]
    assert foreign, "no foreign leg with a rate -- nothing to regress"
    async with app.run_test(size=(126, 46)) as pilot:
        await goto(pilot, "positions")
        t = app.query_one("#pos-table", DataTable)
        rows = {t.coordinate_to_cell_key((r, 0)).row_key.value: t.get_row_at(r)
                for r in range(t.row_count)}
    for leg in foreign:
        shown = _cell_text(rows[leg.trade_id][POS["value"]])
        assert shown != f"{leg.value_base:,.2f}", (
            f"{leg.ticker} is being translated again: {shown}")
        assert shown == f"{leg.value_native:,.2f}", (leg.ticker, shown)


async def test_a_bet_heading_names_the_home_currency_because_it_translates(app, con,
                                                                          goto):
    """The one row here that MUST translate, and therefore the one that must say so.

    A bet's legs can sit in several currencies at once, so its subtotal has no honest
    choice but the home currency -- and an unlabelled subtotal directly above native rows
    is the same ambiguity one level up. `ccy` means the currency of the money on THIS row,
    with no exceptions, which is what lets a reader stop thinking about it.
    """
    async with app.run_test(size=(126, 46)) as pilot:
        await goto(pilot, "positions")
        t = app.query_one("#pos-table", DataTable)
        headings = {}
        for r in range(t.row_count):
            key = t.coordinate_to_cell_key((r, 0)).row_key.value or ""
            if key.startswith("·bet:"):
                headings[key] = t.get_row_at(r)

    base = book.base_currency(con)
    assert headings, "no bet heading rows"
    for key, row in headings.items():
        assert _cell_text(row[POS["ccy"]]) == base, key

    # And the figure really is the translated sum, not a native one that happens to be
    # close because most legs are already in the home currency.
    legs = [x for x in book.positions(con) if x.bet_id]
    if legs:
        by_bet = {}
        for x in legs:
            by_bet.setdefault(x.bet_id, []).append(x)
        for bet_id, group in by_bet.items():
            row = headings.get(f"·bet:{bet_id}")
            if row is None or any(x.value_base is None for x in group):
                continue
            want = f"{sum(x.value_base for x in group):,.2f}"
            assert _cell_text(row[POS["value"]]) == want, (bet_id, _cell_text(row[POS["value"]]))


def test_a_leg_with_no_recorded_rate_still_shows_what_it_is_worth_in_its_own_currency():
    """WITHHOLDING WHAT THE BOOK KNOWS is the same error as inventing what it does not.

    `_base` needs a rate and correctly returns None without one. But qty and the mark are
    both recorded in the leg's own currency, so its native value, P&L and percentage are
    all perfectly knowable -- and used to render as three em-dashes. That was the state
    for any foreign leg the FX table had not caught up with.
    """
    leg = book.Position(
        trade_id="t1", bet_id=None, ticker="NVDA", currency="USD", qty=10.0,
        entry=200.0, opened="2026-09-01", stop=None, target=None,
        loss_limit_pct=None,
        mark=220.0, mark_date="2026-09-09", fx=None, unconfirmed_fills=0,
        account="tfsa")
    assert leg.value_base is None and leg.pnl_base is None
    assert leg.value_native == 2200.0
    assert leg.cost_native == 2000.0
    assert leg.pnl_native == 200.0
    assert leg.pnl_pct_native == pytest.approx(10.0)


def test_the_percentage_does_not_depend_on_the_rate():
    """fx cancels in a ratio, so the native and translated percentages must agree.

    This is what makes the switch to native rows free in the `%` column: it was already
    the same number, computed the long way round.
    """
    kw = dict(trade_id="t1", bet_id=None, ticker="NVDA", currency="USD", qty=10.0,
              entry=200.0, opened="2026-09-01", stop=None, target=None,
              loss_limit_pct=None,
              mark=220.0, mark_date="2026-09-09", unconfirmed_fills=0, account="tfsa")
    for rate in (1.0, 1.3807, 0.72):
        leg = book.Position(fx=rate, **kw)
        assert leg.pnl_pct == pytest.approx(leg.pnl_pct_native), rate


async def test_capital_in_one_foreign_currency_is_not_reported_as_zero(app, con, goto):
    """A MISSING CURRENCY IS NOT A ZERO BALANCE, and the ledger header said it was.

    The header had a `len(by_ccy) > 1` branch whose other side read
    `money(by_ccy.get(base, 0.0))` and labelled it with the HOME currency. So a book whose
    flows are all in one NON-HOME currency fell through to it, failed to find the home
    currency in the dict, and printed "net in 0.00 CAD" while holding real money.

    Reachable, not theoretical: `c 15000 USD` parses to a USD flow. This is the same defect
    as `or 0.0` in a sum -- the bug that once advertised +11,760.18 on a book that had made
    +255.58 -- wearing a dict's clothes.
    """
    con.execute("DELETE FROM flows")
    con.execute("DELETE FROM adjustments")
    # THROUGH THE REAL WRITER, so the row is one the app could actually have produced --
    # hand-rolled SQL against a STRICT table with six CHECK constraints proves less and
    # breaks first.
    book.add_flow(con, amount=15000.0, on_date="2026-09-01", currency="USD",
                  account="tfsa", note="usd only")
    con.commit()

    async with app.run_test(size=(126, 46)) as pilot:
        await goto(pilot, "dashboard")
        head = " ".join(_plain(app, "#cap-head"))

    assert "15,000.00 USD" in head, head
    assert "0.00" not in head.replace("15,000.00", ""), (
        f"a currency the book does not hold was reported as a zero balance: {head}")


async def test_the_header_names_the_rate_that_reconciles_the_rows_to_the_totals(app, con,
                                                                                goto):
    """NATIVE ROWS CLOSE ONE RECONCILIATION GAP AND WOULD OPEN ANOTHER WITHOUT THIS.

    A bet subtotal is in the home currency; its legs are each in their own. So getting from
    `815.56 USD + 873.60 USD + 650.90 CAD` to the `2,982.75 CAD` printed directly above them
    needs a multiplier, and until now that multiplier lived on tab 1 only. Being unable to
    check a total against the rows beneath it is the complaint that started all of this, one
    level up -- so the rate belongs on this page.

    In the HEADER, not a column: it is one number per currency, not one per row, and the
    table has no cells to spare.
    """
    legs = book.positions(con)
    base = book.base_currency(con)
    want = {x.currency: x.fx for x in legs if x.currency != base and x.fx is not None}
    assert want, "fixture has no foreign leg with a rate -- nothing to reconcile"

    async with app.run_test(size=(126, 46)) as pilot:
        await goto(pilot, "positions")
        head = " ".join(_plain(app, "#pos-head"))

    for ccy, rate in want.items():
        assert f"{ccy} @ {rate:,.4f}" in head, (ccy, head)
    # NOT the home currency: `CAD @ 1.0000` in a CAD book is furniture.
    assert f"{base} @" not in head, head

    # AND THE TOTAL REALLY IS RECONSTRUCTABLE with it -- the point of printing it. Sum each
    # leg natively, translate with the advertised rate, and the header's own value must fall
    # out. This is the assertion that would have caught a header quoting a stale rate.
    rebuilt = sum(x.qty * x.mark * (1.0 if x.currency == base else want[x.currency])
                  for x in legs if x.mark is not None)
    assert f"{rebuilt:,.2f}" in head, (f"{rebuilt:,.2f}", head)


async def test_a_leg_with_no_rate_gets_no_invented_rate_in_the_header(app, con, goto):
    """A rate the pane does not have must not be printed as 1.0000.

    That is the parity lie NULL exists to prevent -- a USD leg valued 1:1 in a CAD book is
    wrong by the whole FX factor, about 38% here, and looks like an ordinary number. The
    `totals exclude —` clause is what reports the hole instead.
    """
    # THE FX ROWS LIVE IN `prices` under kind='fx' (book.fx_rate, book.py:197-219), keyed by
    # base/quote rather than a ticker -- and the inverse pair counts too, so both directions
    # have to go or the lookup simply flips and succeeds.
    con.execute("DELETE FROM prices WHERE kind='fx'")
    con.commit()

    async with app.run_test(size=(126, 46)) as pilot:
        await goto(pilot, "positions")
        head = " ".join(_plain(app, "#pos-head"))

    unrated = [x for x in book.positions(con) if x.fx is None]
    if not unrated:
        pytest.skip("the fixture still resolves every rate, so there is no hole to report")
    assert "@ 1.0000" not in head, head
    assert "no FX rate" in head, head


async def test_the_positions_columns_fit_the_terminal(app, goto):
    """THE BUDGET, ASSERTED, because the version stated in a comment had rotted.

    That comment said the widths summed to 86 for a total of 106 while they actually summed
    to 88 for 108 -- stale from a narrower `leg` and never re-added. A budget nothing checks
    is a budget that is wrong by an unknown amount, and overflow clips the RIGHTMOST
    columns, which is where P&L lives.

    108 is what a 110-column terminal leaves. DataTable adds 2 cells of padding per column.
    """
    async with app.run_test(size=(126, 46)) as pilot:
        await goto(pilot, "positions")
        t = app.query_one("#pos-table", DataTable)
        cols = list(t.columns.values())
        widths = [c.width for c in cols]
    total = sum(widths) + 2 * len(cols)
    assert total <= 108, f"{total} cells over a 108-cell budget: {widths}"
    labels = [str(getattr(c.label, "plain", c.label)) for c in cols]
    assert labels == ["leg", "qty", "ccy", "entry", "last", "date", "sl",
                      "value", "P&L", "%"], labels
    # `value` AND `P&L` CARRY NO CURRENCY, and must not: they hold the home currency on an
    # open leg and the native one on a closed leg, so a label would be false for half the
    # table. The per-row `ccy` column is the only place that can be right for every row.
    for bad in ("CAD", "USD"):
        assert not any(bad in x for x in labels), (bad, labels)


async def test_no_column_exists_for_a_field_the_tool_cannot_write(app, goto):
    """`tp` WAS A COLUMN NOTHING COULD EVER FILL: one read of `trades.target` in
    book.positions() and no UPDATE anywhere that sets it, so all seven open legs held NULL
    and the column rendered a stack of em-dashes -- 8 of the 108 cells, spent on furniture.

    Deleting the reader is the cure the author already chose for the two unwritable halves, and it
    is what paid for `ccy`. Stated as an equality so it bites in BOTH directions: re-adding
    the column without a way to fill it fails, and adding a writer without somewhere to
    show it fails too.

    Reads the MOUNTED COLUMNS, not the source. The first version grepped panes.py for "tp"
    and matched the comment explaining the deletion -- prose about a dead column is not a
    dead column, which is the same mistake the unbound-key guard made.
    """
    src = pathlib.Path(book.__file__).read_text()
    statements = re.findall(r"UPDATE\s+trades\s+SET(.*?)WHERE", src, re.I | re.S)
    assert statements, "no UPDATE trades statements at all -- has the regex rotted?"
    writable = any("target" in x for x in statements)

    async with app.run_test(size=(126, 46)) as pilot:
        await goto(pilot, "positions")
        t = app.query_one("#pos-table", DataTable)
        labels = [str(getattr(c.label, "plain", c.label)) for c in t.columns.values()]
    shown = "tp" in labels

    assert writable == shown, (
        "a target column with no writer, or a writer with nowhere to show it: "
        f"writable={writable} shown={shown} labels={labels}")


# --------------------------------------------------------------------------- #
# the grammar, and the footer that has to fit it
#
# The author: "seems like for desk i need to open up ?" and "your entry system is way too
# complicated. way too complicated for the user."
# --------------------------------------------------------------------------- #
GRAMMAR_BUDGET = 70


def test_every_grammar_fits_the_border_subtitle():
    """THE OBJECTIVE TEST OF "TOO COMPLICATED", and it is not arbitrary.

    The grammar renders in the prompt's border subtitle, which is ONE line. Five of ten were
    longer than the author's terminal and were clipped mid-word -- the two worst being `bet status`
    at 155 characters and `instrument` at 157, whose entire purpose is listing the options.
    So they read them in `?` instead, which is what they complained about.

    daylogs keeps all eleven of its grammars under 70 and never needs its overlay. A field
    list that cannot be stated in 70 characters is describing something too complicated,
    which makes this budget a test of the grammar and not only of the rendering.
    """
    over = [(h.label, len(h.grammar)) for h in keymap.HINTS
            if len(h.grammar) > GRAMMAR_BUDGET]
    assert not over, f"clipped in the subtitle: {over}"


def test_the_help_overlays_own_tables_fit_the_overlay():
    """THE SAME BUG, in the place the subtitles send you when they run out of room.

    `?` is where the long form lives, and its sigil list is a TABLE -- aligned columns, one
    row per sigil. Textual wraps a row that does not fit, and the tail lands at the LEFT
    MARGIN, unindented, where it reads as another field:

          @when     a date, a time, or today / yesterday   @2026-09-08
        @today                                  <- looks like a sigil named @today

    Three of the six rows were doing that. Prose is exempt and deliberately so: one logical
    line per paragraph, wrapped by Textual at whatever width it gets. A HARD break is worse
    than none, because a break chosen for this width re-wraps into an orphan at every other
    one -- which is how `-- and where a symbol is` ended up alone on a line.

    The budget is READ FROM THE STYLESHEET rather than hardcoded, so widening the overlay
    widens the allowance instead of leaving a stale number that fails for no reason.
    """
    import re

    from desk.tui import app as app_mod

    css = pathlib.Path(app_mod.__file__).with_name("app.tcss").read_text()
    block = re.search(r"#help-body\s*\{(.*?)\}", css, re.S)
    assert block, "no #help-body rule -- the overlay's width is no longer knowable"
    width = int(re.search(r"width:\s*(\d+)", block.group(1)).group(1))
    pad = int(re.search(r"padding:\s*\d+\s+(\d+)", block.group(1)).group(1))
    assert "border:" in block.group(1)
    budget = width - 2 * pad - 2          # both paddings, both border cells

    offenders = []
    for name in ("_HELP_GRAMMAR", "_HELP_NET"):
        for i, line in enumerate(getattr(app_mod, name).split("\n")):
            if line.startswith("  ") and len(line) > budget:
                offenders.append(f"{name}[{i}] is {len(line)} > {budget}: {line!r}")
    assert not offenders, "\n".join(offenders)


async def test_the_help_overlay_can_be_scrolled_to_its_end(app):
    """It is 90-odd rows in a 37-row box, so every word past the fold depends on this.

    The dashboard's `net` and the bets page's bars were both unreachable content below a
    fold; this is the same shape, and the overlay is where the keys are DOCUMENTED, so a
    silent clip here is the worst version of it. VerticalScroll takes focus on its own --
    the assertion is that it still does, not that someone wired it up.
    """
    async with app.run_test(size=(90, 46)) as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        body = app.screen.query_one("#help-body")
        assert app.screen.focused is body, app.screen.focused
        assert body.virtual_size.height > body.size.height, "fixture no longer overflows"
        await pilot.press("end")
        await pilot.pause()
        assert body.scroll_y > 0, "the overlay cannot be scrolled past its fold"


# label -> the parser behind it, so a hint can be checked against the thing it describes.
# A prompt with no entry here is a prompt nothing tests, which is why the test below asserts
# the map is complete rather than skipping what it does not know.
PARSERS = {
    "fill": lambda s: parse.parse_fill(s, now=NOW),
    "capital in": lambda s: parse.parse_flow(s, now=NOW),
    "offset": lambda s: parse.parse_adjustment(s, now=NOW),
    "account": lambda s: parse.parse_account(s),
    "bet status": lambda s: parse.parse_bet_status(s, now=NOW),
    "stop": lambda s: parse.parse_stop(s),
    "allocation": lambda s: parse.parse_allocation(s, now=NOW),
    "listing": lambda s: parse.parse_listing(s),
    "bet": lambda s: parse.parse_bet(s, now=NOW),
    # The example is a NAME, so it needs the map the app supplies at runtime.
    "leg bet": lambda s: parse.parse_trade_bet(
        s, known={"big techs and semis": "big-techs-and-semis"}),
    "rename": lambda s: parse.parse_bet_name(s),
}


def test_every_grammar_names_only_fields_its_parser_accepts():
    """A hint that advertises a field the parser REFUSES is worse than no hint at all.

    This is not hypothetical: the account grammar read `=default to make it the sleeve...`
    right up until `=` was taken off that prompt, and a reader following it would have been
    told "a account has no = field — drop =".

    Checked by feeding each parser the sigil its own hint mentions and asserting the refusal
    is not the "no such field" one. An earlier version of this test looped over sigils inside
    `if sigil not in grammar: continue` and asserted `sigil in grammar` -- vacuously true.
    """
    assert set(PARSERS) == {h.label for h in keymap.HINTS}, "a prompt with no parser mapped"
    for h in keymap.HINTS:
        # A FIELD IS `<sigil><name>` WITH NO SPACE, which is the notation itself. The account
        # hint says "id, typed after / on every fill" -- prose ABOUT the fill grammar's
        # sigil, not a field this prompt takes, and an earlier version of this test failed on
        # exactly that.
        fields = {m.group(1) for m in
                  re.finditer(rf"([{re.escape(parse.SIGILS)}])\S", h.grammar)}
        for sigil in fields:
            try:
                PARSERS[h.label](f"{h.example} {sigil}x")
            except parse.ParseError as exc:
                assert "has no" not in str(exc), (
                    f"{h.label} advertises {sigil} and refuses it: {exc}")


def test_the_default_flag_works_wherever_it_is_typed(con):
    """IT WENT WRONG TWICE FOR THE SAME REASON, one layer apart.

    As `=default` it was swallowed by the tilde fold, which joined the following tokens'
    values and stripped their sigils. As a bare word it was swallowed again, because `~` for
    a NAME runs to the next sigil and `default` is not one. Both times the flag silently did
    nothing while the name grew a word.

    It is removed at the TOKEN level now, before anything can absorb it.
    """
    for line in ("fresh default", "default fresh", "fresh ~A Name default",
                 "fresh default ~A Name"):
        r = parse.parse_account(line, known_accounts=frozenset())
        assert r.make_default is True, line
        assert r.account_id == "fresh", line
        assert (r.name or "") in ("", "A Name"), (line, r.name)
    assert parse.parse_account("fresh ~A Name").make_default is False
    # And `=` no longer carries a flag anywhere.
    with pytest.raises(parse.ParseError, match="has no ="):
        parse.parse_account("fresh =default")


@pytest.mark.parametrize("width", [70, 80, 100, 126])
async def test_the_footer_shows_the_panes_own_verbs_at_every_width(app, width, goto):
    """IT COLLAPSED TO `? keys · q quit`, and that is why the author reached for `?`.

    Measured before the fix: at 80 columns every tab, and at 100 columns positions, showed
    nothing but the two pinned keys. The shedder popped whole GROUPS, so one column of
    pressure dropped six keys at once. A footer that answers "what can I do here?" with
    nothing teaches you not to look at the footer.
    """
    async with app.run_test(size=(width, 30)) as pilot:
        for scope in ("dashboard", "positions", "bets"):
            await goto(pilot, scope)
            # ONE LINE PER GROUP NOW, so the assertions are per line and across all of them.
            # This test used to read only the LAST line, which was correct while every key
            # shared one row and became "is `q quit` on the nav row" the moment they were
            # stacked -- passing while the write row could have been empty.
            lines = [x for x in _plain(app, "#keyfooter")[1:] if x.strip()]
            assert lines, f"{scope}@{width}: no key lines at all"
            for line in lines:
                assert len(line) <= width, (
                    f"{scope}@{width}: {len(line)} > {width}: {line!r}")
            joined = " ".join(lines)
            # At least two of the pane's OWN verbs survive, not just `?` and `q`.
            own = [k for k in keymap.for_scope(scope)
                   if k.scope == scope and k.kind == "write"]
            shown = [k for k in own if f"{k.key} {k.label}" in joined]
            assert len(shown) >= min(2, len(own)), (
                f"{scope}@{width}: only {[k.label for k in shown]} of "
                f"{[k.label for k in own]}")
            assert "? keys" in joined and "q quit" in joined, "pinned keys must survive"
            # AND THE WRITE ROW IS NEVER THE ONE SACRIFICED. Shedding is per line now, so an
            # over-wide first group can shrink on its own -- the bug daylogs shipped was a
            # global drop order that emptied every later group before touching the first.
            assert any(f"{k.key} {k.label}" in lines[0] for k in own), (
                f"{scope}@{width}: the write row lost all of its own verbs: {lines[0]!r}")


async def test_a_cycling_prompt_has_something_to_cycle(app, con, goto):
    """The other half of the tab promise: `_CYCLES` names the labels whose tab STEPS a pool,
    and a pool that comes back empty is a dead key wearing a hint that promises otherwise.

    `leg bet` is checked against the real book rather than a literal, because its pool is the
    live bets plus one empty string for detaching -- and the empty string is the part a
    hand-written expectation would forget.
    """
    from desk.tui.prompt import _CYCLES

    assert _CYCLES, "no label cycles -- has the mechanism gone?"
    async with app.run_test(size=(126, 46)) as pilot:
        await goto(pilot, "bets")
        for label in _CYCLES:
            pool = app.vocabulary(label)
            assert pool, f"{label} cycles an empty pool"
            if label == "leg bet":
                assert "" in pool, "tab must be able to reach 'no bet'"
                names = {b.name for b in book.bets(con) if book.is_live(b.status)}
                assert names <= pool, f"{names - pool} missing from the pool"


def test_the_leg_bet_prompt_resolves_a_name_back_to_its_id():
    """`tab` puts a NAME in the field, so the parser has to map it back to the key.

    The author: "moving trades between bets can be its own key where i can tab through the target
    bet." Names have spaces, ids do not, and the thing written to `trades.bet_id` is the id.
    """
    known = {"big techs and semis": "big-techs-and-semis", "growth": "growth"}
    assert parse.parse_trade_bet("Big Techs and Semis", known=known) == (
        "big-techs-and-semis")
    # The slug still works typed by hand, with or without the sigil.
    assert parse.parse_trade_bet("growth", known=known) == "growth"
    assert parse.parse_trade_bet("!growth", known=known) == "growth"
    # EMPTY DETACHES, which is the one thing no other prompt accepts.
    assert parse.parse_trade_bet("", known=known) is None
    assert parse.parse_trade_bet("   ", known=known) is None
    # Several words that name nothing is an ERROR, not a guess at which bet was meant.
    with pytest.raises(parse.ParseError):
        parse.parse_trade_bet("No Such Bet Here", known=known)


def test_no_data_file_is_tracked_in_git():
    """The author, 2026-09-25, on publishing: "just make sure we dont record it later on."

    An ignore rule is a convention; this is the enforcement. It asks GIT what is tracked
    rather than reading .gitignore, so it catches the case the ignore file cannot: a file
    already committed before a rule existed, or one added with `git add -f`.

    THIS HAS HAPPENED TWICE. `book/hub.db` was tracked until 2026-09-07 with their real
    positions in it, and 51 files of research notes under `theses/` went the same way --
    both removed later, both still in the history. The ignore rules were also weaker than
    they looked: `book/hub.db` matched only that one path, so a book under any other name
    was committable, and `theses/` had no rule at all.

    A schema is not data, which is why `.sql` is not in the list: `book/schema.sql` IS the
    product.
    """
    import subprocess

    root = pathlib.Path(__file__).resolve().parent.parent
    tracked = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True,
                             text=True, check=True).stdout.split()
    assert tracked, "git ls-files returned nothing -- is this a checkout?"

    suspect = []
    for path in tracked:
        low = path.lower()
        if low.endswith((".db", ".db-journal", ".db-wal", ".db-shm", ".pdf", ".csv")):
            suspect.append(path)
        elif low.startswith(("theses/", "statements/", "archive/")):
            suspect.append(path)
    assert not suspect, f"data is tracked in git: {suspect}"


def test_the_ignore_rules_catch_a_book_under_any_name():
    """The rule used to be the literal path `book/hub.db`, so `mybook.db` was fair game.

    Checked through `git check-ignore`, which is the same matcher `git add` uses -- reading
    .gitignore and reimplementing its precedence here would test my parser, not git's.
    """
    import subprocess

    root = pathlib.Path(__file__).resolve().parent.parent
    for candidate in ("book/hub.db", "mybook.db", "sub/dir/scratch.db",
                      "theses/Bets/Something.md", "statements/May_2026.pdf",
                      "May_2026.pdf"):
        r = subprocess.run(["git", "check-ignore", "-q", candidate],
                           cwd=root, capture_output=True)
        assert r.returncode == 0, f"{candidate} would be committed"
    # AND THE TOOL ITSELF IS NOT IGNORED, which is the other half: a rule broad enough to
    # swallow the source would pass the assertions above and break the repo.
    for keep in ("book/schema.sql", "desk/book.py", "tests/seed.py"):
        r = subprocess.run(["git", "check-ignore", "-q", keep],
                           cwd=root, capture_output=True)
        assert r.returncode != 0, f"{keep} is ignored, but it is the product"


def test_the_screenshot_tool_cannot_read_the_real_book():
    """The screenshots are published, so they are the one artifact where a leak is permanent.

    THE FIRST RUN OF THAT TOOL PUBLISHED THE AUTHOR'S NAME over invented figures. A synthetic
    BOOK was not enough: the dashboard titles itself from `config.profile()`, which reads
    config.toml under $TRADING_DESK_HOME and has nothing to do with which database is open.
    So the tool has to redirect BOTH, and this asserts it still does -- the failure mode is
    silent, and the artifact is a PNG nobody re-reads.

    Static, on the source, because running the tool takes seconds and the property worth
    guarding is what it redirects rather than what it renders.
    """
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "tools" / "screenshots.py").read_text()
    assert "config.ENV_HOME" in src, "the profile is not redirected — the real name leaks"
    assert "config.ENV_BOOK" in src, "an inherited $TRADING_DESK_BOOK would be read"
    assert "seed.build" in src, "the demo data must be the synthetic fixture"
    assert "POLL_DISABLED" in src, "a screenshot run must not hit the network"
    # AND IT MUST NOT NAME A REAL HOLDING. The fixture's tickers are invented; a real one
    # appearing here would mean someone hand-wrote demo data instead of using the fixture.
    for real in ("SGOV", "CBIL", "AEM.TO", "CASH.TO", "ZGLD"):
        assert real not in src, f"{real} is a real holding, not fixture data"


def test_no_footer_label_is_longer_than_it_needs_to_be():
    """daylogs fits 41 keys with a mean label of 6.2 characters; desk had 19 with a mean of
    7.4 and a longest of 14, and its footer was collapsing. A label earns its words by
    disambiguating WITHIN one pane -- `new instrument` says nothing `instrument` does not."""
    labels = [k.label for k in keymap.KEYMAP if k.footer]
    longest = max(labels, key=len)
    assert len(longest) <= 10, f"{longest!r} is {len(longest)} characters"


@pytest.mark.parametrize("label,typed,field,expected", [
    ("bet status", "", "status", parse.BET_STATUSES),
    ("bet status", "cl", "status", ("closed",)),
    ("capital in", "1000 ", "currency", config.CURRENCIES),
    ("offset", "-4.95 U", "currency", ("USD",)),
])
def test_tab_offers_the_closed_vocabularies(label, typed, field, expected):
    """WHY THE HINTS CAN LEAVE THEM OUT, and the reason this had to be built.

    `bet status` was 155 characters because it listed all seven values and `instrument` was
    157 because it listed six kinds -- both far past the width of the subtitle they render
    in, so both were clipped exactly where the options were. The short versions say
    "(tab lists them)", and a hint that says that and then does not is worse than the long
    one it replaced.
    """
    c = parse.complete(typed, len(typed), label,
                       tickers=("ACME",), bets=("b1",), accounts=("tfsa",))
    assert c.field == field, (label, typed, c.field)
    assert set(c.matches) == set(expected), (label, typed, c.matches)


def test_a_hint_that_promises_tab_gets_it():
    """Every "(tab...)" in a grammar must correspond to a real completion.

    Read off the hints rather than listed here, so adding the phrase to a new prompt without
    wiring the vocabulary fails rather than ships.
    """
    from desk.tui.prompt import _CYCLES

    promised = [h for h in keymap.HINTS if "tab" in h.grammar]
    assert promised, "no hint mentions tab -- has the wording changed?"
    for h in promised:
        if h.label in _CYCLES:
            # A CYCLING PROMPT STEPS A POOL, it does not complete a prefix, so
            # parse.complete() is the wrong place to look for it. What must be true is that
            # the pool is not empty -- an app-level vocabulary, asserted in its own test
            # below -- and that the label is registered as cycling at all, which is this.
            continue
        # Complete at the START of the field the phrase sits next to: for both current
        # cases that is a bare slot, so an empty line or the example's leading words.
        offers = False
        for upto in range(len(h.example) + 1):
            c = parse.complete(h.example[:upto], upto, h.label,
                              tickers=("ACME",), bets=("b1",), accounts=("tfsa",),
                              # `listing` is the one pool that does not come from the book:
                              # they are the candidates a lookup just returned, held on the
                              # pane. Supplied here the way the app supplies them.
                              listings=("COIN", "COIN.TO"))
            if c.matches:
                offers = True
                break
        assert offers, f"{h.label} says tab lists them and nothing does"


def test_the_dead_as_of_grammar_is_gone():
    """`k` went in v11 with the rules it selected between, and the grammar outlived it.

    Dead code that raises when revived is worse than none -- the same reason
    book.accounts() was repaired rather than left naming two dropped columns.
    """
    assert not hasattr(parse, "parse_as_of")
    assert not hasattr(book, "set_clock")
    assert not hasattr(book, "clock_as_of")
    assert not any(h.label == "as-of date" for h in keymap.HINTS)


async def test_t_previews_a_theme_and_escape_puts_the_old_one_back(app):
    async with app.run_test(size=(110, 40)) as pilot:
        was = app.theme
        await pilot.press("t")
        await pilot.pause()
        assert app.theme_picker.is_open
        await pilot.press("right")
        await pilot.pause()
        assert app.theme != was, "← → must apply the theme live, not just move a cursor"
        await pilot.press("escape")
        await pilot.pause()
        assert app.theme == was, "escape restores the theme the picker opened on"
        assert not app.theme_picker.is_open


async def test_enter_keeps_the_previewed_theme_and_writes_it_to_config(app, tmp_path,
                                                                      monkeypatch):
    """ISOLATED, because this WRITES. An earlier probe of mine hit the real config.toml."""
    monkeypatch.setenv("TRADING_DESK_HOME", str(tmp_path))
    monkeypatch.setattr(config, "_config_cache", None)
    (tmp_path / "config.toml").write_text('# a comment worth keeping\nname = "Test"\n')
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        chosen = app.theme_picker.selected
        await pilot.press("enter")
        await pilot.pause()
    assert app.theme == chosen
    written = (tmp_path / "config.toml").read_text()
    assert f'theme = "{chosen}"' in written
    # A LINE EDIT, NOT A TOML REWRITE: tomllib cannot write, and any writer that can would
    # discard the comments that make this file editable by hand.
    assert "# a comment worth keeping" in written
    assert 'name = "Test"' in written


async def test_any_other_key_ends_a_preview_rather_than_leaving_it_stuck(app, goto):
    """daylogs learned this: every write key opens a prompt and takes focus, which left the
    picker displayed but deaf -- still advertising keys that now did something else."""
    async with app.run_test(size=(110, 40)) as pilot:
        was = app.theme
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        assert app.theme != was
        await pilot.press("2")                 # a tab switch, not a picker key
        await pilot.pause()
        assert not app.theme_picker.is_open
        assert app.theme == was, "the preview outlived the picker"


def test_a_bad_theme_name_falls_back_rather_than_stopping_the_desk():
    """A cosmetic setting must not take the whole desk down, and it is edited by hand."""
    assert themes.resolve("nord") == "nord"
    assert themes.resolve("no-such-theme") == themes.DEFAULT
    assert themes.resolve(None) == themes.DEFAULT
    assert themes.DEFAULT in themes.names()


def test_the_theme_strip_always_fits_its_width():
    """It is measured with len(), which is only true because the cursor is marked with
    CHARACTERS rather than markup -- the one thing that must not change here."""
    names = themes.names()
    for width in (40, 60, 80, 120, 200):
        for i in (0, 1, len(names) // 2, len(names) - 1):
            out = themes.strip(names, i, width)
            assert len(out) <= width, (width, i, len(out))
            assert f"▸{names[i]}◂" in out, "the cursor must always be visible"


async def test_a_typed_fill_lands_in_the_named_account(app, con, type_into, goto):
    """`/rrsp` overrides the default, and the account is part of the position key."""
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await pilot.press("n")
        await type_into(pilot, "USDCO 1 430.00 !growth /rrsp")
        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, app.prompt.error
    row = con.execute(
        "SELECT t.account_id FROM fills f JOIN trades t ON t.id=f.trade_id "
        "ORDER BY f.id DESC LIMIT 1").fetchone()
    assert row["account_id"] == "rrsp"


async def test_a_fill_with_no_account_takes_the_recorded_default(app, con, type_into):
    async with app.run_test() as pilot:
        await pilot.press("n")
        await type_into(pilot, "USDCO 1 430.00 !growth")
        await pilot.press("enter")
        await pilot.pause()
    row = con.execute(
        "SELECT t.account_id FROM fills f JOIN trades t ON t.id=f.trade_id "
        "ORDER BY f.id DESC LIMIT 1").fetchone()
    assert row["account_id"] == book.default_account(con) == "tfsa"


async def test_a_misspelt_account_is_refused_not_stored_as_none(app, con, type_into,
                                                                 goto):
    """trades.account_id is nullable, so a typo would silently mean 'no account'."""
    before = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await pilot.press("n")
        await type_into(pilot, "USDCO 1 430.00 !growth /tfsaa")
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.is_open
        assert "not an account" in app.prompt.error
    assert con.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == before


def test_the_same_ticker_in_two_sleeves_is_two_positions(con):
    """PARK is really held in both. One trade would blend the cost bases."""
    a = book.add_fill(con, ticker="USDCO", shares=1, price=400.0,
                      bet_id="growth", on_date="2026-09-07", account="tfsa")
    b = book.add_fill(con, ticker="USDCO", shares=1, price=500.0,
                      bet_id="growth", on_date="2026-09-07", account="rrsp")
    assert a[0] != b[0], "the account is part of the position key"
    held = {p.account: p.entry for p in book.positions(con) if p.ticker == "USDCO"}
    assert held["rrsp"] == pytest.approx(500.0), "the RRSP leg kept its own basis"


def test_contributions_pool_across_accounts(con):
    """The limit is one wall for one portfolio, so the denominator is both sleeves.

    This pinned {"tfsa": 35000, "rrsp": 10000} and went red when the author remembered a
    two-dollar top-up — punishing the book for becoming more accurate. What it
    should assert is the RELATIONSHIP: the pooled figure is the sum of the sleeves,
    and no contribution is unattributed.
    """
    per = dict(con.execute("SELECT account_id, SUM(amount) FROM flows "
                           "GROUP BY account_id").fetchall())
    assert None not in per, "a flow with no account vanishes from every sleeve total"
    assert len(per) > 1, "this book has more than one funded account"
    assert book.capital_in(con) == pytest.approx(sum(per.values()))


def test_pnl_is_flow_immune_whatever_the_deposit_is_dated(blank):
    """A DEPOSIT MUST NOT MOVE P&L. It raises cash and contributed by the same amount.

    THREE TESTS COLLAPSE INTO THIS ONE, and that is the point worth recording. There used
    to be test_pnl_is_flow_immune, test_a_deposit_dated_after_the_balance_cannot_manufacture
    _a_loss and test_a_withdrawal_after_the_balance_cannot_invent_a_gain -- three separate
    guards against one bug, which was that `balance` came from a statement dated D while
    `capital` summed flows up to D, so anything in between landed in P&L as a gain or loss
    of exactly its own size. Real numbers: C$6,000 in gave a C$4,465 "loss".

    Derived net has no D. It contains every deposit already, so the two sides are the same
    moment by construction and the date cannot matter. Hence the second half: a deposit
    dated years later, and a withdrawal, and P&L does not budge.
    """
    book.add_flow(blank, amount=40_000.0, on_date="2026-01-01")
    _buy(blank, "t1", "CADCO", "CAD", 100.0, 20.0)
    _mark(blank, "CADCO", "CAD", 18.0)
    before = book.net(blank).pnl
    assert before == pytest.approx(-200.0), before

    book.add_flow(blank, amount=10_000.0, on_date="2026-01-02")
    assert book.net(blank).pnl == pytest.approx(before), "the deposit moved P&L"
    book.add_flow(blank, amount=5_000.0, on_date="2029-12-31")
    assert book.net(blank).pnl == pytest.approx(before), "a future deposit moved P&L"
    book.add_flow(blank, amount=-3_000.0, on_date="2029-12-31")
    assert book.net(blank).pnl == pytest.approx(before), "a withdrawal moved P&L"


def test_the_attribution_says_so_when_the_balance_is_stale(con):
    """A fill after the snapshot makes the residual meaningless, so it must say so.

    Filtering the fills out was tried and was worse: realised came from the filtered
    set while unrealised came from v_position, which is not date-aware, so a late
    sale left realised and moved the position it was valued against. Reporting beats
    half-correcting.
    """
    import contextlib
    import io

    from desk import check as bookcheck
    book.add_fill(con, ticker="USDCO", shares=-10, price=200.0, bet_id="growth",
                  on_date="2026-12-31")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bookcheck.attribution(con)
    out = buf.getvalue()
    assert "STALE" in out, out
    assert "Re-read the balance" in out


async def test_undo_restores_a_deleted_flow(app, blank, type_into, goto):
    async with app.run_test() as pilot:
        await goto(pilot, "dashboard")
        await pilot.press("c")
        await type_into(pilot, "1000 @2026-09-01 ~test")
        await pilot.press("enter")
        await pilot.pause()
        assert book.capital_in(blank) == 1000.0
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert book.capital_in(blank) is None
        await pilot.press("u")
        await pilot.pause()
        assert book.capital_in(blank) == 1000.0, \
            "undo must restore account_id too, or the row reappears unattributed"
        assert blank.execute("SELECT account_id FROM flows").fetchone()[0] == "tfsa"


async def test_the_help_overlay_opens_and_closes(app):
    async with app.run_test() as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        assert len(app.screen_stack) == 2
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1


async def test_the_positions_columns_fit_a_standard_terminal(app):
    """Overflow clips the RIGHTMOST columns silently, and that is where P&L lives.

    A half-shown P&L is worse than no P&L: `▲ +197.` reads as a number. DataTable
    adds 2 cells of padding per column on top of the declared widths.
    """
    async with app.run_test(size=(110, 24)) as pilot:
        await pilot.pause()
        t = app.query_one("#pos-table")
        declared = sum(c.width for c in t.columns.values())
        total = declared + 2 * len(t.columns)
        assert total <= 110 - 2, (
            f"columns need {total} cells, and a narrow-but-normal 110-column "
            f"terminal leaves 108 after the table margin")


async def test_the_footer_never_advertises_a_key_the_pane_cannot_handle(app, goto):
    """WIDE ON PURPOSE. Below the 100-column breakpoint the footer keeps only the pinned
    keys, so asserting a pane key appears there would be asserting the wrong thing --
    see the narrowing test below, which is about that behaviour instead."""
    async with app.run_test(size=(120, 30)) as pilot:
        await goto(pilot, "dashboard")    # the dashboard has no `n`
        rendered = app.key_footer.text
        assert "new fill" not in rendered, "the dashboard has no key_fill"
        # `balance` was the key asserted here until v11 removed it, and `new account` until
        # the labels were shortened to stop the footer collapsing. `a account` is one of the
        # dashboard's writes; if it stops appearing, the pane advertises nothing it can do.
        assert "a account" in rendered, "but it does record one"


async def test_a_narrow_footer_still_leaves_a_way_to_find_every_key(app, goto):
    """At 80 columns the footer drops to `? keys · q quit` and that is the design.

    It is only defensible because `?` is PINNED: the write keys become undiscoverable
    otherwise, which is a worse version of the phantom-key problem -- a key that exists
    and is advertised nowhere.
    """
    async with app.run_test(size=(80, 24)) as pilot:
        await goto(pilot, "positions")
        rendered = app.key_footer.text
        assert "? keys" in rendered, f"no way to discover the rest: {rendered!r}"
        assert "q quit" in rendered
    pinned = [k.key for k in keymap.KEYMAP if k.pin]
    assert "question_mark" in pinned, "the escape hatch must be pinned, not lucky"


async def test_the_positions_header_pnl_excludes_unpriced_legs(app, con):
    """It printed +64.30% on a book whose real open-leg P&L was +1.40%.

    `sum(r.cost_base or 0.0)` counted a missing entry as a zero cost while the same
    leg's full market value went into value, so the header invented ~11,500 CAD of
    gain.

    THIS TEST USED TO SKIP UNCONDITIONALLY, which is worse than not existing. The
    fixture prices every leg, so `len(priced) == len(rows)` was always true and the
    guard had zero coverage -- a reviewer reintroduced the original bug and the whole
    suite still passed. It now MAKES the hazardous state with seed.unprice() instead
    of hoping to find it.
    """
    seed.unprice(con)                       # a mark, no cost basis: the 2026 bug
    rows = book.positions(con)
    priced = [r for r in rows
              if r.cost_base is not None and r.value_base is not None]
    unpriced = [r for r in rows if r.cost_base is None]
    assert priced and unpriced, "the mutation must leave BOTH kinds of leg"
    expected = sum(r.value_base for r in priced) - sum(r.cost_base for r in priced)
    async with app.run_test(size=(150, 30)) as pilot:
        await pilot.pause()
        head = str(app.query_one("#pos-head").render())
    assert f"{expected:+,.2f}" in head, f"header does not show {expected:+,.2f}: {head}"
    assert "exclude" in head, "the header must name what it left out"
    # And the bug's signature must be ABSENT: the naive sum would have counted the
    # unpriced leg's full market value as gain.
    naive = sum(r.value_base for r in rows if r.value_base is not None) \
        - sum(r.cost_base for r in priced)
    assert f"{naive:+,.2f}" not in head, "the header is still summing a missing cost"


def test_cost_and_mark_are_not_silently_incomparable(con):
    """`at cost` and `at mark` must not be printed as comparable when they are not.

    THIS TEST USED TO ASSERT THE BUG. It ended on

        assert b.positions_value > b.positions_cost

    which is the HAZARD, not the handling: positions_cost summed `p.cost_base or 0.0`, so
    a leg with no recorded entry contributed nothing to cost and its whole value to mark,
    and the dashboard printed a phantom gain of exactly that position's size. The audit
    caught the test as well as the code -- it would have gone on passing forever with the
    defect in place, because it was pinned to the fixture rather than to the rule.

    The rule is the project's own: where nothing is recorded, the figure is withheld.
    """
    seed.unprice(con)
    nocost = [p.ticker for p in book.positions(con) if p.cost_base is None]
    assert nocost, "the mutation must leave an unpriced leg"
    n = book.net(con)
    holed = [s for s in n.sleeves if s.positions_cost is None]
    assert holed, (
        f"cost must be withheld while {nocost} have no entry price, not summed as zero")
    # The em dash still has to be explained. It used to be an obligation naming the legs;
    # it is Net.unknown now, and the requirement is unchanged -- a blank with no reason is
    # a figure the reader has to guess about.
    assert n.unknown, "a withheld figure with no stated reason is a mystery"


def test_a_usd_leg_with_no_fx_rate_is_not_valued_at_par(con):
    """A USD leg in a CAD book with no rate must be unvalued, not valued 1:1.

    positions() defaulted a missing rate to 1.0, so a USD leg reported ~38% low and
    looked like a real number. attribution() already refused exactly this ("a par
    rate would be wrong by the whole FX factor"), so the two halves of the tool
    disagreed about the same hazard.
    """
    con.execute("DELETE FROM prices WHERE kind='fx'")
    usd = [p for p in book.positions(con) if p.currency == "USD"]
    cad = [p for p in book.positions(con) if p.currency == "CAD"]
    assert usd and cad, "the fixture must hold both, or this proves nothing"
    for p in usd:
        assert p.fx is None, f"{p.ticker}: a missing rate became {p.fx}"
        assert p.cost_base is None and p.value_base is None
    for p in cad:
        # A CAD leg in a CAD book needs no rate; 1.0 there is a fact, not a fallback.
        assert p.fx == 1.0 and p.cost_base is not None


# --------------------------------------------------------------------------- #
# bets: a book row and nothing else
#
# There were fifteen tests here about vault folders, thesis templates, H1 titles,
# pointer rot and sha256 drift. All of it is gone, because the tool no longer
# touches a vault -- the author: "i manually handle the thesis, i create notes via
# shortcuts in obsidian or by chatting with AI, the book tool only manages the
# [book]." Deleting those tests is not a loss of coverage; the behaviour they
# covered does not exist.
# --------------------------------------------------------------------------- #
async def test_n_in_the_bets_pane_registers_a_bet(app, con, type_into):
    async with app.run_test() as pilot:
        await pilot.press("3")
        await pilot.pause()
        assert app.scope == "bets"
        await pilot.press("n")
        await pilot.pause()
        await type_into(pilot, "memory-cycle =8000 ~DRAM repricing has further to run")
        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, app.prompt.error
    row = con.execute(
        "SELECT status, opened, notes, allocation_base, allocation_ccy, "
        "allocation_set_on FROM bets WHERE id='memory-cycle'").fetchone()
    assert row["status"] == "forming", "a bet is not live until it can be refuted"
    assert row["notes"] == "DRAM repricing has further to run"
    # A frozen allocation is meaningless without its currency and the date it was set.
    assert row["allocation_base"] == 8000.0
    assert row["allocation_ccy"] and row["allocation_set_on"]


async def test_registering_a_bet_writes_no_file_anywhere(app, con, tmp_path,
                                                         type_into, monkeypatch):
    """THE POINT of removing the vault. Registering a bet touches the book only.

    Watches the repo and a scratch cwd. If a folder or a template ever comes back,
    this fails. The cwd gets its OWN directory: tmp_path holds the book fixture, so
    watching it would count the database and its journal as files the bet created.
    """
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    before = {p for p in ROOT.rglob("*") if ".git" not in p.parts}
    async with app.run_test() as pilot:
        await pilot.press("3")
        await pilot.press("n")
        await type_into(pilot, "no-files =100")
        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, app.prompt.error
    after = {p for p in ROOT.rglob("*") if ".git" not in p.parts}
    created = {p for p in after - before if "__pycache__" not in p.parts}
    assert not created, f"registering a bet created files: {created}"
    assert not list(cwd.iterdir()), f"and wrote into the cwd: {list(cwd.iterdir())}"
    assert con.execute("SELECT 1 FROM bets WHERE id='no-files'").fetchone()


async def test_a_bet_needs_no_note_at_all(app, con, type_into):
    """`~note` used to be a required title, because the desk created the file and
    needed an H1. Nothing is created now, so the id is the whole requirement."""
    async with app.run_test() as pilot:
        await pilot.press("3")
        await pilot.press("n")
        await type_into(pilot, "bare-bet")
        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, app.prompt.error
    row = con.execute("SELECT notes, allocation_base FROM bets "
                      "WHERE id='bare-bet'").fetchone()
    assert row["notes"] is None and row["allocation_base"] is None


async def test_a_duplicate_bet_is_refused_in_the_prompt(app, con, type_into):
    async with app.run_test() as pilot:
        await pilot.press("3")
        await pilot.press("n")
        await type_into(pilot, "growth ~again")
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.is_open, "a rejection keeps the line for correcting"
        assert "already exists" in app.prompt.error


def test_a_bet_id_is_lowercased_rather_than_rejected():
    """Typing AI-CAPEX gets you ai-capex, not an error.

    Case is the one deviation worth normalising instead of refusing: it is the id they will retype
    where the thesis is written, and 'was it capitalised?' is a worse
    thing to have to remember than a rule about hyphens.
    """
    assert parse.parse_bet("AI-CAPEX", now=NOW).bet_id == "ai-capex"
    assert parse.parse_bet("AiCapex", now=NOW).bet_id == "aicapex"


async def test_the_bets_columns_fit_a_standard_terminal(app):
    async with app.run_test(size=(110, 24)) as pilot:
        await pilot.press("3")
        await pilot.pause()
        t = app.query_one("#bet-table")
        total = sum(c.width for c in t.columns.values()) + 2 * len(t.columns)
        assert total <= 108, f"bets table needs {total} cells of 108"


# --------------------------------------------------------------------------- #
# CRUD, which was advertised in the footer and not implemented
#
# The author: "have you made sure that i can create/edit/delete/update data in each tab
# just like daylogs?" It was a no: `enter` and `x` on positions and `enter` on
# capital all printed "not wired yet" while the footer promised them. Worse than
# absent, because the footer is the contract.
# --------------------------------------------------------------------------- #
def test_no_key_in_the_footer_prints_not_wired_yet(app):
    """THE TEST THAT SHOULD HAVE CAUGHT IT.

    test_the_footer_never_advertises_a_key_the_pane_cannot_handle only checked that
    the SCOPE matched — a handler that exists and refuses is invisible to it. Three
    keys shipped that way. This reads the source of every advertised handler.
    """
    import inspect
    offenders = []
    # Resolve handlers the way the app does, per pane class. A dead loop over a hardcoded
    # scope list used to sit here doing nothing but `del` a variable it had just made; it
    # also named "capital" and "balance", neither of which is a scope any more.
    from desk.tui import panes
    for scope, cls in (("positions", panes.PositionsPane),
                       ("dashboard", panes.DashboardPane),
                       ("bets", panes.BetsPane)):
        assert scope in keymap.SCOPES, f"{scope} is not a scope"
        for k in keymap.KEYMAP:
            if k.scope != scope:
                continue
            fn = getattr(cls, f"key_{k.action}", None)
            if fn is None:
                offenders.append(f"{scope}/{k.key}: no handler at all")
                continue
            src = inspect.getsource(fn)
            if "not wired yet" in src or "not implemented" in src:
                offenders.append(f"{scope}/{k.key} ({k.label}): refuses")
    assert not offenders, (
        "keys the footer advertises that do nothing: " + "; ".join(offenders))


async def test_x_deletes_the_newest_fill_and_names_it_first(app, con, goto):
    """A delete you cannot picture is one that should be refused.

    A positions row is an AGGREGATE, so `x` has to choose a fill. It takes the newest
    and the confirmation quotes ticker, signed quantity, price and date.
    """
    before = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await pilot.press("x")
        await pilot.pause()
        # The confirmation must name the fill, not "the selected row".
        assert app._confirm is not None, "x must ask before deleting"
        await pilot.press("y")
        await pilot.pause()
    assert con.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == before - 1


async def test_a_deleted_fill_comes_back_with_u(app, con, goto):
    before = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await pilot.press("x")
        await pilot.press("y")
        await pilot.pause()
        assert con.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == before - 1
        await pilot.press("u")
        await pilot.pause()
    assert con.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == before


async def test_n_after_cancelling_an_edit_does_not_replace_the_row(app, con,
                                                                  type_into, goto):
    """The bug this guards is silent and destructive.

    `enter` arms an edit by stashing the fill id. Escape used to leave it armed, so
    the next plain `n` would DELETE the row that had been under the cursor and add a
    different one in its place.
    """
    before = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await pilot.press("enter")          # arms the edit, prefills the prompt
        await pilot.pause()
        assert app.prompt.is_open and app.prompt.value, "enter must prefill"
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("n")              # a CREATE, not a replace
        await type_into(pilot, "USDCO 1 99.00 !growth")
        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, app.prompt.error
    after = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    assert after == before + 1, "the cancelled edit must not have deleted anything"


async def test_enter_prefills_a_line_that_round_trips(app, con, goto):
    """The prefill has to be a line the parser accepts, or an edit is a dead end.

    parse(render(row)) == row is already a tested property; this asserts the pane
    actually uses it rather than formatting its own string.
    """
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await pilot.press("enter")
        await pilot.pause()
        line = app.prompt.value
    assert line, "enter must prefill something"
    r = parse.parse_fill(line, now=NOW)          # must not raise
    assert r.ticker and r.price > 0


async def test_editing_a_flow_replaces_it_rather_than_adding_one(app, blank, goto):
    book.add_flow(blank, amount=1000.0, on_date="2026-02-01")
    async with app.run_test() as pilot:
        await goto(pilot, "dashboard")
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.is_open and "1000" in app.prompt.value
        # Clear and retype a corrected amount.
        app.prompt.value = "2500 @2026-02-01 ~corrected"
        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, app.prompt.error
    rows = blank.execute("SELECT amount FROM flows").fetchall()
    assert len(rows) == 1, f"an edit must not leave two rows: {rows}"
    assert rows[0][0] == 2500.0


async def test_editing_a_flow_is_undoable(app, blank, goto):
    book.add_flow(blank, amount=1000.0, on_date="2026-02-01")
    async with app.run_test() as pilot:
        await goto(pilot, "dashboard")
        await pilot.press("enter")
        await pilot.pause()
        app.prompt.value = "2500 @2026-02-01"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("u")
        await pilot.pause()
    amounts = sorted(r[0] for r in blank.execute("SELECT amount FROM flows"))
    # EXACTLY the pre-edit state: the original back AND the replacement gone. Stacking
    # only the original left 2500 behind, and when SQLite reused the freed rowid the
    # restore hit the primary key and `u` did nothing whatsoever.
    assert amounts == [1000.0], f"undo must return the book to the pre-edit state: {amounts}"


def test_the_app_disables_animation(con):
    """The author: tab switching "feel sluggish and losing frame rates".

    Measured 430 ms per switch against 113 ms with animation off, on 2 to 16 queries
    either way — so it was the Tabs underline animation, never the data. On a tool
    whose argument is that recording is fast, a 300 ms flourish per keypress is the
    wrong trade.
    """
    app = DeskApp(con)
    assert app.animation_level == "none"


# --------------------------------------------------------------------------- #
# Focus, which is why none of the row keys worked
#
# Textual leaves focus on ContentTabs after a tab switch, and every row-scoped key
# needs the pane's DataTable to have it. So the whole class of them was dead: `enter`
# arrives as RowSelected and never fired, up/down moved between TABS, and because the
# cursor could therefore never leave (0, 0), `x` deleted from the FIRST row whichever
# one you believed you had selected — a destructive key on an invisible selection.
# --------------------------------------------------------------------------- #
async def test_the_pane_table_has_focus_not_the_tab_bar(app):
    """Every pane whose ROWS are the content focuses them. The dashboard's are not.

    Folding capital in gave the dashboard a table, and focusing it was tried and reverted:
    Textual scrolls a focused widget into view, so the page opened pinned to the bottom with
    `net` off screen and `up` unable to return. `tab` reaches the ledger when a row needs
    editing -- see the reachability test.
    """
    async with app.run_test() as pilot:
        await pilot.pause()
        for key, table_id in (("2", "pos-table"), ("3", "bet-table")):
            await pilot.press(key)
            await pilot.pause()
            assert getattr(app.focused, "id", None) == table_id, (
                f"after {key}: focus is {getattr(app.focused, 'id', None)}")


async def test_down_moves_the_row_cursor_rather_than_the_tab(app, goto):
    """The cursor must move, or `x` can only ever act on the first row.

    RELATIVE, NOT `== 1`. Row 0 is a group heading now, so the pane opens with the cursor
    already on row 1 and pinning the absolute index was asserting the layout rather than
    the behaviour this test is named for.
    """
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        t = app.query_one("#pos-table", DataTable)
        assert t.row_count > 1, "this needs at least two rows to mean anything"
        was = t.cursor_coordinate.row
        await pilot.press("down")
        await pilot.pause()
        assert t.cursor_coordinate.row == was + 1
        assert app.query_one("#tabs", TabbedContent).active == "tab-positions"


async def test_left_and_right_still_change_tabs_with_a_table_focused(app, goto):
    """Focusing the table must not cost the tab keys: DataTable also wants arrows."""
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        tabs = app.query_one("#tabs", TabbedContent)
        await pilot.press("right")
        await pilot.pause()
        assert tabs.active == "tab-bets", "capital folded in, so bets is next along"
        await pilot.press("left")
        await pilot.pause()
        assert tabs.active == "tab-positions"


async def test_opening_the_prompt_keeps_focus_against_a_late_refresh(app, goto):
    """Pane focus runs a frame late, so it must never outrank an open prompt.

    Without the guard, `n` opened the prompt and the table took focus back, so `enter`
    went to the table and the line you had typed was silently dropped.
    """
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await pilot.press("n")
        await pilot.pause()
        await pilot.pause()
        assert app.focused is app.prompt, (
            f"the prompt lost focus to {getattr(app.focused, 'id', None)}")


async def test_escape_closes_the_prompt_and_does_not_crash(app):
    """`esc` used to take the app DOWN.

    InlinePrompt.Cancelled subclassed Input.Changed, whose __init__ requires
    (input, value), and it was constructed with one argument — so every press raised
    TypeError. The footer advertised the key; no test had ever pressed it.
    """
    async with app.run_test() as pilot:
        await pilot.press("a")          # the dashboard's own write, so no walk needed
        await pilot.pause()
        assert app.prompt.is_open
        await pilot.press("escape")
        await pilot.pause()
        assert not app.prompt.is_open
        assert app.is_running, "escape must not kill the app"
        await pilot.press("a")          # and the app still works afterwards
        await pilot.pause()
        assert app.prompt.is_open


def test_restore_refuses_a_table_it_does_not_own(con):
    """restore() interpolates the table NAME, which no bound parameter can carry."""
    with pytest.raises(book.DeskError):
        book.restore(con, "trades; DROP TABLE fills", {"id": 1})
    with pytest.raises(book.DeskError):
        book.delete_row(con, "audit_log", 1)


# --------------------------------------------------------------------------- #
# the dashboard
#
# It is the first screen, so it is the one that must not lie and must not wrap.
# --------------------------------------------------------------------------- #
def _plain(app, selector: str) -> list[str]:
    """A Static's lines with the markup stripped.

    Static.content is the raw markup string it was updated with, so what is asserted
    below is the text a terminal would actually show -- widths included.
    """
    from rich.text import Text
    raw = app.query_one(selector, Static).content
    return [Text.from_markup(ln).plain for ln in str(raw).splitlines()]


def _body(app) -> list[str]:
    return _plain(app, "#dash-body")


async def test_the_dashboard_leads_with_the_scoreboard_then_the_sleeves(app):
    """The ORDER is the design, and two of the three blocks it once had are gone.

    Block 2 was the loss-limit gauge, and block 3 was the gaps list. The wall went because
    nothing here could enforce it; the gaps went because the author was flat about them: "yes i
    think in a good product, gaps shouldnt be there at all."

    What is left leads with the answer -- net, contributed, return -- and follows with the
    arithmetic behind it. Nothing replaced the middle. An empty gauge would have been
    worse than the space.
    """
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        lines = _body(app)

    def at(prefix):
        return next(i for i, ln in enumerate(lines) if ln.startswith(prefix))

    assert at("net") < at("contributed") < at("return") < at("cash") < at("positions")
    for gone in ("loss limit", "gaps", "risk-free"):
        assert not any(gone in ln for ln in lines), f"{gone} is gone from this pane"


@pytest.mark.parametrize("width", [62, 80, 110, 200])
async def test_no_dashboard_line_overflows_its_width(app, width):
    """WRAPPING IS THE BUG THIS GUARDS.

    Textual re-wraps an over-long line at column ZERO, which destroys the label column
    and turned four obligations into what looked like eight. The pane therefore wraps by
    hand, and that only works if it measures the width it actually has -- the scrollbar
    costs two columns, which is exactly what it got wrong first.
    """
    async with app.run_test(size=(width, 40)) as pilot:
        await pilot.pause()
        avail = app.query_one("#dash-body", Static).content_size.width
        assert avail > 0, "the body must have been laid out for this to mean anything"
        long = [ln for ln in _body(app) if len(ln) > avail]
    assert not long, f"at {width} cols, {avail} available: " + "; ".join(long)


async def test_the_dashboard_scrolls_and_still_changes_tabs(app, con):
    """A figure you cannot scroll to is one the tool did not record, as far as you know.

    The page has no fixed length -- the return split, a line per sleeve, a reason per
    withheld figure, and now the whole capital ledger -- so on a short terminal the bottom
    falls off. Scrolling must not cost the tab keys. The size is small on purpose.

    THE SCROLL VIEW HOLDS FOCUS, and briefly the ledger table did instead -- which pinned
    the page to the bottom, hid `net`, and made `up` do nothing because the table consumed it
    to move a cursor already at row 0. So this asserts the two things that broke: the page
    opens at the TOP, and the arrows move it.
    """
    async with app.run_test(size=(62, 12)) as pilot:
        await pilot.pause()
        sc = app.query_one("#dash-scroll", VerticalScroll)
        assert sc.max_scroll_y > 0, "this size should overflow, or the test proves nothing"
        assert getattr(app.focused, "id", None) == "dash-scroll"
        assert sc.scroll_y == 0, "the dashboard must open on the headline, not the ledger"
        await pilot.press("down")
        await pilot.pause()
        assert sc.scroll_y > 0, "down must scroll the dashboard"
        await pilot.press("right")
        await pilot.pause()
        assert app.query_one("#tabs", TabbedContent).active == "tab-positions"


async def test_the_header_carries_the_name_from_the_profile(app, monkeypatch):
    """The author: "inside the profile, you need my name, and my home currency."

    Both, and in the header rather than a settings screen nobody opens. An absent profile
    falls back to "desk" -- config.profile() has a default for every key, so a fresh
    install runs unconfigured.
    """
    monkeypatch.setattr(config, "profile", lambda: config.Profile(
        name="Ada Example", home_currency="CAD"))
    async with app.run_test() as pilot:
        await pilot.pause()
        head = " ".join(_plain(app, "#dash-head"))
    assert "Ada Example" in head and "CAD" in head, head


async def test_the_fetch_runs_off_the_ui_thread_and_reports_a_failure(app, monkeypatch):
    """The fetch is on a 30-second timer, so it must not be on the UI thread.

    DRIVEN THROUGH `_fetch` NOW RATHER THAN THROUGH `m`, because there is no `m`: the author removed
    it on 2026-09-26 -- "remove the refresh button. we dont need it anymore" -- once the desk
    fetched on open, on a timer, and on a change of holdings. The worker is what mattered
    about that test, not the keystroke.

    The failure half is asserted because a silent failed poll would leave the header saying
    "marked 3 days ago" with nothing to say the desk has been trying and failing to fix that
    every thirty seconds since.
    """
    calls = []
    monkeypatch.setattr(type(app), "_run_prices",
                        lambda self: (calls.append("ok") or ("  2/2 symbols\n", None)))
    async with app.run_test() as pilot:
        app._fetch(announce=False)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert calls == ["ok"]
        assert app.fetching is False, "the flag must be cleared on the UI thread"

        monkeypatch.setattr(type(app), "_run_prices",
                            lambda self: ("", "URLError: timed out"))
        app._fetch(announce=False)
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.fetching is False, "a failure must not leave the desk saying fetching"


def test_there_is_no_refresh_key_and_no_handler_for_one():
    """The author: "remove the refresh button. we dont need it anymore."

    BOTH HALVES, because handlers are resolved by NAME -- `handler_for` does
    getattr(self, f"app_{action}") -- so an orphaned `app_refresh` would be a live handler for
    an action nothing dispatches, and the footer asks `handler_for` whether a key is real.
    Leaving one without the other is how the two drift apart.
    """
    from desk.tui.app import DeskApp

    assert not any(k.key == "m" for k in keymap.KEYMAP), "the `m` key is back"
    assert not any(k.action == "refresh" for k in keymap.KEYMAP)
    assert not hasattr(DeskApp, "app_refresh"), "a handler survives its key"


async def test_a_new_holding_fetches_at_once_rather_than_waiting_for_the_timer(app, con,
                                                                              monkeypatch):
    """The author: "when i have new positions or close positions. auto refresh prices."

    A TICKER THE BOOK HAS NEVER HELD HAS NO MARK AT ALL, so every figure on its row is an em
    dash until a poll comes round. Thirty seconds of not knowing what you just bought is worth
    a round trip.

    ON THE SET OF HELD TICKERS, not on every write, and that is load-bearing: the worker is
    `exclusive`, so starting one CANCELS any in flight. Firing per fill would mean four quick
    fills cancel three fetches and complete none.

    DRIVEN THROUGH book.add_fill + refresh_all RATHER THAN THE PROMPT. Every write route ends
    at refresh_all, which is where the hook lives, and going through `n` made this test depend
    on prompt plumbing that has its own tests -- when it first failed, the mechanism was fine
    and the keystrokes were not.
    """
    fetches = []
    monkeypatch.setattr(type(app), "_run_prices",
                        lambda self: (fetches.append(1) or ("  ok\n", None)))
    app.POLL_DISABLED = False
    async with app.run_test(size=(126, 46)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        before = len(fetches)

        # ADDING TO SOMETHING ALREADY HELD leaves the set alone, so it must not refetch.
        held = book.positions(con)[0]
        book.add_fill(con, ticker=held.ticker, shares=1, price=10.0,
                      bet_id=held.bet_id, on_date="2026-09-26", account=held.account)
        app.refresh_all()
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert len(fetches) == before, "adding to an existing holding refetched"

        # A TICKER THE BOOK HAS NEVER HELD changes the set. LEVR is registered in the fixture
        # but not open, which is exactly the case with no mark to show.
        assert not any(x.ticker == "LEVR" for x in book.positions(con))
        book.add_fill(con, ticker="LEVR", shares=5, price=11.0, bet_id=None,
                      on_date="2026-09-26", account="tfsa")
        app.refresh_all()
        await pilot.pause()
        await app.workers.wait_for_complete()
    assert len(fetches) == before + 1, (
        f"a brand new holding should fetch exactly once, got {len(fetches) - before}")


async def test_the_first_draw_does_not_cancel_the_startup_fetch(app, monkeypatch):
    """`on_mount` kicks a fetch; the first `refresh_all` must not start a second one.

    The worker is `exclusive`, so a second start CANCELS the first -- the one case where
    noticing a "change" on the very first look would break the thing it is meant to help.
    `_held` is None until looked at, which is why it is distinct from the empty set.
    """
    fetches = []
    monkeypatch.setattr(type(app), "_run_prices",
                        lambda self: (fetches.append(1) or ("  ok\n", None)))
    app.POLL_DISABLED = False
    async with app.run_test() as pilot:
        await pilot.pause()
        app.refresh_all()
        app.refresh_all()
        await pilot.pause()
        await app.workers.wait_for_complete()
    assert len(fetches) <= 1, f"the first draws started {len(fetches)} fetches"


def test_the_poll_is_thirty_seconds():
    """Halved on 2026-09-26, and affordable only because the fetch no longer holds a write
    lock for its whole run -- a shorter interval on the old code would have made the desk
    unwritable more of the time rather than merely busier."""
    from desk.tui.app import DeskApp

    assert DeskApp.POLL_SECONDS == 30.0


async def test_the_dashboard_header_says_whose_and_how_fresh_and_grants_nothing(app):
    """It says who, in what currency, and how old the prices are. It rules on nothing.

    The header went "may i trade?" -> "N blocking" -> "N gap(s) in the record" -> this.
    Every one of the first three was the tool answering a question it has no standing to
    answer, or handing over a list of chores; the fourth is the two facts the numbers
    below cannot state about themselves.
    """
    async with app.run_test() as pilot:
        await pilot.pause()
        head = " ".join(_plain(app, "#dash-head"))
    assert "CAD" in head, head
    assert "marked" in head or "no prices yet" in head, head
    for phrase in ("blocking", "breached", "gap", "may i trade", "clear to trade",
                   "you may"):
        assert phrase not in head.lower(), f"the dashboard passed judgement: {head!r}"


# --------------------------------------------------------------------------- #
# What the adversarial audit found in the code merged one hour earlier
#
# Every one of these was PROVEN by execution before being fixed, three of the four
# against a copy of the real book. They share a shape: the failure is silent, and two
# of them appear at the exact moment the user does what the desk told them to do.
# --------------------------------------------------------------------------- #
async def test_a_rejected_edit_leaves_the_book_exactly_as_it_was(app, con, goto):
    """A REJECTED LINE MUST CHANGE NOTHING. It used to destroy a row.

    An edit is delete-then-add and the connection is autocommit, so when add_fill hit one
    of the book's own walls the delete had already committed. Editing the 40-share USDCO
    position to sell 80 was refused with "USDCO holds 60; selling 80 would go short" -- a
    quantity that existed only because the -20 trim had just been deleted -- and the book
    was left holding a position that was never held, with no toast and no repaint.
    """
    held = book.positions(con)
    usdco = next(p for p in held if p.ticker == "USDCO")
    before_qty = usdco.qty
    before_fills = con.execute(
        "SELECT COUNT(*) FROM fills f JOIN trades t ON t.id=f.trade_id "
        "WHERE t.ticker='USDCO'").fetchone()[0]
    assert before_fills > 1, "needs more than one fill for the trap to exist"

    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        # Put the cursor on the USDCO row.
        t = app.query_one("#pos-table", DataTable)
        for row in range(t.row_count):
            if t.coordinate_to_cell_key((row, 0)).row_key.value == usdco.trade_id:
                t.cursor_coordinate = (row, 0)
                break
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.is_open, "enter must arm the edit"
        app.prompt.value = f"USDCO -{before_qty + 40:g} 110.00 !growth @2026-06-01 /tfsa"
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.is_open, "the wall must reject it and keep the prompt open"
        assert "short" in app.prompt.error, app.prompt.error

    after = next(p for p in book.positions(con) if p.ticker == "USDCO")
    assert after.qty == before_qty, (
        f"a rejected edit changed the position: {before_qty} -> {after.qty}")
    assert con.execute(
        "SELECT COUNT(*) FROM fills f JOIN trades t ON t.id=f.trade_id "
        "WHERE t.ticker='USDCO'").fetchone()[0] == before_fills


async def test_a_rejected_edit_stays_armed_so_the_correction_replaces(app, con, goto):
    """After a rejection the edit must still be armed, or the fix becomes a duplicate.

    _editing was cleared BEFORE the delete, so the corrected line the prompt invites was
    handled as a fresh ADD: superseded_by was never called, and a later `u` restored the
    original on top of the replacement -- the exact regression superseded_by exists to
    prevent.
    """
    usdco = next(p for p in book.positions(con) if p.ticker == "USDCO")
    before_qty, before_fills = usdco.qty, con.execute(
        "SELECT COUNT(*) FROM fills f JOIN trades t ON t.id=f.trade_id "
        "WHERE t.ticker='USDCO'").fetchone()[0]

    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        t = app.query_one("#pos-table", DataTable)
        for row in range(t.row_count):
            if t.coordinate_to_cell_key((row, 0)).row_key.value == usdco.trade_id:
                t.cursor_coordinate = (row, 0)
                break
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        original = app.prompt.value
        app.prompt.value = f"USDCO -{before_qty + 40:g} 110.00 !growth @2026-06-01 /tfsa"
        await pilot.press("enter")           # refused
        await pilot.pause()
        assert app.prompt.is_open
        app.prompt.value = original          # correct it back
        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, app.prompt.error
        # The fill count must be unchanged: one row replaced, not one added.
        assert con.execute(
            "SELECT COUNT(*) FROM fills f JOIN trades t ON t.id=f.trade_id "
            "WHERE t.ticker='USDCO'").fetchone()[0] == before_fills
        await pilot.press("u")
        await pilot.pause()

    after = next(p for p in book.positions(con) if p.ticker == "USDCO")
    assert after.qty == before_qty, (
        f"undo after a corrected edit must land back on {before_qty}, got {after.qty}")


async def test_an_empty_submit_disarms_a_pending_edit(app, blank, goto):
    """An empty submit is a cancel, so it must disarm the edit exactly as escape does.

    It did not, and the result was silent: `enter` armed the edit, clearing the line and
    pressing enter closed the prompt with the edit STILL ARMED, and the next `c` deleted
    the row that had been under the cursor and wrote a different one in its place.
    """
    book.add_flow(blank, amount=1000.0, on_date="2026-02-01")
    async with app.run_test() as pilot:
        await goto(pilot, "dashboard")
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.is_open
        app.prompt.value = ""
        await pilot.press("enter")            # an empty submit: a cancel
        await pilot.pause()
        assert not app.prompt.is_open
        await pilot.press("c")                # a CREATE, not a replace
        await pilot.pause()
        app.prompt.value = "2500 @2026-03-01"
        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, app.prompt.error
    amounts = sorted(r[0] for r in blank.execute("SELECT amount FROM flows"))
    assert amounts == [1000.0, 2500.0], (
        f"the abandoned edit leaked into the next create: {amounts}")


# --------------------------------------------------------------------------- #
# A date that only LOOKS like a date
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["2026-09-31", "2026-02-30", "2026-13-45", "2026-00-10"])
def test_an_impossible_date_is_refused_before_it_reaches_the_book(bad):
    """The SHAPE is not the date, and the shape was all that was checked.

    All four of these passed the grammar and the schema's GLOB, so the row was committed.
    When such a date ends up the earliest sleeve read, balance() and obligations() raise
    "day is out of range for month" on every call -- and the dashboard calls both on
    mount, so the desk will not open. There is no delete key on that tab and undo only
    restores what a delete stacked, so the book cannot be repaired from inside the tool.
    """
    with pytest.raises(parse.ParseError, match="not a real date"):
        parse.parse_fill(f"USDCO 1 10.00 !growth @{bad}", now=NOW)
    with pytest.raises(parse.ParseError, match="not a real date"):
        parse.parse_flow(f"1000 @{bad}", now=NOW)
    with pytest.raises(parse.ParseError, match="not a real date"):
        parse.parse_adjustment(f"-4.95 @{bad}", now=NOW)


def test_a_real_date_still_passes():
    """The guard must not reject the dates that exist -- including a leap day."""
    for good in ("2026-09-30", "2024-02-29", "2026-01-01", "2026-12-31"):
        assert parse.parse_flow(f"1000 @{good}", now=NOW).on_date == good


def test_the_parser_is_the_only_guard_on_an_impossible_date(con):
    """DOCUMENTS WHERE THE GUARD LIVES, so the next writer knows it must validate.

    account_snapshots.as_of is checked by a GLOB on the shape, which an impossible date
    satisfies. Any future code path that writes a date without going through parse.py
    reopens this, so the gap is asserted rather than assumed closed.
    """
    con.execute("INSERT OR REPLACE INTO account_snapshots"
                "(as_of, account_id, total, risk_free, source, notes) "
                "VALUES ('2026-09-31', 'tfsa', 1.0, NULL, 'statement', NULL)")
    assert con.execute("SELECT COUNT(*) FROM account_snapshots "
                       "WHERE as_of='2026-09-31'").fetchone()[0] == 1, (
        "if the schema now rejects this, delete this test and say so")


# --------------------------------------------------------------------------- #
# The wedge: an open prompt must never be able to trap the app
# --------------------------------------------------------------------------- #
async def test_tab_does_not_move_focus_off_an_open_prompt(app, goto):
    """Textual's tab is focus-next, and moving off the prompt used to trap the app.

    With focus gone the prompt's own escape handler stopped firing, and action_dispatch
    refused every key because the prompt was still open -- so escape, q and the digits
    were all dead and only Ctrl+C ended the session.
    """
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await pilot.press("n")
        await pilot.pause()
        assert app.focused is app.prompt
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is app.prompt, (
            f"tab moved focus to {getattr(app.focused, 'id', None)}")
        await pilot.press("escape")
        await pilot.pause()
        assert not app.prompt.is_open


async def test_escape_still_closes_a_prompt_that_has_lost_focus(app, goto):
    """The RECOVERY, independent of what caused focus to go.

    action_dispatch used to refuse every key while the prompt was open, which is right
    for `n` and wrong for the two keys whose whole job is getting out.
    """
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await pilot.press("n")
        await pilot.pause()
        app.screen.set_focus(None)          # however it happened
        await pilot.pause()
        assert app.prompt.is_open and app.focused is None
        await pilot.press("escape")
        await pilot.pause()
        assert not app.prompt.is_open, "escape must always be able to close the prompt"
        await pilot.press("1")
        await pilot.pause()
        assert app.scope == "dashboard", "and the app must be usable again afterwards"


async def test_n_is_still_refused_while_the_prompt_is_open(app, goto):
    """The exception is only for the keys that ESCAPE a prompt, not a hole in the rule."""
    async with app.run_test() as pilot:
        await goto(pilot, "dashboard")
        await pilot.press("c")
        await pilot.pause()
        label = app.prompt.label
        await pilot.press("3")              # a tab switch must not fire
        await pilot.pause()
        assert app.scope == "dashboard" and app.prompt.label == label


# --------------------------------------------------------------------------- #
# The prefill must not quietly change the number it prefills
# --------------------------------------------------------------------------- #
MONEY = [0.01, 0.1, 0.123456789, 50.055, 220.89, 1234.5, 45000.0, 100000.0,
         145002.5, 1234567.89, 12345678.9, 1e7]


@pytest.mark.parametrize("amount", MONEY)
def test_a_flow_amount_survives_the_edit_prefill_exactly(amount):
    """render used `:g` -- SIX significant digits -- and the loss was silent.

    The whole point of a prefill is that you press enter without retyping it, so any
    rounding it introduces is a write the user never made:

        145,002.50   -> '145003'       -> 145,003.00   (50 cents invented)
        1,234,567.89 -> '1.23457e+06'  -> 1,234,570.00 (2.11 gone)

    Parameterised over the shapes that actually break it -- more than six figures, and
    fractional share counts, which many brokers hand out routinely.
    """
    line = parse.render_flow({"amount": amount, "on_date": "2026-09-01",
                              "account": "tfsa", "note": None})
    assert parse.parse_flow(line, now=NOW).amount == amount, line
    assert "e+" not in line, f"scientific notation in a money prompt: {line!r}"


@pytest.mark.parametrize("price", MONEY)
def test_a_fill_price_and_a_fractional_share_count_survive_exactly(price):
    row = {"ticker": "USDCO", "shares": 0.123456789, "price": price,
           "bet_id": "growth", "on_date": "2026-09-01",
           "fee": 4.95, "account": None, "note": None}
    back = parse.parse_fill(parse.render_fill(row), now=NOW)
    assert back.price == price
    assert back.shares == 0.123456789, "a fractional share count must not be rounded"
    assert back.fee == 4.95


def test_a_whole_number_still_reads_as_a_whole_number():
    """The fix must not make the prompt uglier than it was: 45000, not 45000.0."""
    line = parse.render_flow({"amount": 45000.0, "on_date": "2026-09-01",
                              "account": None, "note": None})
    assert line.startswith("45000 "), line


# --------------------------------------------------------------------------- #
# A number that is not a number
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("token", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_a_non_finite_number_is_refused(token):
    """nan slipped past the guard whose whole job was to require a positive price.

    float() accepts these, and every comparison with nan is False -- so `price > 0` did
    not stop it, and the row was written with a price of nan, poisoning every figure
    derived from it. inf poisons the totals instead.
    """
    with pytest.raises(parse.ParseError, match="finite"):
        parse.parse_fill(f"USDCO 1 {token} !growth", now=NOW)
    with pytest.raises(parse.ParseError, match="finite"):
        parse.parse_fill(f"USDCO {token} 10 !growth", now=NOW)
    with pytest.raises(parse.ParseError, match="finite"):
        parse.parse_flow(f"{token} @2026-09-01", now=NOW)


@pytest.mark.parametrize("token,why", [
    ("1,50", "a decimal comma became 150.0 -- a hundredfold price"),
    ("1,5", "same shape, fewer digits"),
    ("1,2345", "four digits after the comma is not a thousands group"),
    ("12,34,56", "not thousands groups either"),
])
def test_an_ambiguous_comma_is_refused_rather_than_stripped(token, why):
    """Every comma used to be stripped, so a decimal comma multiplied by 100."""
    with pytest.raises(parse.ParseError, match="thousands separator"):
        parse.parse_fill(f"USDCO 1 {token} !growth", now=NOW)


@pytest.mark.parametrize("token,value", [
    ("1,234.56", 1234.56), ("12,345,678.90", 12345678.90),
    ("1234.56", 1234.56), ("999", 999.0), ("1,000", 1000.0),
])
def test_a_real_thousands_separator_still_parses(token, value):
    """The guard must not reject the way a statement actually prints a number."""
    assert parse.parse_fill(f"USDCO 1 {token} !growth", now=NOW).price == value


# --------------------------------------------------------------------------- #
# Completion
#
# The author: "you dont have tab completions it seems" -- while typing a ticker the registry had
# never heard of, so the miss case matters as much as the hit.
# --------------------------------------------------------------------------- #
TICKERS = frozenset({"USDCO", "CADCO", "PARK", "LEVR"})
BETS = frozenset({"growth", "parking"})
ACCOUNTS = frozenset({"tfsa", "rrsp"})


def _c(raw, caret=None, label="fill"):
    return parse.complete(raw, len(raw) if caret is None else caret, label,
                          tickers=TICKERS, bets=BETS, accounts=ACCOUNTS)


def test_the_sigil_decides_the_field_not_the_position():
    """`!` is a bet and `/` an account wherever they appear — the grammar is order-free."""
    assert _c("USDCO 1 10 !gro").field == "bet"
    assert _c("USDCO 1 10 /tf").field == "account"
    assert _c("/tf", caret=3).field == "account", "even in the first slot"
    assert _c("USDCO 1 10 @tod").field == "when"


def test_the_ticker_slot_is_the_first_BARE_word_not_the_first_character():
    """The grammar is order-free, so `/tfsa USDCO 10 65` is a valid fill.

    Deciding the ticker slot by character position made the ticker uncompletable there.
    Counting BARE tokens is the rule that matches how the parser itself reads the line.
    """
    assert _c("/tfsa USD").field == "ticker"
    assert _c("!growth USD").field == "ticker"
    assert _c("/tfsa USDCO 1").field == "", "still only the first bare slot"


def test_nothing_completes_inside_a_note():
    """`~` absorbs the rest of the line, so every word after it is prose."""
    assert _c("USDCO 1 10 ~bought USD").field == ""
    assert _c("USDCO 1 10 ~why /tf").field == "", "even a sigil is prose after ~"


def test_a_bare_word_is_a_ticker_only_in_the_first_slot():
    """Every other bare slot in a fill is a number, so offering tickers there is noise."""
    assert _c("USD").field == "ticker"
    assert _c("USDCO 1").field == "", "the quantity slot must offer nothing"
    assert _c("USDCO 1 1").field == "", "nor the price slot"


def test_a_prose_or_number_sigil_offers_nothing():
    """`~` absorbs prose and `=` is a number. Offering the wrong vocabulary is worse."""
    assert _c("USDCO 1 10 ~some no").field == ""
    assert _c("USDCO 1 10 =4.9").field == ""


def test_one_match_completes_and_several_give_the_shared_prefix():
    one = _c("USD")
    assert one.matches == ("USDCO",) and one.common == "USDCO"
    several = _c("USDCO 1 10 !")
    assert several.matches == ("growth", "parking")
    assert several.common == "", "nothing is shared, so nothing may be inserted"


def test_the_shared_prefix_is_inserted_when_there_is_one():
    c = parse.complete("PA", 2, "fill", tickers=frozenset({"PARK", "PARKB", "PARKC"}))
    assert c.matches == ("PARK", "PARKB", "PARKC")
    assert c.common == "PARK", "PARK is shared by all three"


def test_completion_is_case_insensitive_but_inserts_the_recorded_spelling():
    c = _c("usdc")
    assert c.matches == ("USDCO",)
    assert c.replaced("usdc", c.common) == ("USDCO", 5)


def test_completion_replaces_only_the_word_under_the_caret():
    raw = "USDCO 1 10 !gro /tfsa"
    caret = raw.index(" /")            # end of "!gro"
    c = parse.complete(raw, caret, "fill", bets=BETS)
    new, pos = c.replaced(raw, "growth")
    assert new == "USDCO 1 10 !growth /tfsa", new
    assert new[pos] == " ", "the caret lands right after the inserted word"


def test_a_miss_reports_the_vocabulary_rather_than_nothing():
    c = _c("ZZZ")
    assert c.field == "ticker" and c.matches == ()
    assert c.prefix == "ZZZ"


async def test_tab_completes_a_ticker_a_bet_and_an_account(app, con, goto):
    """Driven through the pilot, because the wiring is where this can break."""
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await pilot.press("n")
        await pilot.pause()
        app.prompt.value = "US"
        app.prompt.cursor_position = 2
        await pilot.press("tab")
        await pilot.pause()
        assert app.prompt.value == "USDCO", app.prompt.value
        app.prompt.value += " 10 65 !gro"
        app.prompt.cursor_position = len(app.prompt.value)
        await pilot.press("tab")
        await pilot.pause()
        assert app.prompt.value == "USDCO 10 65 !growth", app.prompt.value
        app.prompt.value += " /tf"
        app.prompt.cursor_position = len(app.prompt.value)
        await pilot.press("tab")
        await pilot.pause()
        assert app.prompt.value == "USDCO 10 65 !growth /tfsa", app.prompt.value


async def test_tab_on_a_miss_names_what_is_known(app, goto):
    """A dead key reads as a broken key, so a miss has to say something."""
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await pilot.press("n")
        await pilot.pause()
        app.prompt.value = "ZZZZ"
        app.prompt.cursor_position = 4
        await pilot.press("tab")
        await pilot.pause()
        assert app.prompt.value == "ZZZZ", "a miss must not alter the line"
        sub = str(app.prompt.border_subtitle)
        assert "no ticker" in sub, sub
        assert "USDCO" in sub, f"it must name what IS known: {sub}"


async def test_tab_never_moves_focus_off_the_prompt(app, goto):
    """Completion took over the key that used to wedge the app."""
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is app.prompt
        assert app.prompt.is_open


# --------------------------------------------------------------------------- #
# Registering an instrument from the TUI
#
# The registry is a wall on purpose, but the only writer was a CLI subcommand -- so
# hitting it inside the app was a dead end. The author hit it with ZGLD.
# --------------------------------------------------------------------------- #
def test_no_message_anywhere_names_a_key_that_does_not_exist():
    """A message telling you to press a key that does nothing is worse than no message.

    IT CAUGHT A REAL ONE. The registry refusal said "press i to register it" and survived `i`
    being deleted, because nothing passed `known_tickers` any more so the branch was dead --
    dead code that lies. Widened from that one message to every string in the two modules that
    talk to the user, which is what makes it a guard rather than a regression test.
    """
    import io
    import re
    import tokenize

    from desk.tui import panes

    bound = {k.key for k in keymap.KEYMAP}
    offenders = []
    for mod in (parse, book, panes):
        path = pathlib.Path(mod.__file__)
        # STRING LITERALS ONLY, never comments, and never a multi-line one. Scanning raw
        # source flagged parse.py's own note ABOUT the message it had just deleted -- prose
        # describing a dead message is not a dead message, and a guard that cannot tell the
        # difference is a guard someone switches off. Multi-line literals are docstrings here,
        # which are documentation rather than something a user is shown.
        for tok in tokenize.generate_tokens(io.StringIO(path.read_text()).readline):
            if tok.type != tokenize.STRING or "\n" in tok.string:
                continue
            for m in re.finditer(r"press ([a-zA-Z0-9])\b", tok.string):
                if m.group(1) not in bound:
                    offenders.append(f"{path.name}:{tok.start[0]} "
                                     f"names unbound key {m.group(1)!r}")
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize("line,match", [
    ("ZGLD", "need ticker, currency and kind"),
    ("ZGLD CAD", "need ticker, currency and kind"),
    ("ZGLD GBP etf", "not a currency this book can value"),
    ("ZGLD CAD gold", "not a kind"),
    # A FOURTH bare word is now the leverage factor, so the count that is too many is five.
    ("ZGLD CAD etf BMO Gold", "then sigils"),
    ("ZGLD CAD etf BMO", "leverage factor must be a number"),
    ("SOXL USD leveraged-etf", "needs its factor"),
    ("ZGLD CAD etf 3", "only belongs on a leveraged-etf"),
])
# --------------------------------------------------------------------------- #
# Closing the loop: the tool can now write what its dashboard demands
# --------------------------------------------------------------------------- #
def _leg_needing_a_stop(con):
    """An open leg with no recorded stop.

    READ FROM `trades` DIRECTLY, because `v_trade_stop_check` went in v14 with the curve --
    its `verdict` column compared a recorded stop against the curve's requirement, which is
    exactly the grading this tool no longer does. "Needs a stop" is now simply "has none".
    """
    r = con.execute("SELECT id FROM trades WHERE status='open' "
                    "AND loss_limit_pct IS NULL ORDER BY id LIMIT 1").fetchone()
    return r[0] if r else None


async def _point_at(pilot, app, table_id, key):
    t = app.query_one(table_id, DataTable)
    for row in range(t.row_count):
        if t.coordinate_to_cell_key((row, 0)).row_key.value == key:
            t.cursor_coordinate = (row, 0)
            await pilot.pause()
            return t
    raise AssertionError(f"{key} is not in {table_id}")


async def test_s_records_a_stop_and_prefills_what_is_already_there(app, con, goto):
    """It used to prefill the CURVE's requirement and grade what you typed against it.

    There is no curve now. A stop is still a fact worth recording -- where you decided to
    get out -- so the key stays and prefills what is recorded, like every other edit.
    """
    tid = con.execute("SELECT id FROM trades WHERE status='open' LIMIT 1").fetchone()[0]
    con.execute("UPDATE trades SET loss_limit_pct=NULL, loss_limit_basis=NULL WHERE id=?",
                (tid,))
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await _point_at(pilot, app, "#pos-table", tid)
        await pilot.press("s")
        await pilot.pause()
        assert app.prompt.is_open and app.prompt.label == "stop"
        assert app.prompt.value == "", "nothing recorded yet, so nothing to prefill"
        app.prompt.value = "25% =58.40"
        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, app.prompt.error
        # And now it prefills what was recorded.
        await pilot.press("s")
        await pilot.pause()
        assert app.prompt.value == "25% =58.4", app.prompt.value
        await pilot.press("escape")
        await pilot.pause()
    row = con.execute("SELECT loss_limit_pct, loss_limit_basis, stop FROM trades "
                      "WHERE id=?", (tid,)).fetchone()
    assert (row["loss_limit_pct"], row["loss_limit_basis"]) == (25.0, "gross-deployed")
    assert row["stop"] == 58.40


async def test_u_reverts_a_stop_because_an_update_undo_is_not_an_insert(app, con, goto):
    """A trades pre-image put through the INSERT path would fail on the primary key.

    That is undo appearing to work and doing nothing, which this project shipped once
    already, so the update mode exists and this pins it.
    """
    tid = con.execute("SELECT id FROM trades WHERE status='open' LIMIT 1").fetchone()[0]
    con.execute("UPDATE trades SET loss_limit_pct=NULL, loss_limit_basis=NULL WHERE id=?",
                (tid,))
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await _point_at(pilot, app, "#pos-table", tid)
        await pilot.press("s")
        await pilot.pause()
        app.prompt.value = "30"
        await pilot.press("enter")
        await pilot.pause()
        assert con.execute("SELECT loss_limit_pct FROM trades WHERE id=?",
                           (tid,)).fetchone()[0] == 30.0
        await pilot.press("u")
        await pilot.pause()
    row = con.execute("SELECT loss_limit_pct, loss_limit_basis, loss_limit_source "
                      "FROM trades WHERE id=?", (tid,)).fetchone()
    assert tuple(row) == (None, None, None), f"undo left {dict(row)}"


# test_r_sets_a_review_date WAS HERE. `r` and bets.review_date went in v15: the key
# wrote a date that no screen ever showed, so 0 of 6 of the author's bets had one.


async def test_re_baselining_an_allocation_demands_a_reason_and_records_the_old(app, con,
                                                                              goto):
    """The schema calls re-baselining "a deliberate, recorded act".

    Every leg's share of the bet is measured against this number, so changing it silently
    would move every required stop under the bet at once and leave no trace of why.
    """
    bet = con.execute("SELECT id, allocation_base FROM bets "
                      "WHERE allocation_base IS NOT NULL LIMIT 1").fetchone()
    assert bet, "the fixture needs an allocated bet"
    was = bet["allocation_base"]
    async with app.run_test() as pilot:
        await goto(pilot, "bets")
        await _point_at(pilot, app, "#bet-table", bet["id"])
        await pilot.press("a")
        await pilot.pause()
        assert parse._num(was) in app.prompt.value, app.prompt.value
        app.prompt.value = parse._num(was + 1000)
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.is_open, "a silent re-baseline must be refused"
        assert "reason" in app.prompt.error, app.prompt.error
        app.prompt.value = f"{parse._num(was + 1000)} ~the sleeve grew"
        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, app.prompt.error
    row = con.execute("SELECT allocation_base, allocation_rebased_from, "
                      "allocation_rebase_why FROM bets WHERE id=?",
                      (bet["id"],)).fetchone()
    assert row["allocation_base"] == was + 1000
    assert row["allocation_rebased_from"] == was, "the prior figure must survive"
    assert row["allocation_rebase_why"] == "the sleeve grew"


async def test_setting_an_allocation_for_the_first_time_needs_no_reason(app, con, goto):
    """There is nothing to explain when there was no prior figure."""
    con.execute("UPDATE bets SET allocation_base=NULL, allocation_ccy=NULL, "
                "allocation_set_on=NULL WHERE id='growth'")
    async with app.run_test() as pilot:
        await goto(pilot, "bets")
        await _point_at(pilot, app, "#bet-table", "growth")
        await pilot.press("a")
        await pilot.pause()
        assert app.prompt.value == "", "nothing to prefill"
        app.prompt.value = "8000"
        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, app.prompt.error
    row = con.execute("SELECT allocation_base, allocation_ccy, allocation_set_on "
                      "FROM bets WHERE id='growth'").fetchone()
    assert row["allocation_base"] == 8000.0
    assert row["allocation_ccy"] and row["allocation_set_on"], (
        "the schema requires both alongside an allocation")


# --------------------------------------------------------------------------- #
# The cursor must survive a rebuild
# --------------------------------------------------------------------------- #
async def test_the_row_cursor_survives_a_write(app, con, goto):
    """DataTable.clear() resets the cursor to (0, 0), and every write calls reload().

    So the SECOND action in a row landed on whatever was at the top: setting a stop and
    then setting it again silently wrote to a DIFFERENT leg. Found by driving the new key
    twice in one session. It is the same defect as `x` deleting row zero, by another route,
    which is why the fix is in the base Pane and this test is about the base behaviour.
    """
    tid = _leg_needing_a_stop(con)
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        t = await _point_at(pilot, app, "#pos-table", tid)
        row_before = t.cursor_coordinate.row
        assert row_before != 0 or t.row_count == 1, (
            "point this test at a row that is not already the first one")
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value == tid, (
            "the write moved the cursor off the row the user had selected")
        # And the second press must therefore act on the SAME leg.
        await pilot.press("s")
        await pilot.pause()
        app.prompt.value = "95"
        await pilot.press("enter")
        await pilot.pause()
    # WAS: assert the verdict starts with "LOOSER" -- i.e. that the 95% stop was looser than
    # the curve required. The curve went in v14, so what this test is really about is that the
    # SECOND `s` landed on the leg under the cursor rather than on row zero: assert the stop
    # itself, which is the fact, not a grade of it.
    assert con.execute("SELECT loss_limit_pct FROM trades WHERE id=?",
                       (tid,)).fetchone()[0] == 95.0, (
        "the second stop landed on a different leg")


async def test_an_abandoned_stop_prompt_does_not_leak_into_the_next_one(app, con, goto):
    """`_stopping` is the same shape as `_editing`, which leaked exactly this way."""
    tid = _leg_needing_a_stop(con)
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        await _point_at(pilot, app, "#pos-table", tid)
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        pane = app.query_one("#positions")
        assert pane._stopping is None, "the armed leg survived the cancel"
        for attr in pane._ARMED:
            assert getattr(pane, attr) is None, f"{attr} survived the cancel"
    assert con.execute("SELECT loss_limit_pct FROM trades WHERE id=?",
                       (tid,)).fetchone()[0] is None


# --------------------------------------------------------------------------- #
# `a`: opening a sleeve
#
# This section was headed "desk asof: the clock the rules are judged against" and held
# the two `k` tests. Both went with the key -- the clock chose between rule versions,
# and there are no rules left to choose between.
# --------------------------------------------------------------------------- #
async def test_a_opens_a_sleeve_so_a_fresh_book_is_not_stranded(app, con, goto):
    """Was `desk account add`, the one bootstrap a book cannot skip.

    Every write lands in an account, so a book with none can record nothing -- removing
    the CLI without this key would have made a fresh book unusable through the tool.
    """
    assert "fhsa" not in book.known_accounts(con)
    async with app.run_test() as pilot:
        await goto(pilot, "dashboard")
        await pilot.press("a")
        await pilot.pause()
        assert app.prompt.label == "account"
        app.prompt.value = "fhsa ~First Home Savings"
        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, app.prompt.error
    row = con.execute("SELECT name, is_default FROM accounts WHERE id='fhsa'").fetchone()
    assert tuple(row) == ("First Home Savings", 0)
    assert "fhsa" in book.known_accounts(con)


async def test_a_refuses_an_id_that_is_not_a_slug(app, con, goto):
    """The id is typed again after `/` on every fill, so it has to be retypeable.

    This used to assert a refused KIND. v11 has no kinds -- an account is a name -- so what
    is left to validate is the one field that still matters.
    """
    async with app.run_test() as pilot:
        await goto(pilot, "dashboard")
        await pilot.press("a")
        await pilot.pause()
        app.prompt.value = "My_TFSA ~nope"
        await pilot.press("enter")
        await pilot.pause()
        assert app.prompt.is_open
        assert "not an account id" in app.prompt.error, app.prompt.error
    assert "my" not in book.known_accounts(con)


async def test_the_first_account_can_be_made_the_default(app, blank, goto):
    async with app.run_test() as pilot:
        await goto(pilot, "dashboard")
        await pilot.press("a")
        await pilot.pause()
        app.prompt.value = "nonreg ~Non-registered default"
        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, app.prompt.error
    assert blank.execute("SELECT id FROM accounts WHERE is_default=1"
                         ).fetchone()[0] == "nonreg", "at most one default, and it moved"


# --------------------------------------------------------------------------- #
# Changing a bet's status
#
# The author: "how do i change the bet status?" -- you could not. ai-capex-semis was closed and
# 2026-q4-reverse was made live by hand-written SQL.
# --------------------------------------------------------------------------- #
def _flatten(con, bet):
    """Remove the bet's open legs outright.

    `UPDATE trades SET status='closed'` is refused by closed_trade_holds_nothing, and
    rightly: a closed trade holding shares is the contradiction that trigger exists for.
    So the legs are deleted rather than relabelled.
    """
    ids = [r[0] for r in con.execute(
        "SELECT id FROM trades WHERE bet_id=? AND status='open'", (bet,))]
    for tid in ids:
        con.execute("DELETE FROM fills WHERE trade_id=?", (tid,))
        con.execute("DELETE FROM trades WHERE id=?", (tid,))
    return ids


async def _status(pilot, app, bet, line):
    await _point_at(pilot, app, "#bet-table", bet)
    await pilot.press("s")
    await pilot.pause()
    assert app.prompt.label == "bet status", app.prompt.label
    app.prompt.value = line
    await pilot.press("enter")
    await pilot.pause()


async def test_s_on_bets_moves_the_status_and_dates_it(app, con, goto):
    bet = con.execute("SELECT id FROM bets WHERE status='live' LIMIT 1").fetchone()
    if not bet:
        pytest.skip("the fixture has no live bet")
    _flatten(con, bet[0])
    async with app.run_test() as pilot:
        await goto(pilot, "bets")
        await _status(pilot, app, bet[0], "closed @2026-09-05 ~the thesis played out")
        assert not app.prompt.is_open, app.prompt.error
    row = con.execute("SELECT status, closed, notes FROM bets WHERE id=?",
                      (bet[0],)).fetchone()
    assert row["status"] == "closed"
    assert row["closed"] == "2026-09-05", "the @date becomes the closing date"
    assert "the thesis played out" in row["notes"], "the reason must be kept"


async def test_a_bet_cannot_be_closed_while_it_still_holds_a_leg(app, con, goto):
    """THE SCHEMA DOES NOT ENFORCE THIS and it should.

    A bet marked closed while a leg is open is a book saying the thing is finished while
    money is still at risk. Every other refusal in set_bet_status restates a wall the
    database already has; this one adds one.
    """
    bet = con.execute(
        "SELECT bet_id FROM trades WHERE status='open' GROUP BY bet_id LIMIT 1"
    ).fetchone()[0]
    was = con.execute("SELECT status FROM bets WHERE id=?", (bet,)).fetchone()[0]
    async with app.run_test() as pilot:
        await goto(pilot, "bets")
        await _status(pilot, app, bet, "closed ~done with it")
        assert app.prompt.is_open, "closing over an open leg must be refused"
        assert "open leg" in app.prompt.error, app.prompt.error
        assert "Close the positions first" in app.prompt.error
    assert con.execute("SELECT status FROM bets WHERE id=?",
                       (bet,)).fetchone()[0] == was


async def test_falsified_requires_a_sentence(app, con, goto):
    """The strongest thing the book can say about a claim should not be one word."""
    bet = con.execute("SELECT id FROM bets WHERE status='live' LIMIT 1").fetchone()[0]
    _flatten(con, bet)
    async with app.run_test() as pilot:
        await goto(pilot, "bets")
        await _status(pilot, app, bet, "falsified")
        assert app.prompt.is_open
        assert "needs a sentence" in app.prompt.error, app.prompt.error
        app.prompt.value = "falsified ~the vol squeeze never came and VIX stayed at 14"
        await pilot.press("enter")
        await pilot.pause()
        assert not app.prompt.is_open, app.prompt.error
    row = con.execute("SELECT status, closed, notes FROM bets WHERE id=?",
                      (bet,)).fetchone()
    assert row["status"] == "falsified" and row["closed"]
    assert "VIX stayed at 14" in row["notes"]


async def test_a_status_move_is_undoable(app, con, goto):
    bet = con.execute("SELECT id FROM bets WHERE status='live' LIMIT 1").fetchone()[0]
    _flatten(con, bet)
    before = dict(con.execute("SELECT status, closed FROM bets WHERE id=?",
                              (bet,)).fetchone())
    async with app.run_test() as pilot:
        await goto(pilot, "bets")
        await _status(pilot, app, bet, "retired ~exited for other reasons")
        assert not app.prompt.is_open, app.prompt.error
        await pilot.press("u")
        await pilot.pause()
    after = dict(con.execute("SELECT status, closed FROM bets WHERE id=?",
                             (bet,)).fetchone())
    assert after == before, f"undo left {after}, was {before}"


async def test_a_no_op_status_is_refused(app, con, goto):
    """Which is why the prompt does not prefill the current status."""
    bet, was = con.execute("SELECT id, status FROM bets LIMIT 1").fetchone()
    async with app.run_test() as pilot:
        await goto(pilot, "bets")
        await pilot.press("s")
        await pilot.pause()
        assert app.prompt.value == "", (
            "prefilling the current status would make the cheap keystroke a no-op")
        await _status(pilot, app, bet, was)
        assert app.prompt.is_open
        assert "already" in app.prompt.error, app.prompt.error


def test_a_forming_bet_gets_its_opened_date_when_it_goes_live(con):
    """The schema requires `opened` for any status but forming, so going live sets it."""
    con.execute("UPDATE bets SET status='forming', opened=NULL WHERE id='growth'")
    assert con.execute("SELECT opened FROM bets WHERE id='growth'").fetchone()[0] is None
    book.set_bet_status(con, "growth", status="live", on_date="2026-09-08")
    row = con.execute("SELECT status, opened FROM bets WHERE id='growth'").fetchone()
    assert (row["status"], row["opened"]) == ("live", "2026-09-08")


def test_a_close_cannot_predate_the_open(con):
    row = con.execute("SELECT id, opened FROM bets WHERE opened IS NOT NULL "
                      "AND status='live' LIMIT 1").fetchone()
    if not row:
        pytest.skip("no live bet with an opened date")
    _flatten(con, row["id"])
    earlier = (dt.date.fromisoformat(row["opened"]) - dt.timedelta(days=1)).isoformat()
    with pytest.raises(book.DeskError, match="cannot close"):
        book.set_bet_status(con, row["id"], status="closed", on_date=earlier)


def test_every_status_the_schema_allows_is_reachable_through_the_grammar(con):
    """The parser's list and the schema's CHECK must not drift apart."""
    import re
    ddl = con.execute("SELECT sql FROM sqlite_master WHERE name='bets'").fetchone()[0]
    block = ddl[ddl.index("status"):]
    allowed = set(re.findall(r"'(\w+)'", block[:block.index(")")]))
    assert allowed, f"could not read the CHECK: {block[:120]}"
    assert allowed == set(parse.BET_STATUSES), (
        f"the grammar and the schema disagree: "
        f"only in schema {allowed - set(parse.BET_STATUSES)}, "
        f"only in grammar {set(parse.BET_STATUSES) - allowed}")


# --------------------------------------------------------------------------- #
# the write path: defects an adversarial audit confirmed, 2026-09-28
#
# These are one failure wearing five faces: a multi-statement write that was not a
# transaction, and a pre-image that could not put back everything the write removed.
# --------------------------------------------------------------------------- #
def test_atomic_rolls_back_when_the_COMMIT_itself_fails(blank):
    """THE COMMIT SAT OUTSIDE THE try, so the guarantee excluded the riskiest statement.

    COMMIT is where SQLite takes the write lock and touches the disk, so it is the one most
    likely to raise -- SQLITE_BUSY, or an I/O error on a synced volume. When it did, the
    exception escaped the `with` before any ROLLBACK ran and the connection stayed INSIDE an
    open transaction for the rest of the session: every later write reported success, SELECT
    on that connection saw it, a second reader saw none of it, and the implicit rollback at
    process exit discarded the lot.

    Turns red if the COMMIT moves back below the except, or the finally is dropped.
    """
    # A PROXY, because sqlite3.Connection.execute is a read-only attribute. atomic() only
    # ever touches .execute and .in_transaction, so forwarding those two is the whole surface.
    class FailsOnCommit:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *a):
            if str(sql).strip().upper().startswith("COMMIT"):
                raise sqlite3.OperationalError("disk I/O error")
            return self._con.execute(sql, *a)

        @property
        def in_transaction(self):
            return self._con.in_transaction

    proxy = FailsOnCommit(blank)
    with pytest.raises(sqlite3.OperationalError):
        with book.atomic(proxy):
            blank.execute("INSERT INTO bets(id,status,opened) "
                          "VALUES ('b-commit-fail','forming','2026-05-01')")

    assert not blank.in_transaction, (
        "a failed COMMIT left the connection inside a transaction -- every later write "
        "would report success and be discarded at process exit")
    assert blank.execute("SELECT 1 FROM bets WHERE id='b-commit-fail'").fetchone() is None, (
        "the write survived a COMMIT that failed")


def test_undo_survives_sqlite_reusing_the_deleted_rows_id(con):
    """`x`, then record anything, then `u`. restore() re-asserted the row's own rowid.

    `fills.id` is an INTEGER PRIMARY KEY with no AUTOINCREMENT, so deleting the HIGHEST row
    frees that id and the next INSERT takes it. restore() then collided on UNIQUE -- after
    its parent-trade INSERT had already run, leaving an open trade holding zero fills: the
    state delete_fill exists to prevent, which check.py rejects twice over, and which `x`
    cannot clear because on a leg with nothing left it answers "has no fills to delete".

    Turns red if `row.pop("id", None)` is removed from restore().
    """
    tid = "2026-05-04-cadco"
    fill = con.execute("SELECT id FROM fills WHERE trade_id=?", (tid,)).fetchone()["id"]
    assert fill == con.execute("SELECT MAX(id) FROM fills").fetchone()[0], (
        "fixture changed: this test needs the deleted fill to free the TOP rowid")

    pre = book.delete_fill(con, fill)
    assert con.execute("SELECT 1 FROM trades WHERE id=?", (tid,)).fetchone() is None
    # The ordinary next keystroke. It takes the freed id.
    book.add_fill(con, ticker="USDCO", shares=1.0, price=100.0, on_date="2026-05-05",
                  account="rrsp")
    assert con.execute("SELECT MAX(id) FROM fills").fetchone()[0] == fill, (
        "SQLite did not reuse the rowid, so this test no longer tests anything")

    book.restore(con, "fills", pre)          # used to raise UNIQUE constraint failed
    assert con.execute("SELECT COUNT(*) FROM fills WHERE trade_id=?",
                       (tid,)).fetchone()[0] == 1, "the fill did not come back"
    assert con.execute("SELECT 1 FROM trades WHERE id=?", (tid,)).fetchone() is not None
    for (t,) in con.execute("SELECT id FROM trades WHERE status='open'").fetchall():
        assert con.execute("SELECT COUNT(*) FROM fills WHERE trade_id=?",
                           (t,)).fetchone()[0] > 0, f"{t} is open holding nothing"


def test_deleting_a_leg_does_not_silently_destroy_its_confirmations(con):
    """`confirmations` and `falsifiers` cascade off trades(id), and undo could not see them.

    delete_fill removes an emptied trade and foreign keys are ON, so SQLite deleted the
    attached rows itself -- and the pre-image carried only the trade and the fill. On the real
    book one OPEN trade holds three confirmations rows, so `x` on its last fill destroyed
    three records of a known hole in the record with no message, and `u` then reported
    "restored the fills row" while they stayed gone.

    Turns red if delete_fill stops capturing the children, or restore stops replaying them.
    """
    tid = "2026-05-04-park"
    con.execute("INSERT INTO confirmations(scope,trade_id,field,issue) VALUES (?,?,?,?)",
                ("trade", tid, "fills[0].price", "was this the per-share price?"))
    con.execute("INSERT INTO falsifiers(trade_id,role,wrong_if,provenance,cadence) "
                "VALUES (?,?,?,?,?)",
                (tid, "side-leg", "the spread never closes", "at-entry", "quarterly"))
    before = (con.execute("SELECT COUNT(*) FROM confirmations WHERE trade_id=?",
                          (tid,)).fetchone()[0],
              con.execute("SELECT COUNT(*) FROM falsifiers WHERE trade_id=?",
                          (tid,)).fetchone()[0])
    assert before == (1, 1), "fixture did not take the dependents"

    fill = con.execute("SELECT id FROM fills WHERE trade_id=?", (tid,)).fetchone()["id"]
    pre = book.delete_fill(con, fill)
    assert con.execute("SELECT COUNT(*) FROM confirmations WHERE trade_id=?",
                       (tid,)).fetchone()[0] == 0, "the cascade did not fire"

    book.restore(con, "fills", pre)
    after = (con.execute("SELECT COUNT(*) FROM confirmations WHERE trade_id=?",
                         (tid,)).fetchone()[0],
             con.execute("SELECT COUNT(*) FROM falsifiers WHERE trade_id=?",
                         (tid,)).fetchone()[0])
    assert after == before, (
        f"undo restored the fill but not what the cascade took with it: {after} != {before}")
    assert con.execute("SELECT issue FROM confirmations WHERE trade_id=?",
                       (tid,)).fetchone()[0] == "was this the per-share price?", (
        "the row came back with different content")


def test_a_failed_undo_does_not_consume_the_row_it_was_replacing(con):
    """Undo of an EDIT is delete-then-restore, and it ran as two autocommits.

    So a restore that failed after the delete committed left the book worse than before the
    undo: the superseding row gone, the original not back, and -- because delete_row on a
    leg's last fill takes the emptied parent trade with it -- possibly an open trade holding
    nothing. app_undo's except clause pushes the entry back so the user can try again, which
    was a promise about state that had already been half-written.

    Turns red if the `with book.atomic(...)` around the two calls in app_undo is removed.
    """
    tid = "2026-05-04-cadco"
    original = con.execute("SELECT id FROM fills WHERE trade_id=?", (tid,)).fetchone()["id"]
    pre = book.delete_fill(con, original)
    _, replacement, _ = book.add_fill(con, ticker="CADCO", shares=99.0, price=9.0,
                                      on_date="2026-05-05", account="tfsa", bet_id="parking")
    broken = dict(pre)
    broken["shares"] = None            # fills.shares is REAL NOT NULL

    with pytest.raises(sqlite3.Error):
        with book.atomic(con):
            book.delete_row(con, "fills", replacement)
            book.restore(con, "fills", broken)

    assert not con.in_transaction, "the failed pair left a transaction open"
    assert con.execute("SELECT 1 FROM fills WHERE id=?", (replacement,)).fetchone(), (
        "the replacement was destroyed by an undo that then failed -- the user loses a row "
        "they never asked to delete, and the entry is pushed back as if nothing happened")


async def test_escape_clears_an_armed_edit_so_the_next_n_does_not_delete(app, con, goto):
    """`enter`, then escape through the APP binding, then `n`. It used to delete the row.

    `enter` arms `_editing` with a fill id; submitting then deletes that fill and re-adds it.
    on_inline_prompt_cancelled guards this and says so in its own comment -- but that handler
    fires on the prompt's cancel MESSAGE, while app_back is the app-level `esc` binding, which
    is what receives the key whenever focus is not inside the prompt (one mouse click is
    enough). So the same escape took a guarded or an unguarded path depending on where focus
    happened to be, and on the unguarded one the next `n` ran the edit branch: the fill under
    the old cursor was deleted and the toast said "edited".

    Turns red if app_back stops calling cancel_edit.
    """
    async with app.run_test() as pilot:
        await goto(pilot, "positions")
        pane = app._active_pane()
        t = app.query_one("#pos-table", DataTable)
        t.focus()
        # NOT ROW 0 -- that is a bet HEADING, keyed with the INERT prefix so that _cursor_trade
        # refuses it (the guard that stops `x` deleting a fill from a group title). The cursor
        # has to sit on a real open leg whose newest fill has a price.
        t.move_cursor(row=t.get_row_index("2026-05-04-usdco"))
        await pilot.pause()
        # THE PANE'S OWN HANDLER, not the keystroke. What is under test is app_back, and
        # routing `enter` needs the table focused -- which is exactly the condition that does
        # NOT hold in the bug being fixed, where focus had left the prompt.
        pane.key_activate()
        await pilot.pause()
        assert pane._editing is not None, "key_activate did not arm an edit"
        assert app.prompt.is_open, "the edit prompt did not open"

        app.app_back()                      # the binding, not the prompt's own message
        await pilot.pause()
        assert pane._editing is None, (
            "escape left an edit armed -- the next n/c/o would delete that row and "
            "report it as an edit")


# --------------------------------------------------------------------------- #
# withholding, and the home currency. 2026-09-29
# --------------------------------------------------------------------------- #
def _unprice_one_leg(con):
    """Make exactly one open leg impossible to cost, the way the real book does it.

    Dropping the fx row rather than the entry price, because that is the case the audit
    reproduced and the one that actually happens: a fill is recorded the moment it happens and
    the rate arrives on the next 30-second poll.
    """
    ccy = con.execute(
        "SELECT t.currency FROM trades t JOIN v_position p ON p.trade_id=t.id "
        "WHERE t.status='open' AND t.currency <> (SELECT 'CAD') LIMIT 1").fetchone()
    assert ccy is not None, "fixture changed: needs an open leg in a foreign currency"
    con.execute("DELETE FROM prices WHERE kind='fx' AND base=?", (ccy[0],))
    return ccy[0]


def test_an_unpriceable_leg_withholds_the_spend_instead_of_shrinking_it(con):
    """THE SIGNATURE DEFECT, guarded at last. Mutation-proven vacuous before this existed.

    The audit showed that replacing either withholding expression with a zero-coalescing sum
    kept all 310 tests green, while one unpriced leg made the bets tab and the dashboard
    confidently UNDERSTATE the spend -- the +11,760.18 shape. The old assertion that claimed
    to guard it passed because the fixture bet had no allocation at all, so filled_pct
    returned None before cost was ever read.

    Turns red if Bet.cost, PortfolioGroup.cost or BetComposition's `dry` starts coalescing.
    """
    _unprice_one_leg(con)

    affected = [b for b in book.bets(con) if b.unpriced_legs]
    assert affected, "fixture: no bet ended up holding the unpriceable leg"
    for b in affected:
        assert b.cost is None, (
            f"{b.id} reported a confident cost of {b.cost} while {b.unpriced_legs} leg(s) "
            f"cannot be translated -- the sum skipped them instead of withholding")
        assert b.filled_pct is None, f"{b.id} printed a filled percentage from a partial spend"
        assert b.value is None, f"{b.id} reported a market value it cannot know"
        # AND THE COMPOSITION BLOCK'S dry, which is what draws the (dry) bar.
        c = book.bet_composition(con, b.id)
        assert c.unpriced, f"{b.id}: bet_composition did not name the unpriced ticker"
        assert c.dry is None, (
            f"{b.id}: dry={c.dry} subtracts only the legs it could price, so unmeasured "
            f"spend is drawn as unspent budget")

    for g in book.portfolio(con):
        if g.bet_id is None or not g.allocation:
            continue
        blind = [x for x in g.legs if x.cost_base is None]
        if blind:
            assert g.cost is None and g.filled_pct is None, (
                f"{g.name}: the dashboard printed {g.filled_pct} with {len(blind)} leg(s) "
                f"it cannot cost")


def test_an_allocation_in_another_currency_is_not_a_denominator(con):
    """set_allocation stamps allocation_ccy with the home currency AT WRITE TIME.

    So changing `home_currency` in config.toml leaves every stored allocation denominated in
    the old one -- while `filled`, `of_allocated_pct` and the composition bars all divide a
    home-currency cost by it. The figure was printed, not withheld, and wrong by the whole FX
    factor: a bet allocated C$20,000 holding US$10,000 reported 50.0% where the truth is 70.0%.

    Withheld rather than translated, deliberately: an allocation is a number the user TYPED,
    and silently restating what someone declared is worse than declining to divide by it.

    Turns red if the allocation_ccy guard in bets() or bet_composition() is removed.
    """
    target = con.execute(
        "SELECT id FROM bets WHERE allocation_base IS NOT NULL LIMIT 1").fetchone()
    assert target is not None, "fixture changed: needs a bet with an allocation"
    bet_id = target["id"]
    before = next(b for b in book.bets(con) if b.id == bet_id)
    assert before.allocation is not None and before.of_allocated_pct is not None, (
        "fixture: this bet should have a usable allocation to begin with")

    # The allocation was declared in a currency that is no longer home.
    con.execute("UPDATE bets SET allocation_ccy='JPY' WHERE id=?", (bet_id,))

    after = next(b for b in book.bets(con) if b.id == bet_id)
    assert after.allocation is None, (
        "an allocation stamped in a non-home currency was still used as a denominator")
    assert after.filled_pct is None and after.of_allocated_pct is None
    c = book.bet_composition(con, bet_id)
    assert c.allocation is None and c.dry is None, (
        "bet_composition still divided by a foreign-currency allocation")


def test_a_mark_in_the_wrong_currency_makes_a_leg_unpriced_not_mispriced(con):
    """`quote` is part of the prices PRIMARY KEY, so a wrong-currency mark JOINS the old row.

    INSERT OR REPLACE cannot replace across a currency change, so both rows coexist and the
    mark subquery picked one on a rowid tiebreak -- then applied the fx rate for the
    instrument's registered currency, valuing the position at a foreign price and printing it
    as an ordinary number. Reachable whenever the price source's currency disagrees with the
    one captured at registration, which lowercase payloads alone can cause.

    Turns red if `AND quote=t.currency` is dropped from positions().
    """
    leg = next(p for p in book.positions(con) if p.mark is not None)
    real = leg.mark
    # A mark for the same ticker and the same day, in a currency it is not quoted in.
    con.execute("INSERT OR REPLACE INTO prices(base,quote,kind,on_date,amount,source) "
                "VALUES (?,?,?,?,?,?)",
                (leg.ticker, "JPY" if leg.currency != "JPY" else "KRW", "mark",
                 "2099-01-01", real * 157.0, "test"))
    after = next(p for p in book.positions(con) if p.trade_id == leg.trade_id)
    assert after.mark == real, (
        f"{leg.ticker} took a mark quoted in another currency: {after.mark} vs {real}")


def test_the_cost_basis_is_an_acb_and_not_an_average_of_buys(con):
    """v_position.average_price averaged BUYS while shares_held is NET. CRITICAL.

    The two terms of `shares_held * average_price` came from different fill sets, so it was not
    the cost of the shares still held -- and every cost figure in the tool is built on it.

    The audit's worked example: buy 60@100, trim -20@110, add 20@150. The view says 112.50
    (9,000/80) and a cost of 6,750; the truth is 116.667 and 7,000, because selling 20 of 60
    removes a THIRD of the cost, not a third of the shares at the later average.

    The error is exactly zero when every buy precedes every sell or all buy prices are equal --
    which is the shape of tests/seed.py, and why 310 tests passed over this for months. So the
    fixture is given the failing shape explicitly.

    Turns red if positions() goes back to reading average_price from the view.
    """
    con.execute("INSERT INTO bets(id,status,opened) VALUES ('acbtest','forming','2026-06-01')")
    book.add_fill(con, ticker="CADCO", shares=60.0, price=100.0, on_date="2026-06-01",
                  account="tfsa", bet_id="acbtest")
    book.add_fill(con, ticker="CADCO", shares=-20.0, price=110.0, on_date="2026-06-02",
                  account="tfsa", bet_id="acbtest")
    book.add_fill(con, ticker="CADCO", shares=20.0, price=150.0, on_date="2026-06-03",
                  account="tfsa", bet_id="acbtest")

    leg = next(p for p in book.positions(con)
               if p.ticker == "CADCO" and p.bet_id == "acbtest")
    assert leg.qty == pytest.approx(60.0)
    assert leg.entry == pytest.approx(7000.0 / 60.0, abs=1e-6), (
        f"entry {leg.entry} is the average of the BUYS (112.5), not the cost base of what "
        f"is held (116.667)")
    assert leg.cost_native == pytest.approx(7000.0, abs=0.005), (
        f"cost {leg.cost_native} -- a sell must remove cost in the same proportion as shares")

    # AND THE VIEW STILL DISAGREES, which is the point: the arithmetic moved to Python
    # because SQL could not express it. If this ever matches, the view was fixed too and
    # this assertion should go rather than being loosened.
    view = con.execute("SELECT average_price FROM v_position WHERE trade_id=?",
                       (leg.trade_id,)).fetchone()[0]
    assert view == pytest.approx(112.5), (
        "v_position.average_price changed meaning; re-read this test")


def test_a_cost_basis_that_cannot_be_ordered_is_withheld(con):
    """ORDER MATTERS NOW, so an undated fill makes the answer unknowable rather than a guess.

    The old view had no opinion about order at all, which is exactly how it managed to be
    wrong without ever looking uncertain. A buy with no price withholds for the same reason
    it always did; a SELL with no price does NOT, because what a sale realises belongs to
    realised P&L and not to the cost of what is left.

    Turns red if _acb_per_share starts defaulting instead of returning None.
    """
    con.execute("INSERT INTO bets(id,status,opened) VALUES ('undated','forming','2026-07-01')")
    book.add_fill(con, ticker="CADCO", shares=10.0, price=10.0, on_date="2026-07-01",
                  account="tfsa", bet_id="undated")
    leg = next(p for p in book.positions(con)
               if p.ticker == "CADCO" and p.bet_id == "undated")
    assert leg.entry == pytest.approx(10.0), "fixture: this leg should cost out fine first"

    con.execute("UPDATE fills SET on_date=NULL WHERE id=("
                "SELECT MAX(id) FROM fills WHERE trade_id=?)", (leg.trade_id,))
    after = next(p for p in book.positions(con) if p.trade_id == leg.trade_id)
    assert after.entry is None and after.cost_native is None and after.cost_base is None, (
        "an undated fill leaves the order -- and therefore the cost -- unknowable, so every "
        "cost figure must be withheld rather than computed from a guessed order")

    # A SELL with no price is fine: its price is not an input to the cost of what remains.
    con.execute("UPDATE fills SET on_date='2026-07-01' WHERE trade_id=? AND on_date IS NULL",
                (leg.trade_id,))
    book.add_fill(con, ticker="CADCO", shares=-4.0, price=99.0, on_date="2026-07-02",
                  account="tfsa", bet_id="undated")
    con.execute("UPDATE fills SET price=NULL WHERE id=("
                "SELECT MAX(id) FROM fills WHERE trade_id=?)", (leg.trade_id,))
    sold = next(p for p in book.positions(con) if p.trade_id == leg.trade_id)
    assert sold.entry == pytest.approx(10.0), (
        "a sell with no recorded price does not change the cost base of the shares left")


def test_no_user_facing_string_names_an_unbound_key():
    """THE PROSE-TRUTH DEFECT, made checkable instead of re-found.

    This repo has advertised a removed key four times: the README's `m`, and three entries in
    __main__'s removed-subcommand map (`marks` -> "m", `asof` -> "k", `instrument` -> "i"),
    each naming a binding that had been deleted. The README's own closing paragraph diagnoses
    it -- "a hand-written table in a README has no such test, which is exactly how it comes to
    advertise a key that was removed" -- and then the repo declined twice to add the
    assertion. This is that assertion.

    Scoped to strings that TELL SOMEONE TO PRESS SOMETHING: the pattern is a backtick-quoted
    single character, or a bare key followed by " on " / " to ". Prose that merely mentions a
    letter is not a claim about a binding, and a test that flagged it would be deleted within
    a week for crying wolf.
    """
    bound = {k.key for k in keymap.KEYMAP} | {"tab", "enter", "escape", "question_mark"}
    # The keymap spells two of them the way Textual does; the prose spells them as glyphs.
    bound |= {"?", "1", "2", "3"}

    offenders = []
    for rel in ("desk/__main__.py", "desk/tui/footer.py"):
        text = (pathlib.Path(ROOT) / rel).read_text()
        for i, line in enumerate(text.split("\n"), 1):
            if line.lstrip().startswith("#"):
                continue          # a comment explaining a DELETED key is the point
            for m in re.finditer(r"`([a-zA-Z?])` (?:on|to)\b|\b([a-zA-Z?]) on the\b", line):
                key = m.group(1) or m.group(2)
                if key not in bound:
                    offenders.append(f"{rel}:{i} names `{key}`, which is bound to nothing")
    assert not offenders, "\n".join(offenders)
