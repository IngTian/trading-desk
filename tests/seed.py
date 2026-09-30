"""A synthetic book, built from desk/schema.sql. The tests do not read the author's record.

WHY THIS EXISTS. The desk tests used to run against a COPY of the live book, and
that coupling failed three separate times in one week: `capital_in(con) is None`
went red when a real contribution was recorded, a row count of 3 went red when a
real position was opened, and `{"tfsa": 35000}` went red when the author remembered a
two-dollar top-up. Every one of those failures was the book becoming MORE accurate.
A test that punishes that is asserting about data, not code.

So the fixture is deterministic and made up. Numbers here are chosen to exercise
shapes, not to match anything real:

  * two accounts, one of them the default, so pooling is exercised
  * one CAD instrument and one USD one, so every currency branch is taken
  * a bet with a frozen allocation, and one with none
  * a closed round trip, an open position, and a partial trim
  * a mark and an FX rate on the same date as the snapshots, so the P&L
    attribution has one valuation date and no staleness
  * the portfolio loss limit as a rule version, because the desk reads the wall
    from data rather than a constant
  * income in BOTH currencies, so the per-currency conversion is executed
  * an fx_conversions row with a real spread, so the FX term is executed and its
    SIGN is asserted -- at exactly spot the term is 0.00 and a sign error hides

Tests that genuinely need to audit the REAL record live in test_book.py and skip
when there is no book to audit.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from desk import config  # noqa: E402

# One date for everything: snapshots, marks and FX. A fixture whose sleeves are
# marked on different days would exercise the "refuses to total" branch on every
# unrelated test.
AS_OF = "2026-09-07"
FX_USDCAD = 1.4


def build(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.executescript(config.schema_sql().read_text())
    # FOREIGN KEYS ON, BECAUSE book.connect() TURNS THEM ON. This fixture did not, so the
    # whole suite ran against a database whose referential integrity was switched off while
    # the shipping code runs with it on -- the two differed in a pragma that changes
    # SEMANTICS, not just strictness: `ON DELETE CASCADE` does nothing when foreign keys are
    # off. That is why 310 tests could not see delete_fill silently destroying the
    # confirmations and falsifiers rows attached to a trade it removed; the cascade the bug
    # depends on never fired under test. An audit found the bug by reading the schema.
    con.execute("PRAGMA foreign_keys = ON")
    x = con.execute

    x("INSERT INTO session(actor) VALUES ('test')")
    x("INSERT INTO schema_version VALUES (9, ?, 'schema.sql', 'test fixture')",
      (AS_OF,))

    # ---- accounts. Two, so pooling and the default are both exercised.
    x("INSERT INTO accounts(id,name,opened,is_default) "
      "VALUES ('tfsa','Test TFSA','2026-01-02',1)")
    x("INSERT INTO accounts(id,name,opened,is_default) "
      "VALUES ('rrsp','Test RRSP','2026-01-02',0)")

    # ---- instruments. One CAD, two USD, one money-market, so every branch is taken.
    for tk, ccy, kind, rf, sym in (("CADCO", "CAD", "common", 0, "CADCO.TO"),
                                   ("USDCO", "USD", "common", 0, "USDCO"),
                                   ("LEVR", "USD", "leveraged-etf", 0, "LEVR"),
                                   ("PARK", "USD", "money-market", 1, "PARK")):
        lev = 3.0 if kind == "leveraged-etf" else None
        x("INSERT INTO instruments(ticker,currency,kind,leverage_factor,risk_free,"
          "quote_symbol) VALUES (?,?,?,?,?,?)", (tk, ccy, kind, lev, rf, sym))

    # THE WALL-AS-DATA BLOCK WAS HERE: principles, rules and rule_versions, seeded so
    # the loss-limit rule could be read from the book rather than hardcoded. All three
    # tables went in v14 -- the tool enforces nothing, so there is no rule to version.

    # ---- bets: one with a frozen allocation, one without.
    # FORMING FIRST, THEN THE FALSIFIER, THEN PROMOTE. A trigger enforces exactly
    # that order -- "insert a live bet as forming, add its main-support falsifier,
    # then promote" -- so a bet cannot be live for even one statement without
    # something that could refute it. The fixture obeys the wall rather than
    # bypassing it, which is the point of building from the real schema.
    x("INSERT INTO bets(id,status,opened,thesis,main_support,allocation_base,"
      "allocation_ccy,allocation_set_on) VALUES ('growth','forming','2026-02-02',"
      "'A test claim.','A test main support.',20000.0,'CAD','2026-02-02')")
    x("INSERT INTO falsifiers(bet_id,role,leg_label,wrong_if,provenance,"
      "written_on,known_by) VALUES ('growth','main-support','the test leg',"
      "'the test number goes the other way','at-entry',"
      "'2026-02-02','2026-12-31')")
    x("UPDATE bets SET status='live' WHERE id='growth'")
    x("INSERT INTO bets(id,status,opened) VALUES ('parking','forming','2026-02-02')")

    # ---- contributions, both sleeves.
    x("INSERT INTO flows(on_date,amount,kind,account_id) "
      "VALUES ('2026-02-01',30000.0,'deposit','tfsa')")
    x("INSERT INTO flows(on_date,amount,kind,account_id) "
      "VALUES ('2026-02-01',10000.0,'deposit','rrsp')")

    def trade(tid, bet, tk, ccy, acct, status, opened, closed=None):
        x("INSERT INTO trades(id,bet_id,ticker,currency,side,status,account_id,"
          "opened,closed) VALUES "
          "(?,?,?,?,'long',?,?,?,?)",
          # `ruleset_as_of` sat between `closed` and `exit_trigger` and was passed the
          # trade's own open date; dropped in v14 with the rules it pinned a version of.
          # `exit_trigger` followed in v15 -- it was 'none-written' on every close here,
          # which is what it was on 7 of the author's 11, and no screen ever read it.
          (tid, bet, tk, ccy, status, acct, opened, closed))

    def fill(tid, on_date, kind, shares, price):
        x("INSERT INTO fills(trade_id,on_date,kind,shares,price) VALUES (?,?,?,?,?)",
          (tid, on_date, kind, shares, price))

    # a closed round trip, in CAD
    trade("2026-03-02-cadco", "growth", "CADCO", "CAD", "tfsa", "closed",
          "2026-03-02", "2026-04-02")
    fill("2026-03-02-cadco", "2026-03-02", "open", 100.0, 10.00)
    fill("2026-03-02-cadco", "2026-04-02", "close", -100.0, 11.00)

    # an open USD position that has been TRIMMED, so realised and unrealised coexist
    trade("2026-05-04-usdco", "growth", "USDCO", "USD", "tfsa", "open", "2026-05-04")
    fill("2026-05-04-usdco", "2026-05-04", "open", 60.0, 100.00)
    fill("2026-05-04-usdco", "2026-06-01", "trim", -20.0, 110.00)

    # an open money-market leg under the bet that is not a bet
    trade("2026-05-04-park", "parking", "PARK", "USD", "rrsp", "open", "2026-05-04")
    fill("2026-05-04-park", "2026-05-04", "open", 50.0, 100.00)

    # AN OPEN LEG IN THE BASE CURRENCY. Without one, every open position was USD and
    # the CAD branch of positions() was exercised only by a closed trade -- so "a CAD
    # leg needs no FX rate" had no coverage at all. Entry equals the mark on purpose,
    # so it adds zero unrealised and the residual stays exactly 0.00.
    trade("2026-05-04-cadco", "parking", "CADCO", "CAD", "tfsa", "open", "2026-05-04")
    fill("2026-05-04-cadco", "2026-05-04", "open", 100.0, 11.50)

    # ---- marks and FX, all on AS_OF so there is exactly one valuation date
    for tk, amount, ccy in (("CADCO", 11.50, "CAD"), ("USDCO", 120.00, "USD"),
                            ("PARK", 100.50, "USD"), ("LEVR", 50.00, "USD")):
        x("INSERT INTO prices(on_date,base,quote,amount,kind,source) "
          "VALUES (?,?,?,?,'mark','fixture')", (AS_OF, tk, ccy, amount))
    x("INSERT INTO prices(on_date,base,quote,amount,kind,source) "
      "VALUES (?,'USD','CAD',?,'fx','fixture')", (AS_OF, FX_USDCAD))

    # ---- income, in BOTH currencies. A USD dividend was once counted at 1.00 CAD
    # per USD because the sum ignored income.currency; with no income row at all,
    # that branch was never executed by any test.
    x("INSERT INTO income(on_date,account_id,ticker,kind,amount,currency,source) "
      "VALUES (?,'tfsa','CADCO','dividend',50.0,'CAD','statement')", (AS_OF,))
    x("INSERT INTO income(on_date,account_id,ticker,kind,amount,currency,source) "
      "VALUES (?,'rrsp','PARK','interest',10.0,'USD','statement')", (AS_OF,))

    # ---- an FX conversion WITH A SPREAD. At exactly spot the term is 0.00, which
    # exercises the code and would hide a sign error; 14,000 CAD for 9,900 USD is a
    # 1.4% all-in spread, so the term is -140.00 and its sign is asserted.
    x("INSERT INTO fx_conversions(on_date,account_id,from_ccy,from_amount,to_ccy,"
      "to_amount,to_amount_source) VALUES (?,'tfsa','CAD',14000.0,'USD',9900.0,"
      "'statement')", (AS_OF,))

    # ---- balances. Chosen so the attribution residual is EXACTLY zero, which makes
    # any drift in the arithmetic show as a nonzero number rather than as a slightly
    # different wrong one.
    #   realised    CADCO +100.00 CAD; USDCO trim 20*(110-100)=200 USD -> +280.00
    #   unrealised  USDCO 40*(120-100)=800 USD -> +1120.00; PARK 50*0.50=25 -> +35.00
    #   income      50.00 CAD + 10.00 USD -> +64.00
    #   fx          9900*1.4 - 14000 -> -140.00
    #   total       100 + 280 + 1120 + 35 + 64 - 140 = 1459.00 on 40,000 contributed
    #
    # The totals are arithmetic, not a modelled cash ledger -- the fixture has no cash
    # rows -- so they are simply set to make the identity hold.
    tfsa_total = 30000.0 + 100.0 + 280.0 + 1120.0 + 64.0 - 140.0
    rrsp_total = 10000.0 + 35.0
    x("INSERT INTO account_snapshots(as_of,account_id,total,risk_free,source) "
      "VALUES (?,'tfsa',?,NULL,'statement')", (AS_OF, tfsa_total))
    x("INSERT INTO account_snapshots(as_of,account_id,total,risk_free,source) "
      "VALUES (?,'rrsp',?,?,'statement')", (AS_OF, rrsp_total, rrsp_total))
    return con


def unprice(con, trade_id: str = "2026-05-04-usdco") -> None:
    """Strip a leg's prices so it has a MARK but no cost basis.

    The state that produced the "+64.30% phantom gain": the header summed a missing
    cost as zero while the same leg's full market value went into value. Both tests
    guarding that skipped unconditionally, because a fixture where every leg is fully
    priced cannot produce it -- and a reviewer proved that reintroducing the bug
    passed the whole suite.

    Kept as a mutation rather than a fourth seeded leg so the clean fixture's residual
    stays exactly zero. An unpriced leg has market value the attribution cannot
    explain, which is a nonzero residual BY DEFINITION.
    """
    con.execute("UPDATE fills SET price=NULL, settled_base=NULL WHERE trade_id=?",
                (trade_id,))
