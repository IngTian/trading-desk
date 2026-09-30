#!/usr/bin/env python3
"""Check the book. Exits non-zero if anything is wrong.

    desk-check            # check, then print the panel
    desk-check --quiet    # check only, no panel
    desk-check --schema   # the live schema, straight from the database

THE SCHEMA IS BACK IN A FILE, and the reason it left is worth keeping. It was
removed because SQLite stores every CREATE statement VERBATIM in sqlite_master,
comments included, so a .sql file beside the database was a second copy of one
thing. That was correct while the database lived in the repo.

It stopped being correct on 2026-09-07, when the data moved out. The author: "data is data
and code is code... so this repo is pure tool instead of tool combined with data."
The schema is CODE -- 29 tables, 31 triggers, 10 views of constraints that encode
every rule this book enforces -- and code that exists only inside someone's data
file cannot be reviewed, diffed, or used to create a fresh book. So desk/schema.sql
is the source of truth, `desk-migrate --check` compares a live book against it,
and --schema below still prints what the database actually has, which is what makes
the drift visible rather than theoretical.

CHANGING THE SCHEMA: edit the book, re-extract schema.sql, add a `schema_version`
row saying what and why, and commit the .sql. SQLite has no DROP CONSTRAINT, so
altering a CHECK still needs the 12-step table rebuild -- and a rebuild can silently
drop a constraint's comments, which is now diffable instead of invisible.

WHY THIS EXISTS, and it replaces something. There used to be a JSONL dump of every
table so git diffs of the record stayed readable. The author killed it, correctly:

    "there is no way i can eyeball the diffs. so it's meaningless anyway. let's
     fulfill that by software codes and structural designs instead of relying on
     my eyeball."

A diff nobody reads is a ritual, not a safeguard. So the guarantee is delivered
here instead: a program asserts the invariants and fails loudly. That is strictly
stronger than a readable diff, because it does not depend on anyone looking.

WHAT BELONGS HERE rather than in the schema. The schema enforces everything a
CHECK or a trigger can see -- one row, or one row plus a lookup. This file holds
the invariants that are CROSS-TABLE or AGGREGATE, which SQLite cannot express as a
constraint. If a check below could be a CHECK, it should be moved into the schema
and deleted from here.
"""
from __future__ import annotations

import os
import sqlite3
import sys

# NO sys.path HACK. This file lived in book/ and had to reach up a directory to import
# desk.config, so it carried its own two-line sys.path.insert. It is a module inside the
# package now -- moved there 2026-09-30 so that `pip install trading-desk` ships the schema
# and this script, without which an installed user could not create a book at all.
#
# Run it as `python -m desk.check` (the `desk-check` console script), never as a path: a
# relative import fails when a package module is executed as a file.
from . import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The book is DATA and is not in this repo. See desk/config.py.
DB = os.fspath(config.book_path())

