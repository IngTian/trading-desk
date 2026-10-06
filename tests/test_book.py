"""AUDIT THE REAL BOOK, whichever one this machine is pointed at.

This file is different in kind from test_desk.py. That one tests CODE and runs
against a synthetic fixture. This one tests the RECORD -- that the author's actual
positions, prices and conversions hold together -- so it needs their actual data and
SKIPS when there is none.

That split is the point. The book left the repository on 2026-09-07 ("data is data
and code is code"), so a fresh clone has code and no record, and a test that
demanded one would fail for a reason that is not a defect. Everything here asserts
a RELATIONSHIP rather than a figure, so it keeps working as the record changes --
which is what makes it safe to run against live data at all.

    TRADING_DESK_BOOK=/path/to/hub.db pytest tests/test_book.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from desk import book, config  # noqa: E402
from desk import check as bookcheck  # noqa: E402

DB = os.fspath(config.book_path())


@pytest.fixture(scope="module")
def con():
    if not os.path.exists(DB):
        pytest.skip(
            f"no book at {DB}. This file audits a real record; the code tests live "
            f"in test_desk.py and need no data. Set TRADING_DESK_BOOK to audit one.")
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)   # read-only: an audit
    # sqlite3.Row, because desk/book.py reads columns BY NAME. book.connect() sets this and
    # this fixture did not, so calling any book.* function from an audit raised
    # "tuple indices must be integers" -- a fixture defect that looked like a library one.
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()

@pytest.fixture()
def writable(tmp_path):
    """A throwaway book from book/schema.sql, for tests that must WRITE.

    The three tests below prove that walls fire, which needs an INSERT that the wall
    then rejects. `con` is the author's record opened read-only -- an audit must not be able
    to change what it audits -- so those tests get their own book instead.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import seed
    c = seed.build(tmp_path / "hub.db")
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


def test_file_is_not_corrupt(con):
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_no_orphan_rows(con):
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("name,sql,why", bookcheck.INVARIANTS,
                         ids=[i[0] for i in bookcheck.INVARIANTS])
def test_invariant_holds(con, name, sql, why):
    rows = con.execute(sql).fetchall()
    assert rows == [], f"{name}: {[r[0] for r in rows]}\n  {why}"


# --- properties of the schema itself, independent of the current data ---------

# test_a_wall_cannot_be_deviated_from WAS HERE. It asserted that the
# `no_deviation_from_a_wall` trigger refused a deviations row against a rule marked as
# a wall -- the mechanical proof that a wall was not negotiable. Both the trigger and
# the rules/rule_versions/deviations tables went in v14: with no rules, there is no
# wall to be non-negotiable about.


# test_a_close_cannot_cite_a_falsifier_of_another_bet WAS HERE, guarding the trigger
# that stopped one stray integer manufacturing a score. Both went in v15 with
# exit_falsifier_id, which 0 of 23 trades ever carried and only v_scoreable_closes read.
# The falsifiers themselves are untouched -- all 14 stay; nothing grades them.


def test_events_is_append_only(writable):
    for stmt in ("UPDATE events SET actor='someone'", "DELETE FROM events"):
        with pytest.raises(sqlite3.IntegrityError):
            writable.execute(stmt)
        writable.rollback()


# THE TWO STOP-CURVE TESTS WERE HERE and they were a tautology by the end. v14 deleted
# the curve, and these did not reference any view or function -- each wrote the formula
# out AS A LITERAL in its own query and asserted SQLite could evaluate it. They survived
# v14 green while testing nothing that ships, which is the schema_attack.sh failure mode:
# a file that looks like coverage and is not is worse than no file.