# (name, sql, explanation). Each query must return ZERO rows when healthy; any row
# it returns is a finding, and its first column is printed as the detail.
INVARIANTS = [
    ("an open trade with no fills",
     "SELECT id FROM trades WHERE status='open' "
     "AND NOT EXISTS (SELECT 1 FROM fills WHERE trade_id=trades.id)",
     "An open position must be made of something."),

    ("an open trade holding nothing",
     "SELECT trade_id FROM v_position p JOIN trades t ON t.id=p.trade_id "
     "WHERE t.status='open' AND p.shares_held <= 0",
     "Status says open; the fills say otherwise."),

    ("a closed trade still holding shares",
     "SELECT trade_id FROM v_position p JOIN trades t ON t.id=p.trade_id "
     "WHERE t.status='closed' AND abs(p.shares_held) > 1e-9",
     "The trigger catches this on UPDATE; this catches a row that arrived any other way."),

    # "a live bet without exactly one unrevoked main-support falsifier" WAS HERE AND WENT
    # WITH v13. It cited Rule 9(a), and there is no ruleset left to cite: the Python gate,
    # two triggers and a table CHECK enforcing the same thing were all removed at the author's
    # request -- "i think we are still enforcing rules which shouldnt be at this stage. let
    # me explore the best shape of those rules before committing too early."
    #
    # A FOURTH COPY OF THE RULE LIVED HERE, one layer out, and leaving it would have been
    # worse than the three in the database: those refused a write, which is at least visible
    # at the moment you try it, whereas this makes `check.py` exit 1 forever on a book that is
    # exactly as its owner intends. A checker that always fails is a checker you stop reading,
    # and this file's own argument is that "a store that hides its own holes is worse than no
    # store" -- which cuts both ways. An UNFALSIFIABLE bet is a real thing to be told about;
    # it is just not an INVARIANT, and the distinction is what this list is for.
    #
    # The integrity checks about falsifiers stay: a trade citing a falsifier that is not its
    # own is a corrupt reference, not a matter of process.

    # "a trade citing a falsifier that is not its own or its bet's" WAS HERE. It guarded
    # `exit_falsifier_id`, dropped 2026-09-27 with exit_trigger: 0 of 23 trades ever carried
    # one, nothing but a test wrote it, and its only reader was v_scoreable_closes -- which
    # graded whether a close was scoreable, the same grading the curve and the rules engine
    # were deleted for.

    ("an open trade under a bet that is closed or falsified",
     "SELECT t.id FROM trades t JOIN bets b ON b.id=t.bet_id "
     "WHERE t.status='open' AND b.status IN ('closed','falsified')",
     "If a bet is falsified, rule 5 says exit every trade under it. An open trade "
     "under a dead bet means that did not happen."),

    ("a fill dated before its trade opened",
     "SELECT f.trade_id FROM fills f JOIN trades t ON t.id=f.trade_id "
     "WHERE f.on_date IS NOT NULL AND t.opened IS NOT NULL AND f.on_date < t.opened",
     "Chronology."),

    ("a gap in the events sequence",
     "SELECT 'events ' || (MIN(id)) || '..' || (MAX(id)) || ' but ' || COUNT(*) || ' rows' "
     "FROM events HAVING COUNT(*) <> MAX(id) - MIN(id) + 1",
     "events is append-only. A missing id means a row was deleted despite the trigger, "
     "which is the one thing the audit log cannot survive."),

    # RULE 1 WAS HERE: a leveraged position must carry a loss limit. Its own comment
    # called it "POLICY, checked at read time", which is exactly what is going. It read
    # `v_trade_ruleset` to decide whether the rule was in force when the trade opened --
    # machinery for versioned rules, in a tool that now has none.

    # THE PRINCIPLE HERE IS WORTH STATING, because it decides fail vs note
    # throughout this file: A GAP THAT CARRIES AN OPEN CONFIRMATION IS A NOTE. A
    # GAP NOBODY RECORDED IS A FAILURE. The store's job is to surface its own
    # holes, so failing on a hole it already declared would punish the honesty;
    # what must fail is a hole nothing accounts for.
    # "a trade with no ruleset date and nothing explaining why" WAS HERE. `ruleset_as_of`
    # recorded WHICH VERSION of the rules a trade was opened under, so that a trade could
    # not be judged by a rule written after it. With no rules there is no version to
    # pin, and the `k` key that set the clock went with them.

    # OVER-DEPLOYMENT MOVED TO WARNINGS on 2026-09-27. Spending more than a bet's declared
    # allocation is a FACT worth seeing, not an invalid book -- an allocation is a plan they
    # wrote, and exceeding your own plan is a decision, not corruption. As an INVARIANT it
    # made check.py exit 1 on a book that was exactly as intended, which is the shape of
    # every rule removed from this project.

    # THE CURVE INVARIANT WAS HERE AND WENT ON 2026-09-27. It failed the book when a
    # recorded stop was looser than `clamp(5*round((100/sqrt(share_of_bet))/5), 10, 50)` --
    # so GOOG at 21.5% of its bet demanded a stop no wider than 20% -- and it demanded that
    # any departure be logged in `deviations`, a table with zero rows and no writer anywhere.
    # A wall with no door, and the fourth of that shape removed from this project.
    #
    # It never fired only because every recorded stop is NULL; it was armed for the first
    # time `s` was used with a wide one. The author: "this tool is purely recording and enforcing 0
    # rules. ZERO."
]

def _overspent(con):
    """Bets whose spend has passed their declared allocation, in the HOME currency.

    WRITTEN IN PYTHON BECAUSE THE SQL VERSION COULD NOT BE RIGHT. v_bet_deployment summed
    `shares_held * average_price` per leg in that leg's own currency and this warning divided
    the total by `allocation_base`, which is home-currency -- so on a USD-legged bet against a
    CAD plan the ratio was understated by the whole FX factor and could not fire until the bet
    was ~1.38x over. `book.bets()` already does this correctly: Bet.cost is translated, and it
    is None when any leg cannot be translated, which is the case a view has no way to express.

    A bet whose cost cannot be measured is NOT reported as within its plan. It is reported as
    unmeasurable, because "no finding" and "cannot tell" are different answers.
    """
    from desk import book  # deferred: check.py runs without the TUI
    out = []
    for b in book.bets(con):
        if not b.allocation:
            continue
        if b.cost is None:
            out.append(f"{b.id}: spend NOT MEASURABLE — "
                       f"{b.unpriced_legs} leg(s) have no recorded cost or no rate")
        elif b.cost > b.allocation * 1.0000001:
            out.append(f"{b.id} at {100.0 * b.cost / b.allocation:.1f}% "
                       f"({b.cost:,.2f} spent of {b.allocation:,.2f})")
    return out


WARNINGS = [
    ("a bet spent beyond its frozen allocation",
     lambda con: _overspent(con),
     "Worth seeing and not a defect: an allocation is a plan, and going past your own plan "
     "is a decision. Moved here from INVARIANTS on 2026-09-27, and off v_bet_deployment "
     "and into Python in v16 -- the view summed NATIVE costs against a home-currency "
     "allocation, so it printed 43.8% where the desk printed 62.1% for the same bet."),

    # "a trade whose ruleset date is unknown but recorded as a gap" WAS HERE. It read
    # v_trades_without_a_ruleset, dropped in v14 -- and its whole subject was whether a
    # trade's COMPLIANCE could be determined, which is not a question this tool asks any
    # more. It was the last reader of the ruleset machinery.

    # "a live bet with no review date" WAS HERE. It quoted rule 9(h), which is the kind of
    # thing v14 removed everywhere else, and the column it read is gone: `r` wrote a review
    # date and no screen ever showed one, so 0 of 6 bets had one and this warning fired on
    # every run for every bet. A permanent warning is noise, not a check.

    # "a falsifier that can never come due" WAS HERE AND WENT WITH IT. The author keeps the 14
    # recorded falsifiers -- they are their reasoning, and recording is what this tool is
    # for -- but nothing checks them any more. The integrity check above stays: a trade
    # citing a falsifier belonging to another bet is a corrupt reference, not a process
    # rule.

    ("an open confirmation",
     "SELECT COALESCE(trade_id,bet_id,'(book)') || ': ' || field FROM confirmations "
     "WHERE resolved_on IS NULL",
     "A store that hides its own holes is worse than no store."),

    # The expired-deviation invariant went too: `deviations` has no writer, so the
    # only way to satisfy it was hand-written SQL.
]


def run(con, group, label):
    """Each entry is (name, query, why), where `query` is SQL **or a callable**.

    THE CALLABLE FORM EXISTS BECAUSE SQL CANNOT WITHHOLD. Every figure that has to be
    comparable across currencies has to be translated first, and translation can FAIL -- a
    missing fx row means the figure is unknown, not zero and not at par. A view that hits
    that case has no way to say so: it either invents a rate or drops the row, and this
    project has been bitten by both. So a check whose subject spans currencies is written in
    Python over desk/book.py, where None already means NOT RECORDED. Returns a list of
    strings, same shape as the first column of a SQL result.
    """
    fails = 0
    for name, query, why in group:
        rows = query(con) if callable(query) else con.execute(query).fetchall()
        if rows:
            fails += 1
            print(f"  {label}: {name}  ({len(rows)})")
            for r in rows:
                print(f"      - {r if isinstance(r, str) else r[0]}")
            print(f"      why it matters: {why}")
    return fails


def dump_schema(con):
    """The live schema, from sqlite_master. Generated, never stored."""
    print("-- The schema of the book, read from sqlite_master. GENERATED --")
    print("-- do not save this to a file: a second copy would diverge. --\n")
    print("PRAGMA foreign_keys = ON;   -- per-connection, defaults OFF\n")
    for kind in ("table", "index", "trigger", "view"):
        rows = con.execute(
            "SELECT sql FROM sqlite_master WHERE type=? AND sql IS NOT NULL "
            "ORDER BY name", (kind,)).fetchall()
        if rows:
            print(f"\n-- {'=' * 70}\n-- {kind.upper()}S ({len(rows)})\n-- {'=' * 70}")
            for (sql,) in rows:
                print(sql.strip() + ";\n")