def test_nothing_derived_is_stored(con):
    """average_price and cost basis are computed by v_position, never columns."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(trades)")}
    for banned in ("average_price", "cost_basis", "cost_basis_total",
                   "position_shares", "shares_held", "realised"):
        assert banned not in cols, f"trades.{banned} would be a second copy of a derived number"


# --------------------------------------------------------------------------- #
# INVARIANTS, NOT FIGURES.
#
# An earlier version of this block asserted cost == 34999.96, proceeds == 35892.50,
# the exit price 152.28, the sale date 2026-08-31. Every one of those was a number
# The author or a statement had handed over, so the tests only restated the input: they
# would pass on a book where the arithmetic was broken and fail on a book that was
# merely corrected. The one generic test in the original set -- "no fill is dated to
# a weekend" -- immediately found a real bug the specific ones all missed.
#
# So these assert relationships that must hold for ANY book: things that stay true
# after the next correction, the next trade, the next account.
# --------------------------------------------------------------------------- #
def test_income_never_lands_in_flows(con):
    """A dividend is P&L; in `flows` it would inflate the loss limit's denominator.

    flows is the denominator every drawdown figure divides by, so it must contain
    contributions and nothing else. Income booked there would read as capital paid
    in and hide a real gain.
    """
    kinds = {r[0] for r in con.execute("SELECT DISTINCT kind FROM flows")}
    assert kinds <= {"deposit", "withdrawal"}


def test_income_currency_is_one_the_book_can_convert(con):
    """attribution() values income at spot. A currency with no rate would be
    counted at par, so an unconvertible one must not exist silently."""
    have = {r[0] for r in con.execute("SELECT DISTINCT base FROM prices WHERE kind='fx'")}
    for ccy, in con.execute("SELECT DISTINCT currency FROM income"):
        assert ccy == "CAD" or ccy in have, f"income in {ccy} with no {ccy}->CAD rate"


def test_a_closed_trade_holds_nothing_and_a_position_is_never_short(con):
    for tid, status in con.execute("SELECT id, status FROM trades"):
        net = con.execute("SELECT COALESCE(SUM(shares),0) FROM fills WHERE trade_id=?",
                          (tid,)).fetchone()[0]
        assert net >= -1e-9, f"{tid} is net short ({net})"
        if status == "closed":
            assert abs(net) < 1e-9, f"{tid} is closed but still holds {net}"


def test_settled_base_and_shares_times_price_agree_to_rounding(con):
    """Both describe the same cash, so they may differ only by display rounding.

    Statements print share counts to 4dp and prices to 2dp, so the reconstruction
    drifts a little. A LARGE gap means one of the two is simply wrong -- a
    transposed digit, or a settled amount recorded against the wrong fill.
    """
    for tid, on_date, shares, price, settled in con.execute(
            "SELECT trade_id, on_date, shares, price, settled_base FROM fills "
            "WHERE settled_base IS NOT NULL AND price IS NOT NULL"):
        product = abs(shares * price)
        # 4dp on shares at a price P is at most P/2 of drift, plus 2dp on the price
        # over the whole quantity. Generous, but orders of magnitude below a typo.
        tol = max(0.01, price * 0.5 + abs(shares) * 0.005)
        assert abs(product - settled) <= tol, (
            f"{tid} {on_date}: shares*price={product:,.2f} but settled_base="
            f"{settled:,.2f}, off by {product - settled:+,.2f} (tolerance {tol:,.2f})")


def test_a_currency_conversion_never_pays_better_than_the_market(con):
    """A conversion costs the holder something. An implied rate BETTER than market
    means the amounts are wrong; an absurdly worse one means a typo.

    No figure is pinned: the market rate is read from the book and the bound is a
    plausibility band, so this keeps working when the amounts are replaced by the
    statement's.
    """
    for f_ccy, f_amt, t_ccy, t_amt, on_date in con.execute(
            "SELECT from_ccy, from_amount, to_ccy, to_amount, on_date "
            "FROM fx_conversions WHERE to_amount IS NOT NULL"):
        row = con.execute(
            "SELECT amount FROM prices WHERE kind='fx' AND base=? AND quote=? "
            + ("AND on_date <= ? " if on_date else "")
            + "ORDER BY on_date DESC LIMIT 1",
            (f_ccy, t_ccy, on_date) if on_date else (f_ccy, t_ccy)).fetchone()
        if row is None:                      # try the inverse pair
            # `on_date` HONOURED HERE TOO. It was missing, so this branch always took the
            # LATEST rate -- and since the book stores USD->CAD only, every CAD->USD
            # conversion came down this path and was judged against today's market.
            row = con.execute(
                "SELECT 1.0/amount FROM prices WHERE kind='fx' AND base=? AND quote=? "
                + ("AND on_date <= ? " if on_date else "")
                + "ORDER BY on_date DESC LIMIT 1",
                (t_ccy, f_ccy, on_date) if on_date else (t_ccy, f_ccy)).fetchone()
        if row is None:
            continue                         # no rate to judge against
        if not on_date:
            # AN UNDATED CONVERSION CANNOT BE JUDGED, and pretending otherwise made this
            # test a function of the market rather than of the book. The one real row has
            # no date by admission -- "DATE NOT RECORDED. The July statement closed 100%
            # CAD, so it happened in August" -- and its implied 1.40225 was correctly WORSE
            # than the 1.38147 spot at the time. Compared against today's 1.4081 it reads
            # as a conversion that paid the holder, so this assertion flipped from green to
            # red on nothing but CAD weakening. A test that breaks when the market moves is
            # not testing the book. It comes back the moment the August statement supplies
            # the date, which is already the recorded plan for this row.
            continue
        market, implied = row[0], t_amt / f_amt
        # implied is destination-per-source; a worse deal means FEWER units back.
        spread = market / implied - 1
        assert spread > -1e-6, (
            f"{f_ccy}->{t_ccy} implies {implied:.5f} vs market {market:.5f} — that is "
            f"a conversion that PAID the holder {-spread:.4%}, so an amount is wrong")
        assert spread < 0.05, (
            f"{f_ccy}->{t_ccy} implies a {spread:.2%} spread, which is far outside any "
            f"retail conversion — check for a transposed digit")


# test_the_book_can_account_for_its_own_pnl WAS HERE, and it had not run in a month.
#
#     realised + income + unrealised + fx  ==  balance - contributions
#
# THE BEST INVARIANT IN THIS FILE, and the only one that could catch a fill that was never
# recorded -- every other check asks whether the book agrees with ITSELF, and a book missing a
# fill is perfectly self-consistent. This one compared against an outside number.
#
# It could not run. Its body sat behind `if "STALE:" in out: skip(...)`, and attribution()
# prints STALE whenever a fill is dated after the newest balance snapshot. NOTHING IN THE
# SHIPPED CODE WRITES account_snapshots -- no INSERT or UPDATE outside the fixtures -- so the
# newest snapshot is whatever was last typed into sqlite3 by hand: 2026-09-07 here, with 25 of
# 49 fills after it. The skip condition could not become false by using the tool.
#
# Its own skip message said "Record a balance (press b)", and `b` moves a leg between bets. So
# it named a key that does something else, to do a thing the desk cannot do, to reach an
# assertion that never fired.
#
# The author, 2026-10-06: "well if you skip a test why dont you delete it." Same argument that
# removed tests/schema_attack.sh -- a file that looks like coverage and is not is worse than no
# file, because a green suite then reads as a checked property.
#
# attribution() ITSELF STAYS and now has a consumer: desk-mcp exposes it, which is how the
# reconciliation is meant to happen -- hand a model a statement and have it compare. If a way
# to record a balance is ever added, this test is worth restoring; it was a good test with no
# way to reach its own body.

def test_no_view_divides_by_a_single_account_snapshot(con):
    """The one-sleeve-is-the-whole-book bug, as a grep the schema enforces.

    latest_balance() had it, was fixed, and v_bet_exposure still had it in SQL where
    nobody looked — a cross-account numerator over one account's total, overstating
    every bet's weight by the size of the other sleeve. Any portfolio denominator
    must aggregate per account first.
    """
    import re
    for name, sql in con.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='view' AND sql IS NOT NULL"):
        for m in re.finditer(r"FROM\s+account_snapshots\b[^)]*", sql, re.I):
            frag = m.group(0)
            if re.search(r"ORDER\s+BY\s+as_of\s+DESC\s+LIMIT\s+1", frag, re.I) \
                    and not re.search(r"GROUP\s+BY|account_id\s*=", frag, re.I):
                pytest.fail(
                    f"{name} takes ONE snapshot row as the portfolio:\n  {frag}")


def test_a_portfolio_weight_is_absent_rather_than_wrong(con):
    """If a bet cannot be valued, its weight must be absent, not approximate.

    THIS USED TO READ v_bet_exposure, which v12 dropped. The view's denominator was the
    pooled `account_snapshots` — a table nothing writes any more — and it valued a USD leg
    with no recorded rate AT PAR via `COALESCE(..., 1.0)`, wrong by about 38%. The property
    the test was written for survives the view; it is asserted here against book.bets(),
    which is what actually runs.

    Generic on purpose: it asserts RELATIONSHIPS, so it keeps holding as accounts, bets and
    prices change rather than pinning today's figures.
    """
    n = book.net(con)
    bets = book.bets(con)
    if not bets:
        pytest.skip("no bets recorded")

    for b in bets:
        if b.unpriced_legs:
            assert b.value is None and b.weight_pct is None, (
                f"{b.id} has {b.unpriced_legs} leg(s) that cannot be valued, so its "
                f"weight must be withheld rather than understated")
        if not b.legs:
            assert b.value in (None, 0.0), f"{b.id} holds nothing"

    if n.net is None:
        assert all(b.weight_pct is None for b in bets), (
            "with no portfolio total there is no denominator, so no weight is meaningful")
        return

    weights = [b.weight_pct for b in bets if b.weight_pct is not None]
    assert sum(weights) <= 100.0 + 1e-6, (
        f"bets cannot exceed the whole portfolio ({n.net:,.2f} {n.home})")
    # AND THE NUMERATOR IS THE SAME MONEY THE DASHBOARD COUNTS. A weight computed off a
    # different valuation of the same legs is a second answer to one question.
    held = sum(b.value for b in bets if b.value is not None)
    assert held <= (n.positions or 0) + 1e-6, (
        "the bets hold more than the portfolio's open positions are worth")


def test_a_weekend_fill_is_declared_rather_than_forbidden(con):
    """SOFTENED, because the original rule was wrong.

    It asserted no fill may fall on a Saturday or Sunday, and it did earn its keep
    -- it found CMG sitting on Sunday 2026-08-16. But the author: "ndx can now trade in
    weekends (premarket session) so the rule should not be that rigid." They are right.
    Extended and weekend sessions exist, so a weekend fill can be perfectly real,
    and a test that forbids it would force a true record to be falsified to stay
    green. That is the worst kind of check.

    What survives is the useful half: a weekend fill is UNUSUAL, so it must carry a
    note or an open confirmation saying which session it was. Declared is fine;
    silent is not. Same principle as everywhere else in this book.
    """
    import datetime as dt
    undeclared = []
    for tid, on_date, notes in con.execute(
            "SELECT trade_id, on_date, notes FROM fills WHERE on_date IS NOT NULL"):
        if dt.date.fromisoformat(on_date).weekday() < 5:
            continue
        flagged = con.execute(
            "SELECT 1 FROM confirmations WHERE trade_id=? AND resolved_on IS NULL",
            (tid,)).fetchone()
        if not notes and not flagged:
            undeclared.append((tid, on_date))
    assert not undeclared, (
        f"weekend fills with nothing explaining the session: {undeclared}. A weekend "
        f"or extended-hours fill is legitimate — say so in the note.")


def test_no_price_row_is_dated_to_a_weekend(con):
    """Same rule for prices, and it catches the FX date bug directly.

    Yahoo stamps USDCAD=X bars at 23:00 UTC of the PREVIOUS day, so deriving the
    date in UTC put FX rows on Sundays and left none on the Friday the balance was
    anchored to. prices.py now uses the exchange's gmtoffset.

    NARROWED FOR FX ON 2026-09-26, when it failed on a single legitimate row. An exchange is
    SHUT at the weekend, so an equity mark dated Saturday can only be a stamping error and
    the rule stays absolute for those. FX has no exchange calendar: Yahoo carries a live bar
    for the current date whatever the day, so a Saturday USD/CAD row is the last known rate
    rather than a phantom session -- and Friday 1.41460 and Saturday 1.41410 were both
    present, which is the opposite of the original bug's signature.

    So the FX rule becomes the SHAPE of that bug rather than the calendar: a weekend rate is
    allowed, but only if the Friday before it is there too. That still fails the moment
    rows land on the weekend INSTEAD of the trading day, which is what this was for.
    """
    import datetime as dt

    rows = [(r[0], r[1], r[2]) for r in con.execute(
        "SELECT base, kind, on_date FROM prices")]
    have = {(base, on) for base, _, on in rows}

    bad = []
    for base, kind, on in rows:
        day = dt.date.fromisoformat(on)
        if day.weekday() < 5:
            continue
        if kind != "fx":
            bad.append((base, kind, on, "an exchange is shut at the weekend"))
            continue
        friday = day - dt.timedelta(days=day.weekday() - 4)
        if (base, friday.isoformat()) not in have:
            bad.append((base, kind, on,
                        f"no rate on the Friday before it ({friday})"))
    assert not bad, f"price rows dated to a weekend: {bad}"


def test_every_traded_currency_has_a_rate(con):
    base = con.execute("SELECT allocation_ccy FROM bets WHERE allocation_ccy "
                       "IS NOT NULL LIMIT 1").fetchone()
    base = base[0] if base else "CAD"
    for ccy, in con.execute("SELECT DISTINCT currency FROM trades"):
        if ccy == base:
            continue
        assert con.execute("SELECT 1 FROM prices WHERE kind='fx' AND base=? AND quote=?",
                           (ccy, base)).fetchone(), f"no {ccy}->{base} rate recorded"



# --------------------------------------------------------------------------- #
# attribution() must never invent a rate
#
# The "no par fallback" comment above the guard promised this and the guard did not
# deliver it: `has_usd` asked `SELECT 1 FROM trades WHERE currency='USD'` and USD money
# also enters through income.currency and fx_conversions. On a book holding a USD sleeve
# funded by a conversion but with no USD POSITION yet, the rate defaulted to 1.0.
#
# Proven on a day-one book -- 40,000 CAD in, 14,000 CAD -> 9,900 USD, no trades, no
# marks: it printed "currency conversion, spread and FX since -4,100.00" where the truth
# at 1.40 is -140.00, then "UNEXPLAINED +3,960.00", then "all 12 hold" and exit 0.
#
# And the state is not a corner. prices.py requests the USD/CAD pair only for currencies
# of trades with status='open', so with no USD trade open `desk marks` never fetches the
# rate and cannot repair it.
# --------------------------------------------------------------------------- #
def _attribution_output(con):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bookcheck.attribution(con)
    return buf.getvalue()


def test_usd_from_a_conversion_alone_still_blocks_a_par_rate(writable):
    """USD money with no USD TRADE must still stop the attribution."""
    con = writable
    # Strip the positions and every rate, leaving USD present only via a conversion.
    con.execute("DELETE FROM fills")
    con.execute("DELETE FROM trades")
    con.execute("DELETE FROM prices WHERE kind='fx'")
    conv = con.execute("SELECT COUNT(*) FROM fx_conversions "
                       "WHERE to_ccy='USD' OR from_ccy='USD'").fetchone()[0]
    assert conv, "the fixture needs a USD conversion for this to mean anything"
    assert con.execute("SELECT COUNT(*) FROM trades WHERE currency='USD'"
                       ).fetchone()[0] == 0

    out = _attribution_output(con)
    assert "cannot be converted" in out, (
        "USD entered by conversion and the rate was invented anyway:\n" + out)
    assert "UNEXPLAINED" not in out, (
        "a residual computed at a par rate is a fabricated number:\n" + out)


def test_usd_income_alone_still_blocks_a_par_rate(writable):
    """Same door, different key: a USD dividend with no USD trade."""
    con = writable
    con.execute("DELETE FROM fills")
    con.execute("DELETE FROM trades")
    con.execute("DELETE FROM fx_conversions")
    con.execute("DELETE FROM prices WHERE kind='fx'")
    acct = con.execute("SELECT id FROM accounts WHERE is_default=1").fetchone()[0]
    cols = [r[1] for r in con.execute("PRAGMA table_info(income)")]
    row = {"on_date": "2026-06-01", "account_id": acct, "amount": 10.0,
           "currency": "USD", "kind": "interest", "source": "statement",
           "notes": None, "ticker": None}
    keys = [k for k in row if k in cols]
    con.execute(f"INSERT INTO income({','.join(keys)}) "
                f"VALUES ({','.join('?' * len(keys))})", tuple(row[k] for k in keys))

    out = _attribution_output(con)
    assert "cannot be converted" in out, (
        "USD entered as income and the rate was invented anyway:\n" + out)


def test_a_currency_the_attribution_cannot_convert_is_refused(writable):
    """A third currency was booked at par: 1 GBP = 1 CAD.

    to_cad returns anything that is not USD unchanged, while the income path and the
    conversion path both already refuse a currency they cannot convert. Only the trades
    path was silent, and it moved unrealised by the whole notional.
    """
    con = writable
    tid = con.execute("SELECT id FROM trades LIMIT 1").fetchone()
    if tid is None:
        pytest.skip("no trade to relabel")
    try:
        con.execute("UPDATE trades SET currency='GBP' WHERE id=?", (tid[0],))
    except Exception:
        pytest.skip("the schema constrains trades.currency, which is a better fix")
    out = _attribution_output(con)
    assert "GBP" in out and "cannot convert" in out, (
        "a GBP leg was valued at par:\n" + out)
    assert "UNEXPLAINED" not in out, out