def main(quiet=False, schema=False):
    if not os.path.exists(DB):
        print(f"FAIL: no database at {DB}")
        return 1
    con = sqlite3.connect(DB)
    # sqlite3.Row, because the checks that span currencies are written in Python over
    # desk/book.py now (see run()'s docstring) and book.py reads every column BY NAME.
    # Without this the first such check died with "tuple indices must be integers" -- the
    # identical defect tests/test_book.py's `con` fixture already carries a comment about.
    # Safe for the SQL checks too: Row supports integer indexing and iteration, so `r[0]`
    # and tuple unpacking below behave exactly as before.
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")

    if schema:
        dump_schema(con)
        con.close()
        return 0

    print("=" * 74)
    print("STRUCTURE")
    print("=" * 74)
    ic = con.execute("PRAGMA integrity_check").fetchone()[0]
    print(f"  integrity_check     {ic}")
    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    print(f"  foreign_key_check   {'ok' if not fk else f'{len(fk)} VIOLATIONS'}")
    fails = (0 if ic == "ok" else 1) + (1 if fk else 0)
    for row in fk:
        print(f"      - {row}")

    print("\n" + "=" * 74)
    print("INVARIANTS — cross-table and aggregate, what a CHECK cannot see")
    print("=" * 74)
    n = run(con, INVARIANTS, "FAIL")
    fails += n
    if not n:
        print(f"  all {len(INVARIANTS)} hold")

    print("\n" + "=" * 74)
    print("ATTENTION — true findings, not failures")
    print("=" * 74)
    if not run(con, WARNINGS, "note"):
        print("  nothing outstanding")

    if not quiet:
        panel(con)

    print("\n" + "=" * 74)
    print("FAILED" if fails else "OK")
    print("=" * 74)
    con.close()
    return 1 if fails else 0


def dash(value, spec):
    """A dash for NOT RECORDED, right-aligned to the same width as the number.

    NULL is a first-class state in this book -- it means nobody wrote the figure
    down -- so every formatter that touches a nullable column has to survive it.
    """
    width = "".join(c for c in spec.split(".")[0] if c.isdigit()) or ""
    return f"{value:{spec}}" if value is not None else f"{'—':>{width or 1}}"


def panel(con):
    print("\n" + "=" * 74)
    print("THE BOOK")
    print("=" * 74)
    # ONE BALANCE PER ACCOUNT. This used to print the single latest row, which
    # reported the TFSA as if it were the whole book the moment the RRSP existed.
    accts = con.execute(
        "SELECT s.account_id, s.total, s.risk_free, s.as_of FROM account_snapshots s "
        "JOIN (SELECT account_id, MAX(as_of) m FROM account_snapshots "
        "      GROUP BY account_id) l ON l.account_id=s.account_id AND l.m=s.as_of "
        "ORDER BY s.account_id").fetchall()
    for a in accts:
        print(f"  {a[0]:<6} {a[1]:>12,.2f}   risk-free {dash(a[2], ',.2f'):>12}"
              f"   as of {a[3]}")
    if len(accts) > 1:
        # Summing across DIFFERENT as-of dates is the pooling error this whole
        # account dimension exists to prevent, so it is refused rather than fudged.
        if len({a[3] for a in accts}) == 1:
            print(f"  {'TOTAL':<6} {sum(a[1] for a in accts):>12,.2f}")
        else:
            print(f"  {'TOTAL':<6} {'—':>12}   accounts are marked as of different "
                  f"dates; no total is meaningful")
    for b in con.execute(
            "SELECT id, status, allocation_base, "
            "(SELECT COUNT(*) FROM trades t WHERE t.bet_id=bets.id AND t.status='open') "
            "FROM bets ORDER BY status, id"):
        alloc = f"{b[2]:,.0f}" if b[2] else "no allocation"
        print(f"  {b[0]:<24} {b[1]:<9} {alloc:>14}   {b[3]} open")
    print()
    # THE PER-LEG CURVE TABLE WAS HERE. It printed each leg's share of its bet, the stop
    # the curve required, and whether one was recorded -- the last place a required stop
    # was visible anywhere. The curve went on 2026-09-27, so there is nothing to print.
    # "all N stops on one day" WAS HERE, and it was the purest curve figure in the file:
    # a sum of per-leg stop losses where every stop came from the curve rather than from
    # anything recorded. It reported a number no decision had produced.
    # THE `due` BLOCK WAS HERE. v_due compared dates against `(SELECT as_of FROM clock)`, a
    # clock frozen on the day the book was created and writable by nothing since the `k` key
    # went -- so every dated obligation was silently unreachable and the count could only
    # fall. Rather than fix a stopped clock to turn three reminders back on, the reminders
    # go: "enforcing 0 rules. ZERO."
    # THE RESOLVED-CLAIM RATE WAS HERE: "N of M" closes that could be scored, read from
    # v_scoreable_closes. It is the same kind of figure as the curve table above it -- a
    # GRADE of their reasoning rather than a record of it -- and every input it had
    # (exit_trigger, exit_falsifier_id) was a field the desk could not write or show. It
    # printed "0 of 11" for as long as it existed. Dropped with the view in v15.
    attribution(con)


def attribution(con):
    """How much of the P&L the book can EXPLAIN, leg by leg.

    The balance says what the portfolio is worth. This says where that came from,
    and the residual is what the record is still missing -- a number that shrinks
    as holes get filled. A book that knows its total but cannot account for it is a
    bank statement, not a trading record.

    EVERYTHING HERE IS VALUED ON ONE DATE: the date the snapshots were taken. The
    first version reached for the newest rate and the newest mark, which made the
    residual move whenever a price row landed even though nothing about the record
    had changed. On an 18,500 USD sleeve that is 1.86 CAD per pip of USDCAD, so the
    residual was mostly FX noise and a routine `desk marks` could flip its sign and
    fire the "costs are missing" alarm with nothing missing at all.
    """
    # ---- the one valuation date, and the sleeves that must agree on it
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT as_of FROM account_snapshots s WHERE as_of = "
        "(SELECT MAX(as_of) FROM account_snapshots x WHERE x.account_id=s.account_id)")]
    asof = min(dates) if dates else None
    accts = con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    marked = con.execute(
        "SELECT COUNT(DISTINCT account_id) FROM account_snapshots").fetchone()[0]
    # `cap` is resolved AFTER asof is known, further down, so that contributions are
    # summed only up to the snapshot's own date. A deposit made after it is not in the
    # balance, and counting it manufactures a loss of exactly its own size.
    cap = None

    print("\n  where the P&L came from")
    if asof is None:
        print("      no balance recorded, so there is nothing to attribute")
        return
    print(f"      {'valued as of':<44}{asof:>12}")
    # PER CURRENCY, because `flows.amount` is native and this figure is compared to a CAD
    # balance. One blind SUM added a USD deposit at par: a single US$10,000 contribution
    # against a correct C$13,800 statement made the checker print "UNEXPLAINED +3,800.00",
    # then "nothing outstanding", then "OK", exit 0 -- six times the size of the only bet this
    # book has ever closed. A withdrawal inverted it into the false "the legs claim more
    # profit than the account holds" alarm. The income path below was already fixed for this
    # exact bug; flows were missed.
    #
    # TRANSLATED FURTHER DOWN, once to_cad exists -- it needs the rate, which is resolved
    # after this point, and routing through it is also what credits usd_exposure so the noise
    # floor knows the sleeve is there.
    cap_native = con.execute(
        "SELECT currency, COALESCE(SUM(amount),0) FROM flows WHERE on_date <= ? "
        "GROUP BY currency", (asof,)).fetchall()
    later_native = con.execute(
        "SELECT currency, COALESCE(SUM(amount),0) FROM flows WHERE on_date > ? "
        "GROUP BY currency", (asof,)).fetchall()
    # ANYTHING recorded after the snapshot makes this a comparison between a stale
    # balance and a current position, which no arithmetic below can correct.
    after = con.execute(
        "SELECT (SELECT COUNT(*) FROM fills WHERE on_date > ?), "
        "       (SELECT COUNT(*) FROM income WHERE on_date > ?)", (asof, asof)
    ).fetchone()
    if any(after):
        print(f"        STALE: {after[0]} fill(s) and {after[1]} income row(s) are "
              f"dated after this balance.")
        print(f"        The residual below compares a {asof} balance against today's "
              f"positions. Re-read the balance.")

    # ---- the rate, AS OF that date. No par fallback: an unconvertible USD sleeve
    # silently valued 1:1 would move this by thousands and still exit 0.
    fxrow = con.execute(
        "SELECT amount, on_date FROM prices WHERE kind='fx' AND base='USD' "
        "AND quote='CAD' AND on_date <= ? ORDER BY on_date DESC LIMIT 1",
        (asof,)).fetchone()
    # USD MONEY ENTERS BY THREE DOORS, and this asked about only one. `trades` was the
    # whole test, so a book holding a USD sleeve funded by a conversion, or paying USD
    # interest, but with no USD POSITION yet, fell through to a par rate -- in the very
    # function whose comment above promises there is no par fallback.
    #
    # Proven: a day-one book with a 14,000 CAD -> 9,900 USD conversion and no positions
    # printed "currency conversion, spread and FX since -4,100.00" (the truth at 1.40 is
    # -140.00) and "UNEXPLAINED +3,960.00", then said "all 12 hold" and exited 0.
    #
    # And that state is not a corner: prices.py only ever requests the USD/CAD pair for
    # the currencies of trades with status='open', so with no USD trade open `desk marks`
    # will never fetch the rate and cannot repair it.
    # FOUR DOORS, NOT THREE. `flows` was missing, so a book funded by a USD deposit with no
    # USD position, income or conversion still fell through to the par rate this comment
    # promises does not exist -- and the sum above would then add that deposit at 1.0.
    has_usd = con.execute(
        "SELECT 1 WHERE EXISTS (SELECT 1 FROM trades WHERE currency='USD') "
        "   OR EXISTS (SELECT 1 FROM income WHERE currency='USD') "
        "   OR EXISTS (SELECT 1 FROM flows WHERE currency='USD') "
        "   OR EXISTS (SELECT 1 FROM fx_conversions "
        "                WHERE to_ccy='USD' OR from_ccy='USD')").fetchone() is not None
    if fxrow is None and has_usd:
        print("      the USD sleeve cannot be converted: no USD/CAD rate on or "
              "before this date")
        print("      so no attribution is printed -- a par rate would be wrong by "
              "the whole FX factor")
        return
    # A THIRD CURRENCY WOULD BE BOOKED AT PAR. to_cad below returns anything that is not
    # USD unchanged, so a GBP leg was valued 1 GBP = 1 CAD -- while the income path
    # (line 457) and the conversion path (line 490) both already refuse a currency they
    # cannot convert. Only the trades path was silent, and it moved unrealised by the
    # whole notional: a 2,000 GBP gain became 2,000 CAD and then printed "NEGATIVE: the
    # legs claim 2,000.00 more profit than the account holds".
    odd = [r[0] for r in con.execute(
        "SELECT DISTINCT currency FROM trades "
        "WHERE currency NOT IN ('CAD', 'USD') ORDER BY currency")]
    if odd:
        print(f"      no attribution is printed: {', '.join(odd)} "
              f"{'legs are' if len(odd) > 1 else 'leg is'} in a currency this "
              f"attribution cannot convert")
        print("      valuing them at par would be wrong by the whole FX factor")
        return

    fx = fxrow[0] if fxrow else 1.0
    if fxrow and fxrow[1] != asof:
        print(f"        rate is {fxrow[1]}'s, the newest on or before {asof}")

    # Every USD amount that passes through here is multiplied by ONE rate, so the
    # total of them is the residual's exact sensitivity to that rate: move fx by one
    # pip and the residual moves by usd_exposure/10000. That is the honest noise
    # floor, and it is accumulated rather than re-queried because the first version
    # summed only the open positions -- missing the USD cash and the whole
    # conversion amount, which together are more than half of it.
    usd_exposure = 0.0

    def to_cad(amount, ccy):
        nonlocal usd_exposure
        if ccy != "USD":
            return amount
        usd_exposure += abs(amount)
        return amount * fx

    # ---- contributed capital, now that the rate and to_cad exist -------------
    cap = sum(to_cad(amt, ccy) for ccy, amt in cap_native)
    later = sum(to_cad(amt, ccy) for ccy, amt in later_native)
    if later:
        print(f"        {later:+,.2f} of contributions arrived AFTER this date and are "
              f"not counted in it")

    # ---- realised, per leg, average cost
    realised, incomplete = 0.0, []
    for t in con.execute("SELECT id, currency FROM trades"):
        # NOT filtered by date, deliberately. Realised must use the same fill set
        # the UNREALISED term's position comes from, and that position comes from
        # v_position, which is not date-aware. Filtering here alone excluded a late
        # sale from realised while it still shrank the position -- moving the residual
        # by the size of the term it was meant to protect. The mismatch is reported
        # instead; see the "recorded after this date" warning above.
        fs = con.execute("SELECT shares, price, settled_base FROM fills "
                         "WHERE trade_id=? ORDER BY id", (t[0],)).fetchall()
        if not fs or not any(f[0] < 0 for f in fs):
            continue                       # nothing sold yet, so nothing realised
        if any(f[1] is None and f[2] is None for f in fs):
            incomplete.append(t[0])
            continue
        # settled_base is the EXACT cash when the statement gave it, and by its own
        # column definition it is already in BASE currency. shares*price is in TRADE
        # currency. Mixing them and converting the result once double-converts a USD
        # leg that has settled_base filled in, so the two are summed separately and
        # only the trade-currency part is converted.
        buys = [f for f in fs if f[0] > 0]
        sells = [f for f in fs if f[0] < 0]
        bought = sum(f[0] for f in buys)
        sold = -sum(f[0] for f in sells)
        if not bought:
            incomplete.append(t[0])
            continue

        def split(rows):
            """(base-currency cash, trade-currency cash) for a set of fills."""
            base = sum(f[2] for f in rows if f[2] is not None)
            trade = sum(abs(f[0] * f[1]) for f in rows if f[2] is None)
            return base, trade

        b_base, b_trade = split(buys)
        s_base, s_trade = split(sells)
        frac = sold / bought                       # average cost, per unit sold
        realised += (s_base - b_base * frac) + to_cad(s_trade - b_trade * frac, t[1])

    # ---- income, IN ITS OWN CURRENCY. A USD distribution lands in the USD sleeve
    # and therefore reaches the CAD snapshot total at spot, exactly like the legs.
    # Summing it currency-blind counted a USD dollar as a Canadian one.
    inc, odd_ccy = 0.0, set()
    for amount, ccy in con.execute(
            "SELECT amount, currency FROM income WHERE on_date <= ?", (asof,)):
        if ccy not in ("CAD", "USD"):
            odd_ccy.add(ccy)
            continue
        inc += to_cad(amount, ccy)

    # ---- unrealised, at the mark AS OF the same date
    unreal, unpriced = 0.0, []
    for r in con.execute(
            # AND quote=t.currency -- see desk/book.py's positions(). Without it this
            # checker could read a mark in the wrong currency and exit 0 on it.
            "SELECT t.ticker, t.currency, p.shares_held, p.average_price, "
            "(SELECT amount FROM prices WHERE kind='mark' AND base=t.ticker "
            "  AND quote=t.currency AND on_date <= :asof "
            "  ORDER BY on_date DESC LIMIT 1) mk "
            "FROM trades t JOIN v_position p ON p.trade_id=t.id "
            "WHERE t.status='open' AND p.shares_held > 0", {"asof": asof}):
        if r[3] is None or r[4] is None:
            unpriced.append(r[0])
            continue
        unreal += to_cad((r[4] - r[3]) * r[2], r[1])

    # ---- THE FX TERM, in both directions.
    # Contributions are CAD; some became USD, and every USD leg above is valued at
    # spot. What those USD COST in CAD lives here and nowhere else, so without this
    # both the conversion spread and every move in USDCAD are invisible.
    #   sleeve_USD x spot - CAD_out == (received x spot - CAD_out) + legs x spot
    # is exact, so this does not double count what the legs already say. Filtering
    # to CAD->USD only, as the first version did, dropped a USD->CAD conversion
    # entirely -- no term, no warning -- and then blamed the gap on a missing loss.
    fxterm, derived, missing_fx, unvaluable = 0.0, False, 0, set()
    for f_ccy, f_amt, t_ccy, t_amt, src in con.execute(
            "SELECT from_ccy, from_amount, to_ccy, to_amount, to_amount_source "
            "FROM fx_conversions WHERE on_date IS NULL OR on_date <= ?", (asof,)):
        if t_amt is None:
            missing_fx += 1
            continue
        if {f_ccy, t_ccy} != {"CAD", "USD"}:
            unvaluable.add(f"{f_ccy}->{t_ccy}")
            continue
        fxterm += to_cad(t_amt, t_ccy) - to_cad(f_amt, f_ccy)
        derived = derived or src == "derived"

    explained = realised + inc + unreal + fxterm
    print(f"      {'realised on closed and trimmed legs':<44}{realised:>+12,.2f}")
    print(f"      {'dividends and lending income':<44}{inc:>+12,.2f}")
    print(f"      {'unrealised on open legs at the mark':<44}{unreal:>+12,.2f}")
    if fxterm or missing_fx or unvaluable:
        print(f"      {'currency conversion, spread and FX since':<44}{fxterm:>+12,.2f}")
        if derived:
            print("        BACKED OUT of the sleeve, so the residual does not test it")
        if missing_fx:
            print(f"        {missing_fx} conversion(s) with no destination amount "
                  f"recorded, so their cost is invisible")
        if unvaluable:
            print(f"        NOT VALUED, no rate for: {', '.join(sorted(unvaluable))}")
    if odd_ccy:
        print(f"        income excluded, no rate for: {', '.join(sorted(odd_ccy))}")
    print(f"      {'':<44}{'-'*12}")
    print(f"      {'explained':<44}{explained:>+12,.2f}")

    # ---- actual. Refuses the same cross-date pooling panel() refuses: summing
    # sleeves read on different days is not a portfolio total at any moment.
    if not cap:
        print(f"      {'actual':<44}{'—':>12}   no contributions on or before {asof}")
        return
    if accts != marked:
        print(f"      {'actual':<44}{'—':>12}   a sleeve has no balance at all")
        return
    if len(set(dates)) > 1:
        print(f"      {'actual':<44}{'—':>12}   sleeves were read on "
              f"{len(set(dates))} different dates ({', '.join(sorted(set(dates)))}), "
              f"so no total is a moment")
        return

    total = con.execute("SELECT SUM(total) FROM account_snapshots WHERE as_of=?",
                        (asof,)).fetchone()[0]
    actual = total - cap
    gap = actual - explained
    print(f"      {'actual, balance minus contributions':<44}{actual:>+12,.2f}")
    print(f"      {'':<44}{'-'*12}")
    print(f"      {'UNEXPLAINED':<44}{gap:>+12,.2f}")

    # A residual below its own resolution is not a clean book, it is a number that
    # cannot mean anything. FIVE PIPS of USDCAD is the floor: a broker and Yahoo
    # do not quote the same rate, and the balance is a figure read off an app by
    # hand, so agreement closer than that is luck rather than bookkeeping.
    #
    # No `* fx` here on purpose -- usd_exposure is in USD and a pip is CAD per USD,
    # so the product is already CAD. Multiplying again inflated the floor by 38%.
    floor = max(1.0, 0.0005 * usd_exposure)
    if usd_exposure:
        # Printed ALWAYS, not only when the residual is inside it. The number is
        # meaningless without its resolution, and a reader who sees only "+11.93"
        # will read three significant figures into it.
        print(f"        {abs(gap)/floor*5:.1f} pips of USDCAD on {usd_exposure:,.0f} "
              f"USD of exposure; 5 pips = {floor:,.2f} is the noise floor")
    if abs(gap) <= floor:
        print("        WITHIN the noise floor — a broker and Yahoo do not quote "
              "the same rate, and the balance is hand-read")
    elif gap < 0:
        # THE SIGN MATTERS. A positive residual means gains the legs do not account
        # for, usually a fill nobody wrote down. A NEGATIVE residual is the
        # dangerous direction: the legs claim profit the account does not have, so
        # recorded COSTS are incomplete and every per-leg verdict about whether a
        # strategy worked is flattered by exactly that much.
        print(f"        NEGATIVE: the legs claim {-gap:,.2f} more profit than the "
              f"account holds.")
        print("        Costs are missing, not gains. Fees, a conversion, or a loss "
              "nobody recorded.")
    if incomplete:
        print(f"        legs with a fill price nobody recorded: "
              f"{', '.join(incomplete)}")
    if unpriced:
        print(f"        open legs with no entry or no mark: "
              f"{', '.join(sorted(set(unpriced)))}")


def cli() -> int:
    """The `desk-check` console script. Reads its own flags off sys.argv.

    A SEPARATE FUNCTION because setuptools calls an entry point with NO arguments and then
    `sys.exit()`s whatever it returns, while main() takes its flags as keywords -- so
    pointing the script at main() directly would silently ignore --quiet and --schema.
    Kept as flags rather than argparse to match what the __main__ block below has always
    accepted, so a command anyone has in their shell history still works.
    """
    if any(a not in ("--quiet", "--schema") for a in sys.argv[1:]):
        print("desk-check [--quiet] [--schema]\n"
              "  --quiet    the invariants only, no panel\n"
              "  --schema   print the LIVE schema, read from the database",
              file=sys.stderr)
        return 2
    return main(quiet="--quiet" in sys.argv, schema="--schema" in sys.argv)


if __name__ == "__main__":
    sys.exit(cli())
