"""Data access over the book. Pure Python, no Textual.

The separation is absolute and copied from daylogs: the TUI renders and handles
keys; every number is computed here, where it can be tested without an app. A pane
that grows arithmetic is a bug.

Two things this module will not do:

  * It will not invent a number. Where the book records nothing, these functions
    return None and the caller must say "not recorded" rather than substitute a
    zero. That is the whole reason the database allows NULL where it does.
  * It will not write outside the tables the desk owns. The walls live in the
    database as CHECKs and triggers, so a bad write raises sqlite3.IntegrityError
    with the wall's own message, and that message is what the user sees.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import os
import sqlite3
from dataclasses import dataclass

from . import config, parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DeskError(ValueError):
    """Shown verbatim to the user, so it reads as guidance.

    AND IT HAS TO FIT ONE LINE. The prompt renders it in the border subtitle, which is where
    the grammar sits and is exactly as wide as the terminal -- five prompt grammars were being
    clipped mid-word before that was measured. An error long enough to clip loses its own
    actionable half, so anything that needs a list belongs in a toast beside it.
    """


class AmbiguousSymbol(DeskError):
    """More than one listing answers to this ticker, and they are different securities.

    CARRIES THE CANDIDATES rather than formatting them into its message, because the two go to
    different places: a short sentence into the prompt's one-line subtitle, and the full list
    into a toast that can afford several lines. Building one string for both clipped the part
    that said what to do.
    """

    def __init__(self, ticker: str, candidates: list[dict]) -> None:
        self.ticker = ticker
        self.candidates = candidates
        super().__init__(
            f"{ticker} matches {len(candidates)} different securities — tab through them "
            f"and pick the one you hold")

    def detail(self) -> str:
        """What each candidate IS, so the choice is informed rather than a coin toss.

        The symbols themselves are what `tab` cycles in the prompt; this says which security
        each one is, which is the part a symbol cannot tell you. NOT A SUGGESTED DEFAULT: an
        earlier version ended with the first candidate's line, and for CBIL that is the
        American fund -- recommending the wrong one in the very message whose point is that
        the tool cannot choose.
        """
        rows = [f"{self.ticker} names {len(self.candidates)} different securities:"]
        for c in self.candidates:
            rows.append(f"  {c['quote_symbol']:<10} {c['currency']}  "
                        f"{c['name'] or 'no name'} on "
                        f"{c['exchange'] or 'an unknown exchange'}")
        return "\n".join(rows)


def connect(path: str | None = None) -> sqlite3.Connection:
    # The book is DATA and lives outside the repo. config resolves it from an
    # explicit path, then $TRADING_DESK_BOOK, then the legacy in-repo location.
    p = os.fspath(config.book_path(path))
    if not os.path.exists(p):
        raise DeskError(
            f"no book at {p}\n"
            f"  create one:      desk-migrate --new\n"
            f"  or point at one: export TRADING_DESK_BOOK=/path/to/hub.db")
    con = sqlite3.connect(p, isolation_level=None)     # autocommit
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")            # per-connection, defaults OFF
    con.execute("PRAGMA journal_mode = DELETE")        # never WAL, per daylogs
    # WAIT FOR A CONCURRENT WRITER RATHER THAN REFUSING THE USER'S WRITE. Python's default
    # is 5 seconds, which was not enough on 2026-09-25: the 60-second price poll held a
    # write lock across its whole fetch run, so a fill submitted in that window came back as
    # a raw `OperationalError: database is locked`. That cause is fixed in prices.py, but the
    # timeout is the belt to its braces -- the desk holds its own locks for milliseconds, so
    # fifteen seconds of waiting can only mean another process, and failing a fill because
    # something else was mid-write is the one outcome this tool must not produce.
    con.execute("PRAGMA busy_timeout = 15000")
    return con


def set_actor(con: sqlite3.Connection, actor: str) -> None:
    """The events log reads this. Set by the app, never by the caller of a write."""
    con.execute("UPDATE session SET actor = ?", (actor,))


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Trade:
    trade_id: str
    bet_id: str
    ticker: str
    currency: str
    qty: float
    entry: float | None
    opened: str | None
    stop: float | None
    target: float | None
    loss_limit_pct: float | None
    # curve_stop_pct AND share_of_bet_pct WERE HERE, fed by v_leg_required_stop. The curve
    # they carried -- clamp(5*round((100/sqrt(share))/5), 10, 50) -- was deleted on
    # 2026-09-27. The author: "i think you can delete the curve." Nothing had read curve_stop_pct
    # since the TUI stopped rendering it.
    mark: float | None
    mark_date: str | None
    # None means NO RATE RECORDED, not parity. A USD leg valued 1:1 in a CAD book is
    # wrong by the whole FX factor -- ~38% here -- and looks like a real number.
    # attribution() already refuses this; positions() used to default it to 1.0.
    fx: float | None
    unconfirmed_fills: int
    # NO `realised` FIELD, and that is the author's design, 2026-11-02: "when i close 80
    # shares of INTC, why dont we just log that in closed positions. i think that's easier
    # than banked." An exit is a closed trade, so it is a row in closed_legs() and nowhere
    # else. A field here would be the same money in two places, which is how the `CLOSED`
    # header came to exclude a figure printed directly beneath it.
    account: str | None = None

    @property
    def base_currency_matches(self) -> bool:
        """A CAD leg in a CAD book needs no rate, so its absence is not a hole."""
        return self.fx == 1.0

    # THE LEG IN ITS OWN CURRENCY. The author chose these for the positions table over the
    # translated `_base` figures below, and the reason is that a row has to add up: with
    # `value_base` a USD leg read `1 x 815.56 = 1,126.04`, which is not arithmetic anybody
    # can follow. Native, the row is self-consistent and the `ccy` column governs all of
    # it. Totals still translate -- they sum across currencies and cannot do otherwise --
    # so the rule is ROWS NATIVE, TOTALS HOME.
    #
    # NO RATE IS NEEDED HERE, which is the part that is better rather than merely
    # different. `_base` withholds everything when `fx` is None, so a USD leg with no
    # recorded rate showed no value, no P&L and no percentage -- three blanks for figures
    # the book knows perfectly well, since qty and the mark are both recorded in USD. This
    # project withholds what it does not know; withholding what it does know is the same
    # error wearing the opposite coat.
    @property
    def cost_native(self) -> float | None:
        return None if self.entry is None else self.qty * self.entry

    @property
    def value_native(self) -> float | None:
        return None if self.mark is None else self.qty * self.mark

    @property
    def pnl_native(self) -> float | None:
        c, v = self.cost_native, self.value_native
        return None if c is None or v is None else v - c

    @property
    def pnl_pct_native(self) -> float | None:
        """Identical to pnl_pct wherever both exist -- fx cancels in a ratio -- and defined
        in the one case pnl_pct is not, which is why it is the one the table uses."""
        c, p = self.cost_native, self.pnl_native
        return None if not c or p is None else 100.0 * p / c

    @property
    def cost_base(self) -> float | None:
        if self.entry is None or self.fx is None:
            return None
        return self.qty * self.entry * self.fx

    @property
    def value_base(self) -> float | None:
        if self.mark is None or self.fx is None:
            return None
        return self.qty * self.mark * self.fx

    @property
    def pnl_base(self) -> float | None:
        c, v = self.cost_base, self.value_base
        return None if c is None or v is None else v - c

    @property
    def pnl_pct(self) -> float | None:
        c = self.cost_base
        p = self.pnl_base
        return None if not c or p is None else 100.0 * p / c


def base_currency(con) -> str:
    """The currency every figure is quoted in. The author's, from the profile.

    IT USED TO BE READ FROM THE FIRST BET THAT HAPPENED TO CARRY AN allocation_ccy --
    `SELECT allocation_ccy FROM bets ... LIMIT 1`, with no ORDER BY, so the currency the
    whole dashboard was denominated in depended on SQLite's row order and would have
    flipped the moment a USD-allocated bet sorted first. The author asked for a profile
    instead: "inside the profile, you need my name, and my home currency."

    `con` is still taken. This is a fact about the person, not the book, but every caller
    already has a connection and none of them should have to learn that.
    """
    return config.profile().home_currency


def fx_rate(con, frm: str, to: str, *, on: str | None = None
            ) -> tuple[float | None, str | None]:
    """(rate, the date it was read). None means NO RATE RECORDED -- never parity.

    A USD leg valued 1:1 in a CAD book is wrong by the whole factor, ~38% here, and
    looks like an ordinary number doing it.

    THE INVERSE COUNTS. The fetcher records USD->CAD because that is the pair Yahoo
    quotes, so a book whose home currency is USD would find nothing and blank its own
    dashboard. 1/rate is the same fact written the other way round.
    """
    if frm == to:
        return 1.0, on
    for a, b, invert in ((frm, to, False), (to, frm, True)):
        sql = ("SELECT amount, on_date FROM prices WHERE kind='fx' AND base=? AND quote=?"
               + ("" if on is None else " AND on_date<=?")
               + " ORDER BY on_date DESC LIMIT 1")
        args = (a, b) if on is None else (a, b, on)
        row = con.execute(sql, args).fetchone()
        if row is not None:
            rate = float(row[0])
            return (1.0 / rate if invert else rate), row[1]
    return None, None


# --------------------------------------------------------------------------- #
# what the account is worth, DERIVED
#
# The author, 2026-09-09: "the net should be computed from cash + positions right? why do you
# bookkeeping a number i did. theoretically, from now on, i only enter deposits, fees,
# or something minor adjustments, and trades and you should get roughly the same number
# as the broker says."
#
# So there is no statement total any more. Net is cash plus what the positions are
# marked at, and cash is what every recorded movement leaves behind.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Sleeve:
    """One currency: its cash, its positions, and the one rate that translates both.

    CASH IS NATIVE AND IS NOT TRANSLATED PER MOVEMENT, which is what makes this work
    without any FX history at all. A CAD deposit is CAD; a USD buy spends USD; a
    recorded conversion moves the money across. Only the CURRENT balance is translated,
    at today's rate -- exactly what a broker statement does.

    That is also why it is right rather than merely convenient. Translating each past
    movement at today's rate would have cancelled the FX gain on the principal: cash out
    and position in are the same USD figure, so `-cost*fx_now + value*fx_now` loses
    `cost * (fx_then - fx_now)` -- a real component, and about C$410 on a 3% move
    against a US$10,000 sleeve, scaling with the sleeve. Holding it native and translating
    the whole of it at once keeps that in.
    """
    currency: str
    positions: float | None          # market value, in THIS currency
    positions_cost: float | None     # what those legs cost, in THIS currency
    fx: float | None                 # -> home currency
    fx_date: str | None
    # THE FIVE MOVEMENTS, KEPT SEPARATELY rather than pre-added into `cash`. They are
    # what makes the return decomposable: `traded + positions` is total trading P&L, and
    # the conversion term is the FX gain on the principal. Adding them up early threw
    # that away and left the dashboard with a total it could not explain.
    flows: float = 0.0               # deposits net of withdrawals
    offsets: float = 0.0             # the `o` rows
    income: float = 0.0              # dividends, interest, stock lending
    converted: float = 0.0           # signed: negative out of this currency, positive in
    traded: float | None = None      # cash a buy took and a sell returned, net of fees
    # This sleeve's flows translated at THEIR OWN dates. Held here because it is the
    # only figure in the type that is not as-of-now, and the difference between it and
    # `flows * fx` IS the revaluation of contributed capital.
    contributed_home: float | None = None
    mark_date: str | None = None     # the OLDEST mark behind `positions`
    unknown_because: tuple[str, ...] = ()   # why cash or value is None, in words

    @property
    def cash(self) -> float | None:
        """What every recorded movement leaves behind, in THIS currency."""
        if self.traded is None:
            return None
        return (self.flows + self.offsets + self.income + self.converted
                + self.traded)

    @property
    def trading(self) -> float | None:
        """Total P&L from trading in this currency: realised AND unrealised, together.

        EXACT, AND WITH NO AVERAGE-COST AMBIGUITY, which is why the decomposition on the
        dashboard ties. `traded` is every buy paid for and every sale received; `positions`
        is what is still held. For a fully closed trade the second term is zero and this is
        the realised figure. For an open one it is market value minus cost, plus any trim
        already banked. Summed over a currency, the two together are the whole answer
        without ever needing to decide which shares were sold.

        A first attempt built the split the other way -- realised from closed trades,
        unrealised from cost basis -- and it misses by the size of every trim taken on a
        leg that is still open, because such a trim belongs to neither bucket.
        """
        if self.traded is None or self.positions is None:
            return None
        return self.traded + self.positions

    @property
    def unrealised(self) -> float | None:
        if self.positions is None or self.positions_cost is None:
            return None
        return self.positions - self.positions_cost

    @property
    def realised(self) -> float | None:
        """Trading P&L minus the part still open. DERIVED AS THE REMAINDER, deliberately.

        Computing it independently would let realised + unrealised disagree with trading,
        and a split that does not add up to the total it splits is worse than no split.
        """
        if self.trading is None or self.unrealised is None:
            return None
        return self.trading - self.unrealised

    @property
    def currency_effect(self) -> float | None:
        """What the exchange rate did, in home currency. Zero for the home sleeve.

        Two parts, and both are real money:

          * `converted * fx` -- C$10,000 became US$7,130.12 at 1.4025, and at 1.3772
            that is C$9,819.60. The C$180.40 difference is a loss, and the naive
            translate-everything-at-today's-rate version silently dropped it.
          * `flows * fx - contributed_home` -- a deposit made in a foreign currency is
            worth something different now than when it arrived.

        ONLY THE SUM ACROSS SLEEVES MEANS ANYTHING. For that conversion the CAD sleeve's
        figure is -10,000.00 and the USD sleeve's is +9,819.60, because a conversion is one
        event recorded as two halves. Printing either alone would be alarming and false. Net
        sums them; nothing should show this per currency.
        """
        if self.fx is None or self.contributed_home is None:
            return None
        return self.converted * self.fx + self.flows * self.fx - self.contributed_home

    @property
    def priced_on(self) -> str | None:
        """The oldest price behind any figure in this sleeve -- mark or rate.

        The oldest, because that is how current the sleeve really is. Reporting the
        newest would let one freshly-fetched rate vouch for a mark a week old.
        """
        dates = [d for d in (self.mark_date, self.fx_date) if d]
        return min(dates) if dates else None

    def _home(self, amount: float | None) -> float | None:
        if amount is None or self.fx is None:
            return None
        return amount * self.fx

    @property
    def cash_home(self) -> float | None:
        return self._home(self.cash)

    @property
    def positions_home(self) -> float | None:
        return self._home(self.positions)

    @property
    def positions_cost_home(self) -> float | None:
        return self._home(self.positions_cost)

    @property
    def trading_home(self) -> float | None:
        return self._home(self.trading)

    @property
    def realised_home(self) -> float | None:
        return self._home(self.realised)

    @property
    def unrealised_home(self) -> float | None:
        return self._home(self.unrealised)

    @property
    def income_home(self) -> float | None:
        return self._home(self.income)

    @property
    def offsets_home(self) -> float | None:
        return self._home(self.offsets)

    @property
    def total(self) -> float | None:
        """Cash plus positions, in this currency. None if either is unrecorded."""
        if self.cash is None or self.positions is None:
            return None
        return self.cash + self.positions

    @property
    def total_home(self) -> float | None:
        return self._home(self.total)


@dataclass(frozen=True)
class Net:
    """What the account is worth, and how it got there. All in `home`.

    Every total is None rather than partial. A net that quietly omits the sleeve it
    could not translate is not a conservative estimate, it is a wrong number -- the
    same rule Pooled was built around, kept now that the statement it pooled is gone.
    """
    home: str
    sleeves: tuple[Sleeve, ...]
    contributed: float | None            # deposits net of withdrawals, in home
    unknown: tuple[str, ...] = ()        # every reason a figure below is None

    def _sum(self, attr: str) -> float | None:
        vals = [getattr(s, attr) for s in self.sleeves]
        return None if any(v is None for v in vals) else sum(vals)

    @property
    def cash(self) -> float | None:
        return self._sum("cash_home")

    @property
    def positions(self) -> float | None:
        return self._sum("positions_home")

    @property
    def positions_cost(self) -> float | None:
        """What the open legs cost, translated. The other half of unrealised P&L.

        HERE RATHER THAN IN THE PANE. panes.py computed this, with an `or 0.0` inside a
        sum -- the exact shape that once printed a phantom +11,760 gain on a book whose
        real open-leg P&L was +255. _sum() withholds instead, and the pane has no
        arithmetic to get wrong.
        """
        return self._sum("positions_cost_home")

    @property
    def net(self) -> float | None:
        return self._sum("total_home")

    @property
    def pnl(self) -> float | None:
        """Net minus what was put in. The whole scoreboard, in one number.

        NO as_of GYMNASTICS. The old loss figure subtracted a statement dated some day D
        from contributions summed up to D, because a deposit after D otherwise
        manufactured a loss of exactly its own size. Derived net has no such date: it
        already contains every deposit, so the two sides are the same moment by
        construction. That whole class of bug is gone rather than guarded.
        """
        if self.net is None or self.contributed is None:
            return None
        return self.net - self.contributed

    @property
    def pnl_pct(self) -> float | None:
        if not self.contributed or self.pnl is None:
            return None
        return 100.0 * self.pnl / self.contributed

    # -- where the return came from ------------------------------------------ #
    # These four SUM TO pnl EXACTLY, and `ties()` asserts it rather than trusting the
    # algebra. That matters because a split which does not add up to the total it splits
    # sends the reader looking for a missing trade that does not exist.
    @property
    def realised(self) -> float | None:
        return self._sum("realised_home")

    @property
    def unrealised(self) -> float | None:
        return self._sum("unrealised_home")

    @property
    def trading(self) -> float | None:
        """Realised and unrealised together. The line to print when the split is unknown.

        A leg with no recorded entry price has no unrealised figure, so realised cannot be
        separated out either -- but the PAIR is still exact, because it needs no cost
        basis. So the dashboard degrades to one line instead of blanking three.
        """
        return self._sum("trading_home")

    @property
    def income(self) -> float | None:
        return self._sum("income_home")

    @property
    def offsets(self) -> float | None:
        return self._sum("offsets_home")

    @property
    def currency_effect(self) -> float | None:
        return self._sum("currency_effect")

    def ties(self, tolerance: float = 0.01) -> bool:
        """Does the split add up to the total? Asserted in a test and checked before print.

        The tolerance is a cent, not zero: `prices` stores float32 marks, so a mark
        round-trips as 100.47000122070312 and the two sides differ in the last places.
        """
        parts = (self.trading, self.income, self.offsets, self.currency_effect)
        if self.pnl is None or any(p is None for p in parts):
            return False
        return abs(sum(parts) - self.pnl) <= tolerance

    @property
    def as_of(self) -> str | None:
        """The OLDEST price behind any figure here, which is how current it really is."""
        dates = [s.priced_on for s in self.sleeves if s.priced_on]
        return min(dates) if dates else None


def _by_currency(con, sql: str, args: tuple = ()) -> dict[str, float]:
    return {r[0]: float(r[1]) for r in con.execute(sql, args) if r[1] is not None}


def net(con) -> Net:
    """Cash plus positions, per currency, translated to the home currency.

    THE FIVE THINGS THAT MOVE CASH, and there is no sixth: a deposit or withdrawal, the
    offset row, dividends and interest, a currency conversion, and a trade. Each lives
    in its own table and none overlaps another, so this is a sum rather than a
    reconciliation.
    """
    home = base_currency(con)

    flows = _by_currency(con, "SELECT currency, SUM(amount) FROM flows GROUP BY currency")
    offs = _by_currency(con,
                        "SELECT currency, SUM(amount) FROM adjustments GROUP BY currency")
    inc = _by_currency(con, "SELECT currency, SUM(amount) FROM income GROUP BY currency")
    # A CONVERSION IS TWO MOVEMENTS. Out of the currency sold, into the one bought --
    # and it is what lets the sleeves stay native: a USD side can exist only because some
    # CAD was sold for it -- C$10,000 becoming US$7,130.12, say. Without the row the USD
    # sleeve reads as having spent money it never had.
    sold = _by_currency(con, "SELECT from_ccy, -SUM(from_amount) FROM fx_conversions "
                             "GROUP BY from_ccy")
    bought = _by_currency(con, "SELECT to_ccy, SUM(to_amount) FROM fx_conversions "
                               "WHERE to_amount IS NOT NULL GROUP BY to_ccy")
    # A BUY TAKES CASH, A SELL RETURNS IT, AND THE SIGN DOES THAT ON ITS OWN: `shares`
    # is negative on a trim, so -(shares*price) is positive there. Fees always leave.
    traded = _by_currency(con, """
        SELECT t.currency, -SUM(f.shares * f.price) - SUM(COALESCE(f.fee, 0))
          FROM fills f JOIN trades t ON t.id = f.trade_id
         WHERE f.price IS NOT NULL
         GROUP BY t.currency""")

    unpriced = _by_currency(con, """
        SELECT t.currency, COUNT(*) FROM fills f JOIN trades t ON t.id = f.trade_id
         WHERE f.price IS NULL GROUP BY t.currency""")
    unconverted = {r[0] for r in con.execute(
        "SELECT to_ccy FROM fx_conversions WHERE to_amount IS NULL")}

    # CONTRIBUTED IS TRANSLATED AT THE RATE ON THE DEPOSIT'S OWN DATE, not today's.
    # It is a historical fact -- what they actually put in -- and revaluing it every time
    # the loonie moves would make the return move with no deposit and no trade. The
    # difference between this and `flows * fx` is exactly Sleeve.currency_effect.
    contributed_home: dict[str, float | None] = {}
    for r in con.execute("SELECT on_date, currency, SUM(amount) AS amount FROM flows "
                         "GROUP BY on_date, currency"):
        ccy = r["currency"]
        rate, _ = fx_rate(con, ccy, home, on=r["on_date"])
        if rate is None:
            contributed_home[ccy] = None
        elif contributed_home.get(ccy, 0.0) is not None:
            contributed_home[ccy] = contributed_home.get(ccy, 0.0) + r["amount"] * rate

    pos = trades(con)
    held = {p.currency for p in pos}
    reasons: list[str] = []
    sleeves: list[Sleeve] = []

    for ccy in sorted({home, *flows, *offs, *inc, *sold, *bought, *traded, *held,
                       *unpriced, *unconverted}):
        why: list[str] = []
        moved: float | None = traded.get(ccy, 0.0)
        if ccy in unpriced:
            why.append(f"{_n(int(unpriced[ccy]), 'fill', 'have')} no price, "
                       f"so {ccy} cash is short by whatever they cost")
            moved = None
        converted = sold.get(ccy, 0.0) + bought.get(ccy, 0.0)
        if ccy in unconverted:
            why.append(f"a conversion into {ccy} has no amount recorded")
            moved = None

        legs = [p for p in pos if p.currency == ccy]
        # NATIVE, so `fx` is deliberately not applied here -- Trade.value_base
        # already translates and this must not translate twice.
        if any(p.mark is None for p in legs):
            why.append("no mark: " + " ".join(
                sorted({p.ticker for p in legs if p.mark is None})))
            value = None
        else:
            value = sum(p.qty * p.mark for p in legs)
        cost = (None if any(p.entry is None for p in legs)
                else sum(p.qty * p.entry for p in legs))

        rate, rate_date = fx_rate(con, ccy, home)
        if rate is None:
            why.append(f"no {ccy}->{home} rate recorded")
        got = contributed_home.get(ccy, 0.0)
        if got is None:
            why.append(f"no {ccy}->{home} rate on a deposit's own date, so contributed "
                       f"capital cannot be totalled")
        marked = [p.mark_date for p in legs if p.mark_date]
        sleeves.append(Sleeve(
            currency=ccy, positions=value, positions_cost=cost,
            fx=rate, fx_date=rate_date,
            flows=flows.get(ccy, 0.0), offsets=offs.get(ccy, 0.0),
            income=inc.get(ccy, 0.0), converted=converted, traded=moved,
            contributed_home=got,
            mark_date=min(marked) if marked else None,
            unknown_because=tuple(why)))
        reasons.extend(why)

    # NOTHING RECORDED IS NOT ZERO CONTRIBUTED. Returning 0.0 would make pnl_pct divide
    # by it, and the whole return read as undefined for a reason nobody could see.
    have_flows = con.execute("SELECT 1 FROM flows LIMIT 1").fetchone() is not None
    totals = [s.contributed_home for s in sleeves]
    contributed = (None if not have_flows or any(t is None for t in totals)
                   else sum(totals))

    return Net(home=home, sleeves=tuple(sleeves), contributed=contributed,
               unknown=tuple(dict.fromkeys(reasons)))


# mark_history() and MarkHistory WERE HERE. They fed the positions sparklines and went
# with them -- the author: "i can see prices in tradingview."
#
# The idea worth keeping if anything ever needs a series again: every ticker shared ONE
# date axis, with None where a market was shut, because per-ticker last-N-rows silently
# compares different spans. The 400-day backfill in prices.py stays either way -- it is
# what gives fx_rate() a rate on every past close date.

def _acb_per_share(fills) -> float | None:
    """Average cost base per share of the shares STILL HELD, walking fills in order.

    v_position.average_price DID THIS WRONG and every cost in the tool was built on it. It
    averaged over the BUY fills only -- `SUM(CASE WHEN shares>0 THEN shares*price)` over
    `SUM(CASE WHEN shares>0 THEN shares)` -- while `shares_held` is SUM(shares), net of sells.
    So the two terms of `shares_held * average_price` came from different fill sets.

    Worked, from the audit: buy 60@100, trim -20@110, add 20@150. The view says 112.50
    (9,000/80) and a cost of 6,750. The truth is 116.667 and 7,000 -- because selling 20 of 60
    removes a THIRD of the cost, not a third of the shares at the later average. The error is
    exactly zero when every buy precedes every sell, or when all buy prices are equal, which is
    the shape of tests/seed.py and why 310 tests passed over it.

    It also sign-flipped the dashboard: "banked in open" read -50.00 on that leg where the trim
    actually banked +200.00, so a later, higher purchase retroactively turned a realised gain
    into a loss.

    ORDER MATTERS NOW, so an undated fill means the answer is unknowable and this returns None
    rather than guessing. The old view had no opinion about order at all, which is precisely how
    it could be wrong without ever looking uncertain.

    AVERAGE COST, not FIFO or specific-lot. That is what a Canadian non-registered account uses
    for tax (the ACB rule), it is what every figure in this tool already assumed it had, and it
    is the only one of the three that needs no extra recorded data. A sell reduces shares and
    cost in the SAME proportion, which is the whole content of the rule.
    """
    w = _walk_fills(fills)
    if w is None or w.held <= 1e-9:
        return None
    return w.cost / w.held


@dataclass(frozen=True)
class _Walk:
    """What one leg's fills add up to, split into the part still held and the part sold.

    BOTH HALVES FROM ONE PASS, because they are the same arithmetic. The average-cost rule
    says a sell removes shares and cost in the SAME proportion, so the cost of what was sold
    and the cost of what is left are two readings of one running figure. Computing them
    separately would be two answers to one question, and they would drift.
    """
    held: float                  # shares still held
    cost: float                  # what those shares cost, including the fees paid to buy
    sold: float                  # shares sold over the leg's life
    sold_cost: float             # what THOSE shares cost, at the ACB when each sale happened
    proceeds: float | None       # what they fetched. None if any sale had no recorded price
    realised: float | None       # proceeds less sold_cost less the fees paid to sell
    last_exit: str | None        # date of the most recent sale, or None if nothing was sold


def _walk_fills(fills) -> _Walk | None:
    """Walk one leg's fills in order. None when the answer is not knowable at all.

    THE SOLD HALF IS A CLOSED TRADE. The author, 2026-11-02: "when i close 80 shares of INTC,
    why dont we just log that in closed positions. i think that's easier than banked." It is
    easier, and it is also more honest: a `banked` column stated a running total that the
    CLOSED section's own header excluded, so the screen showed `realised +850.32` directly
    above a `-4,653.59` that was not in it. An exit of 80 shares IS a closed trade of 80
    shares, so it gets a row and lands in the total like any other.

    That makes a fully-closed leg the special case rather than the general one -- it is simply
    a leg whose sold half is all of it -- which is why one function now answers for both and
    `banked_in_open_legs` has nothing left to report.

    WHAT A SELL REALISES is `units * (price - ACB at that moment)`, less the fee to sell. The
    ACB is the running figure, so an exit is measured against what those shares cost WHEN IT
    HAPPENED rather than against some later average. A later purchase must not change the
    verdict on a sale that already took place.

    FEES SIT WHERE THEY WERE PAID: a buy's fee raises the cost of the shares it bought, a
    sell's fee reduces what that sale realised. Summed over a leg that is fully closed this
    is exactly `proceeds - cost - fees`, which is what the SQL this replaced computed -- so
    every closed figure in the tool is unchanged, and partial exits now work too.

    WITHHELD ENTIRELY, returning None, for the three things that make the ORDER or the COST
    unknowable: an undated fill (order changes every figure here), a buy with no price (the
    cost is simply not recorded), and a sell with nothing to sell (the record contradicts
    itself). A partial answer would be worse than none.

    A SELL WITH NO PRICE IS THE ONE SOFT CASE, and the asymmetry is deliberate: it cannot
    change what the remaining shares cost, nor what the sold ones cost, because both come
    from the running ACB. It makes only `proceeds` and `realised` unknowable, and those are
    reported as None while everything else still answers.
    """
    held = cost = sold = sold_cost = proceeds = realised = 0.0
    unpriced_sale = False
    last_exit: str | None = None
    for f in fills:
        if f["on_date"] is None:
            return None                       # order is unknown, and order changes the answer
        s = f["shares"]
        fee = f["fee"] or 0.0
        if s > 0:
            if f["price"] is None:
                return None                   # a buy with no price: the cost is not recorded
            held += s
            cost += s * f["price"] + fee
        else:
            if held <= 0:
                return None                   # the record contradicts itself; say nothing
            take = min(-s, held)
            # PRO RATA, which is the whole content of the average-cost rule, and the one
            # line both halves of the answer come from.
            out = cost * (take / held)
            sold += take
            sold_cost += out
            cost -= out
            held += s
            last_exit = f["on_date"]
            if f["price"] is None:
                unpriced_sale = True
            else:
                proceeds += take * f["price"]
                realised += take * f["price"] - out - fee
    return _Walk(held=held, cost=cost, sold=sold, sold_cost=sold_cost,
                 proceeds=None if unpriced_sale else proceeds,
                 realised=None if unpriced_sale else realised,
                 last_exit=last_exit)


def trades(con) -> list[Trade]:
    base = base_currency(con)
    rows = con.execute("""
        SELECT t.id, t.bet_id, t.ticker, t.currency, t.stop, t.target,
               t.loss_limit_pct, t.account_id,
               p.shares_held, p.average_price, p.unconfirmed_fills,
               (SELECT MIN(on_date) FROM fills f WHERE f.trade_id=t.id) AS opened,
               -- `quote = t.currency` OR NOTHING. quote is part of the prices PRIMARY
               -- KEY, so a mark arriving in a different currency does not REPLACE the old
               -- row, it JOINS it -- and this subquery then picked one of the two on a
               -- rowid tiebreak and valued the position at a foreign price with fx=1.0,
               -- wrong by the whole factor and printed as an ordinary number. Reachable
               -- whenever the price source's currency disagrees with the one captured at
               -- registration. Constrained, a wrong-currency row makes the leg UNPRICED,
               -- which is a state the whole codebase already handles honestly.
               (SELECT amount FROM prices WHERE kind='mark' AND base=t.ticker
                  AND quote=t.currency ORDER BY on_date DESC LIMIT 1)  AS mark,
               (SELECT on_date FROM prices WHERE kind='mark' AND base=t.ticker
                  AND quote=t.currency ORDER BY on_date DESC LIMIT 1)  AS mark_date,
               (SELECT amount FROM prices WHERE kind='fx' AND base=t.currency
                    AND quote=? ORDER BY on_date DESC LIMIT 1)         AS fx
        FROM trades t
        JOIN v_position p ON p.trade_id = t.id
        WHERE t.status = 'open'
        ORDER BY t.account_id, t.ticker
    """, (base,)).fetchall()

    # ONE EXTRA QUERY, and the arithmetic in Python -- where withholding is expressible.
    # `entry` was v_position.average_price, which is an average of BUYS and not a cost basis;
    # see _acb_per_share. Fetched for every open leg at once rather than per row, so this is
    # two queries however many positions there are.
    fills_of: dict[str, list] = {}
    for f in con.execute(
            "SELECT f.trade_id, f.on_date, f.id, f.shares, f.price, f.fee FROM fills f "
            "JOIN trades t ON t.id = f.trade_id WHERE t.status='open' "
            "ORDER BY f.trade_id, f.on_date IS NULL, f.on_date, f.id"):
        fills_of.setdefault(f["trade_id"], []).append(f)

    out = []
    for r in rows:
        # ONLY THE HELD HALF IS THIS LEG'S BUSINESS NOW. What the leg has SOLD is a closed
        # trade and lives in closed_legs() -- see _walk_fills. A `realised` field here was
        # the same money reported in a second place, which is what the author removed:
        # "why dont we just log that in closed positions."
        out.append(Trade(
            trade_id=r["id"], bet_id=r["bet_id"], ticker=r["ticker"],
            currency=r["currency"], qty=r["shares_held"],
            entry=_acb_per_share(fills_of.get(r["id"], [])),
            opened=r["opened"], stop=r["stop"],
            target=r["target"], loss_limit_pct=r["loss_limit_pct"],
            mark=r["mark"], mark_date=r["mark_date"],
            # 1.0 ONLY when the leg is already in the base currency. Otherwise a
            # missing rate stays None and every derived figure is None with it.
            fx=(1.0 if r["currency"] == base else r["fx"]),
            unconfirmed_fills=r["unconfirmed_fills"] or 0,
            account=r["account_id"]))
    return out


# STILL IN PLAY. A bet you might still act on, as opposed to one whose story is over.
#
# `forming` is IN, and that is the judgement call worth naming: it is a pre-registered plan
# with no position yet, and the author keeps those deliberately -- 2026-08-27-gold-planned exists
# because it was written BEFORE anything was at stake, which its own note calls the point. A
# plan you cannot see is a plan you will not act on.
LIVE_STATUSES = ("forming", "live", "weakened")


def is_live(status: str) -> bool:
    return status in LIVE_STATUSES


@dataclass(frozen=True)
class ClosedLeg:
    """A round trip that is over, and what it made.

    THE FIRST VIEW OF A CLOSED TRADE THIS TOOL HAS EVER HAD. Eight of them sat in the
    book, worth C$2,200 of the C$1,790 return, reachable only from `sqlite3` -- so the
    dashboard's headline had a majority nobody could see.
    """
    trade_id: str
    bet_id: str | None
    ticker: str
    currency: str
    account: str | None
    opened: str | None
    closed: str | None
    qty: float | None                # shares bought over the life of the leg
    entry: float | None              # average, weighted by shares
    exit: float | None
    cost: float | None
    realised: float | None           # in `currency`; proceeds less cost less fees

    @property
    def realised_pct(self) -> float | None:
        if not self.cost or self.realised is None:
            return None
        return 100.0 * self.realised / self.cost


@dataclass(frozen=True)
class _Exit:
    """The sold half of one leg: how much left, what it cost, and what it made."""
    qty: float                   # shares sold over the leg's life
    cost: float | None           # what those shares cost
    entry: float | None          # cost per share
    exit: float | None           # proceeds per share
    realised: float | None


def _exit_of(fills) -> _Exit | None:
    """What a leg's exits add up to, by whichever route is exact. None if it sold nothing.

    TWO CASES, BECAUSE ORDER ONLY MATTERS WHILE SOMETHING IS STILL HELD. That is the whole
    reason this is not one walk, and getting it wrong cost a real figure for a few minutes:

      * A LEG THAT ENDS FLAT needs no order at all. Its result is the cash that went out and
        the cash that came back, and addition does not care which came first. So an UNDATED
        fill is harmless here -- which matters, because the real book has exactly one: a ZNQ
        round trip whose BUY was never dated. Walking it in order put the sell first, found
        nothing to sell, and withheld -- losing a -95.20 that the dashboard counts and that
        the SQL this replaced had always reported. The CAD side stopped reconciling by
        exactly that amount, which is how it was caught.

      * A LEG STILL HOLDING SOMETHING has to decide WHICH shares were sold, and under the
        average-cost rule that is the ACB at the moment of each sale. Order decides the
        answer, so an undated fill genuinely makes it unknowable and withholds.

    FEES SIT WHERE THEY WERE PAID in both routes: a buy's fee raises what the shares cost, a
    sell's fee reduces what that sale realised. On a flat leg the two routes therefore agree
    to the cent, which is the property that makes the split safe.
    """
    sells = [f for f in fills if f["shares"] < 0]
    if not sells:
        return None
    sold = sum(-f["shares"] for f in sells)
    held = sum(f["shares"] for f in fills)
    priced = all(f["price"] is not None for f in fills)

    if priced and abs(held) <= 1e-9:
        buys = [f for f in fills if f["shares"] > 0]
        bought = sum(f["shares"] for f in buys)
        cost = sum(f["shares"] * f["price"] + (f["fee"] or 0.0) for f in buys)
        proceeds = sum(-f["shares"] * f["price"] for f in sells)
        sell_fees = sum(f["fee"] or 0.0 for f in sells)
        return _Exit(qty=sold, cost=cost,
                     entry=cost / bought if bought else None,
                     exit=proceeds / sold if sold else None,
                     realised=proceeds - cost - sell_fees)

    w = _walk_fills(fills)
    if w is None:
        return _Exit(qty=sold, cost=None, entry=None, exit=None, realised=None)
    return _Exit(qty=w.sold, cost=w.sold_cost,
                 entry=w.sold_cost / w.sold if w.sold else None,
                 exit=(w.proceeds / w.sold if w.proceeds is not None and w.sold else None),
                 realised=w.realised)


def closed_legs(con) -> list[ClosedLeg]:
    """Every exit, newest first, in its own currency. A partial exit is one of these.

    THE AUTHOR REDEFINED THIS, 2026-11-02: "when i close 80 shares of INTC, why dont we just
    log that in closed positions. i think that's easier than banked." So this is no longer
    "legs whose status is closed" -- it is every leg that has SOLD anything, and the sold part
    is the row. A full close is the case where the sold part happens to be the whole leg.

    IT FIXED A FIGURE THAT CONTRADICTED ITSELF ON SCREEN. The `CLOSED . <ccy>` header sums the
    rows under it, and before this a trim inside an open leg was not one of those rows -- it
    was a separate `banked in open` line appended outside the total. So the tab showed
    `realised +850.32` with a `-4,653.59` sitting directly beneath it, unreconcilable from the
    screen. Now it is a row, so it is in the total, and there is nothing left to append.

    WALKED, NOT SUMMED IN SQL, and that is forced: the old query computed
    `proceeds - cost - fees` over every fill, which is the exact cash flow of a leg ONLY when
    the leg is fully closed. For a partial exit `cost` would be every buy while `proceeds` is
    one sale, so the figure would be wrong by whatever is still held. The ACB walk answers
    both cases with the same arithmetic -- see _Walk.

    NOT TRANSLATED. A leg's result happened in its own currency on its own dates, and
    restating it at today's rate would fold an FX move into a figure about a stock. The
    dashboard's `USD -> CAD` line is where the currency belongs.
    """
    rows = con.execute("""
        SELECT t.id, t.bet_id, t.ticker, t.currency, t.account_id, t.closed, t.status
          FROM trades t
    """).fetchall()
    fills_of: dict[str, list] = {}
    for f in con.execute("SELECT trade_id, on_date, shares, price, fee FROM fills "
                         "ORDER BY on_date IS NULL, on_date, id"):
        fills_of.setdefault(f["trade_id"], []).append(f)

    out = []
    for r in rows:
        fills = fills_of.get(r["id"], [])
        e = _exit_of(fills)
        # A CLOSED LEG THAT SOLD NOTHING still belongs here: the status says the trade is
        # over, and dropping it would hide a trade rather than report it as unvalued.
        if e is None and r["status"] != "closed":
            continue
        dates = [f["on_date"] for f in fills if f["shares"] < 0 and f["on_date"]]
        out.append(ClosedLeg(
            trade_id=r["id"], bet_id=r["bet_id"], ticker=r["ticker"],
            currency=r["currency"], account=r["account_id"],
            opened=min((f["on_date"] for f in fills if f["on_date"]), default=None),
            # THE LEG'S RECORDED `closed` DATE WHEN IT HAS ONE. A partial exit has none, so
            # the last sale is the date the exit happened.
            closed=r["closed"] or (max(dates) if dates else None),
            qty=(e.qty if e else None) or None,
            entry=e.entry if e else None,
            exit=e.exit if e else None,
            cost=e.cost if e else None,
            realised=e.realised if e else None))
    out.sort(key=lambda c: (c.closed or "", c.trade_id), reverse=True)
    return out


def banked_in_open_legs(con) -> dict[str, float]:
    """Realised money that no closed row accounts for. MUST be empty, and that is the point.

    THIS IS NOW A CHECK, NOT A SOURCE. It used to feed a `banked in open` line on the trades
    tab, because a trim realised money while closing nothing and so appeared in no closed
    row. Since the author redefined an exit as a closed trade -- "when i close 80 shares of
    INTC, why dont we just log that in closed positions" -- every realised dollar has a row,
    and the honest value of this function is zero.

    SO IT EARNS ITS KEEP AS THE INDEPENDENT CROSS-CHECK, which is worth more than the row it
    replaced. It derives the remainder from the SLEEVE's realised total, computed by a
    completely different route from the per-leg walk, and subtracts the closed rows. If the
    two ever disagree, something is being double-counted or dropped -- which is exactly how
    the ZNQ leg was caught: the CAD side came back 95.20 short because an undated fill made
    one leg withhold a figure the sleeve still counted. See _exit_of.

    A test holds this at empty on the fixture, and tests/test_book.py holds it on the real
    book. A non-empty result is a bug report, per currency, in dollars.
    """
    closed: dict[str, float] = {}
    for leg in closed_legs(con):
        if leg.realised is not None:
            closed[leg.currency] = closed.get(leg.currency, 0.0) + leg.realised
    out: dict[str, float] = {}
    for s in net(con).sleeves:
        if s.realised is None:
            continue
        rest = s.realised - closed.get(s.currency, 0.0)
        if abs(rest) >= 0.005:      # below half a cent it is float noise, not money
            out[s.currency] = rest
    return out


@dataclass(frozen=True)
class Bet:
    id: str
    # THE SLUG IS THE KEY; `name` IS WHAT YOU READ. Falls back to the id for any bet
    # written before v13 added the column, so nothing renders blank.
    name: str
    status: str
    opened: str | None
    note: str | None                     # a one-liner HE typed, if they typed one
    allocation: float | None
    allocation_ccy: str | None
    legs: int                            # open trades under it
    # AT MARKET, IN THE HOME CURRENCY. None means a leg could not be valued, never zero.
    #
    # WAS CALLED `deployed` AND THAT NAME WAS THE BUG. The word "deployed" means capital you
    # put in -- which is COST -- while this field held market value, so the tab printed two
    # numbers for one word: "60.1% of 22,500 deployed" on the detail line and "62.0% filled"
    # three rows below it, both true, neither reconcilable from the screen. `value` says what
    # this is and leaves "deployed"/"spent"/"filled" to mean cost, consistently, everywhere.
    value: float | None = None
    weight_pct: float | None = None      # market value as a share of derived net
    # ADDED for the bets table's `weight` column, which the author redefined: "1. allocated capital
    # vs. entire portfolio ... the 1 should go to the table above". What a bet has RESERVED of
    # the portfolio, filled or not, which is a different question from how much is at risk --
    # both of their bets claim 47.3% while deploying 7.9% and 23.4%.
    # OF THE TOTAL ALLOCATED, NOT OF NET, and the author's reason is the better one: "essentially we
    # are giving each bet x amount of cash to play out. this has nothing to do with total net
    # worth." An allocation is a BUDGET, so the comparison that means something is against the
    # other budgets -- 50/50 here -- and net worth answers a different question.
    #
    # The question net worth answers is not lost, it moved: the dashboard's portfolio table
    # carries `of net`, so "94.5% of the book is committed, 5.5% is unclaimed" still has a
    # home. Each page's denominator matches its own subject -- this tab is about the plan.
    of_allocated_pct: float | None = None
    unpriced_legs: int = 0               # legs with no mark or no rate, so `value` is None
    cost: float | None = None            # the open legs at cost, home currency
    # REALISED, TRANSLATED AT TODAY'S RATE. A bet can hold legs in more than one currency --
    # ai-capex-semis held five USD legs and a CAD one -- so a single figure needs a
    # translation, and which rate is a real choice.
    #
    # AT-CLOSE-DATE WAS BUILT FIRST AND REJECTED. It is the more historically honest number
    # -- what the gain was worth on the day it was made -- but it gave the tool TWO answers
    # to "realised": the sum over bets came to C$2,202.47 while the dashboard said
    # C$2,200.00, the C$2.47 being five weeks of currency drift on the USD gains. Two
    # figures for one word, differing by an amount nobody can attribute, is exactly what
    # erodes trust in a derived number.
    #
    # So: one convention, and the per-bet figures sum to the dashboard's exactly -- there is
    # a test for it. The cost is that a realised USD gain's later currency drift sits inside
    # `realised` rather than beside it, which is also how a broker statement reads.
    realised: float | None = None
    closed_cost: float | None = None     # what every exit cost, partial or complete
    closed_legs: int = 0
    unvalued_closed_legs: int = 0        # closed legs with no price or no rate on the day
    # WHICH PLAYBOOK THIS IS AN INSTANCE OF, or None for a one-off. See bets.setup in
    # schema.sql: the reasoning lives once per setup, so hundreds of instances cost one row
    # each instead of one document each.
    setup: str | None = None
    # THE DAY IT ENDED, which only became worth carrying when a setup needed a hold span.
    # None while the bet is live, which is what makes `_span_days` withhold rather than
    # measure a hold that has not finished.
    closed: str | None = None

    @property
    def unrealised(self) -> float | None:
        if self.value is None or self.cost is None:
            return None
        return self.value - self.cost

    @property
    def pnl(self) -> float | None:
        """What this bet has made, open and closed together. The whole point of a bet.

        Withheld rather than partial: a bet whose realised half is unknown must not print
        its unrealised half as though that were the answer. The author asked for "the pnl per
        bet", and half of one is not a P&L.
        """
        if self.realised is None or self.unrealised is None:
            return None
        return self.realised + self.unrealised

    @property
    def pnl_pct(self) -> float | None:
        """Against the capital this bet actually put to work, open and closed.

        NOT against the frozen allocation. A bet that declared C$10,000 and spent C$600
        has not made 5% of its plan -- it has made whatever it made on the C$600. The
        allocation is a ceiling, not a denominator.
        """
        base = self.invested
        if not base or self.pnl is None:
            return None
        return 100.0 * self.pnl / base

    @property
    def invested(self) -> float | None:
        """Every dollar this bet has committed: what it still holds, and what every exit cost.

        THE DENOMINATOR MUST COVER THE SAME CAPITAL AS THE NUMERATOR, and for one day it did
        not. `realised` had grown to count what a trim banked, while `closed_cost` still
        counted only legs that closed COMPLETELY -- so a partly-sold leg handed its result to
        the numerator and none of its cost to the denominator. A bet showed -115.72%, a loss
        bigger than everything put into it, which can only happen if the capital is actually
        gone. It was not: the honest figure was -42.18%.

        The author, shown both: "% of all capital put in", and then separately: "the bet's
        pnl includes total deployed assets and total pnl from open and closed positions." So a
        >100% loss is impossible here unless the money really is gone.

        IT NEEDS NO SPECIAL TERM FOR TRIMS ANY MORE. A partial exit is a closed trade now, so
        `closed_cost` already covers the cost of every exit and this is just two terms again.
        The version that needed three was paying for a `banked` figure that no longer exists.
        """
        if self.cost is None or self.closed_cost is None:
            return None
        return self.cost + self.closed_cost

    @property
    def filled_pct(self) -> float | None:
        """How much of the frozen allocation has been SPENT. Entry cost, not market value.

        THE NUMBER THAT MAKES THE WEIGHT READABLE. A bet can be 1.4% of the book, which
        sounds like nothing -- until you see it is 5.7% of a plan for C$25,000, which is a
        quarter of the book. A weight alone says what is at risk; this says what is coming.

        AT COST, AND THIS REVERSES AN EARLIER DECISION OF MINE. It was market value over
        allocation, on the argument that what is at risk matters more than what was
        committed. That gave the bets tab two "filled" numbers -- 60.1% on the detail line
        and 62.0% in the HOLDINGS header -- for the same bet, three rows apart. The author:
        "i think the standard practice for this is filled. bc that's what important for the
        portfolio decision right? so entry cost/allocation."

        They are right on both counts. `filled` is an order word and what fills is measured at
        the price you paid; and the question this answers is "how much budget is left to
        spend", which only cost can answer. It is also the ONLY basis on which
        `spent + dry == allocation` -- the identity the HOLDINGS bars are drawn from. At
        market a bet that doubled would read past 100% filled while still holding cash.

        What is at risk did not disappear: `value`, `weight_pct` and the P&L columns are all
        at market. Each figure now has one basis and says which.
        """
        if not self.allocation or self.cost is None:
            return None
        return 100.0 * self.cost / self.allocation


def bets(con) -> list[Bet]:
    """Every bet, newest first. A pod-level risk view, and nothing about files.

    There is no thesis status here. The desk does not know where the vault is or
    whether a note exists -- the author writes theses in Obsidian, by shortcut or by
    chatting with an AI, and the tool's business is the book.

    THE WEIGHT IS COMPUTED HERE NOW, not read from v_bet_exposure, and the view had four
    defects rather than one:

      * its denominator was the pooled `account_snapshots`, which nothing writes any more,
        so every weight was measured against a statement dated 2026-09-07 and drifted
        further from the truth every day;
      * `COALESCE((SELECT amount FROM prices WHERE kind='fx' ...), 1.0)` -- A USD LEG WITH
        NO RATE WAS VALUED AT PAR, wrong by the whole factor, about 38% here. That is the
        one hazard this codebase has fixed in four other places and it was still in SQL;
      * `quote='CAD'` was hardcoded, so a USD home currency found nothing and fell into
        the par branch above;
      * it measured at COST, which is what was committed rather than what is at risk.

    Python already has all four right: Trade.value_base returns None when there is no
    mark or no rate, net() withholds a partial total, and the home currency comes from the
    profile. So the arithmetic moves to where those guarantees live.
    """
    rows = con.execute("""
        SELECT b.id, COALESCE(NULLIF(b.name,''), b.id) AS name, b.status, b.opened,
               b.closed, b.setup, b.notes, b.allocation_base, b.allocation_ccy,
               (SELECT COUNT(*) FROM trades t
                  WHERE t.bet_id = b.id AND t.status = 'open') AS legs
        FROM bets b
        ORDER BY b.opened IS NULL, b.opened DESC, b.id
    """).fetchall()

    legs_of: dict[str | None, list] = {}
    for p in trades(con):
        legs_of.setdefault(p.bet_id, []).append(p)
    total = net(con).net
    home = base_currency(con)
    # THE DENOMINATOR FOR `of_allocated_pct`: every LIVE bet's declared capital. Finished bets
    # are excluded because their budgets are no longer competing for anything -- including
    # them would shrink every live bet's share by however much history happens to sit behind
    # it, and the share would drift downwards as the book ages rather than when they re-plans.
    # THE ALLOCATION IS ONLY COMPARABLE IN THE HOME CURRENCY, and set_allocation stamps
    # `allocation_ccy` with whatever the home currency was WHEN IT WAS WRITTEN. So changing
    # `home_currency` in config.toml leaves every stored allocation denominated in the old
    # one -- and `filled`, `of_allocated_pct` and the composition bars all divided a
    # home-currency cost by it anyway, printing confident percentages that were wrong by the
    # whole FX factor. Withheld rather than translated: an allocation is a figure the user
    # TYPED,
    # and silently restating what someone declared is worse than declining to divide by it.
    home_alloc = {r["id"]: (r["allocation_base"] if r["allocation_ccy"] == home else None)
                  for r in rows}
    # THE DENOMINATOR WITHHOLDS TOO, and it did not -- so the numerator obeyed this
    # project's cardinal rule while the denominator quietly broke it. A live bet whose
    # allocation could not be read in the home currency was dropped from this SUM as well as
    # from its own figure, so a remaining bet divided by a total that excluded it and printed
    # a confident "100.0% of plan" while holding a fraction of it. Now every of_allocated_pct
    # goes None, which is the honest answer when one of the budgets cannot be read.
    _live = [home_alloc[r["id"]] for r in rows
             if is_live(r["status"]) and r["allocation_base"] is not None]
    allocated_total = None if any(v is None for v in _live) else sum(_live)

    done_of: dict[str | None, list] = {}
    for leg in closed_legs(con):
        done_of.setdefault(leg.bet_id, []).append(leg)

    out = []
    for r in rows:
        legs = legs_of.get(r["id"], [])
        # SHORT BY ANY LEG IT CANNOT PRICE, not merely absent when the whole bet is
        # unpriceable. A bet with one unpriced leg used to report a confident weight that
        # understated its own market value, which is the worst of the three outcomes.
        blind = [p for p in legs if p.value_base is None]
        value = None if blind else sum(p.value_base for p in legs)
        cost = (None if any(p.cost_base is None for p in legs)
                else sum(p.cost_base for p in legs))
        weight = (None if value is None or not total
                  else 100.0 * value / total)

        done = done_of.get(r["id"], [])
        realised: float | None = 0.0
        closed_cost: float | None = 0.0
        unvalued = 0
        for leg in done:
            # TODAY'S RATE, deliberately, and `on=None` is what says so. See the comment on
            # Bet.realised: close-date rates gave the tool two answers to "realised".
            rate, _ = fx_rate(con, leg.currency, home)
            if leg.realised is None or leg.cost is None or rate is None:
                unvalued += 1
                realised = closed_cost = None
                continue
            if realised is not None:
                realised += leg.realised * rate
                closed_cost += leg.cost * rate

        # BOTH EXTRA LOOPS THAT WERE HERE ARE GONE, and that is the point of the author's
        # redesign. They added what the OPEN legs had banked, and then the cost of what those
        # legs had sold, because a trim realised money that no closed leg carried. Now a trim
        # IS a closed leg -- see closed_legs() -- so `done` above already counts both, and
        # counting them again here would double every partial exit.
        #
        # `invested` therefore needs no third term: closed_cost covers the cost of every
        # exit, partial or complete, which is the capital that `realised` is measured on.

        out.append(Bet(
            id=r["id"], name=r["name"], status=r["status"], opened=r["opened"],
            note=r["notes"],
            allocation=home_alloc[r["id"]], allocation_ccy=r["allocation_ccy"],
            legs=r["legs"], value=value, weight_pct=weight,
            of_allocated_pct=(None if not home_alloc[r["id"]] or not allocated_total
                              else 100.0 * home_alloc[r["id"]] / allocated_total),
            unpriced_legs=len(blind), cost=cost,
            realised=realised, closed_cost=closed_cost,
            closed_legs=len(done), unvalued_closed_legs=unvalued,
            setup=r["setup"], closed=r["closed"]))
    return out


FINISHED = ("closed", "falsified", "retired", "abandoned")


@dataclass(frozen=True)
class Setup:
    """What one playbook has actually done, across every bet that named it.

    THE FIGURE THAT DECIDES WHAT TO STOP DOING. The author, 2026-11-02, on expecting
    hundreds of bets a year: "now a bet is mostly a structure and a setup." Once a bet is an
    instance rather than a thesis, the question stops being "how is this bet doing" and
    becomes "does this setup make money" -- which needs n instances, not one.

    FINISHED INSTANCES ONLY. A live bet has no verdict, so counting it would move the hit
    rate every time a price did. `live` is reported beside the record, never inside it.
    """
    setup: str
    live: int                    # instances still running, excluded from every figure below
    n: int                       # finished instances, the denominator for all of it
    wins: int
    pnl: float | None            # summed over finished instances, home currency
    hold_days: float | None      # mean, over finished instances that carry both dates

    @property
    def hit_pct(self) -> float | None:
        return None if not self.n else 100.0 * self.wins / self.n

    @property
    def expectancy(self) -> float | None:
        """Mean result per finished instance -- what one more of these is worth."""
        return None if not self.n or self.pnl is None else self.pnl / self.n


def setups(con) -> list[Setup]:
    """Every named setup, worst expectancy last. Bets with no setup are not a setup.

    WITHHELD RATHER THAN PARTIAL, like every other total here: if any finished instance's
    P&L cannot be computed -- an untranslatable leg, an unpriced sale -- the setup's `pnl`
    and `expectancy` are None instead of a sum over the instances that happened to work.
    `hit_pct` survives that, because whether a bet made money is knowable from its sign more
    often than its size is.
    """
    by: dict[str, list] = {}
    for b in bets(con):
        if b.setup:
            by.setdefault(b.setup, []).append(b)

    out = []
    for name, group in by.items():
        done = [b for b in group if b.status in FINISHED]
        live = len(group) - len(done)
        got = [b.pnl for b in done]
        # sum([]) IS 0.0 AND THAT WOULD BE A LIE. A setup whose instances are all still
        # running has not made nothing -- it has made no verdict yet. Same rule as every
        # other total in this file: nothing to add is None, not zero.
        total = None if not got or any(x is None for x in got) else sum(got)
        wins = sum(1 for x in got if x is not None and x > 0)
        spans = [_span_days(b.opened, b.closed) for b in done]
        spans = [d for d in spans if d is not None]
        out.append(Setup(setup=name, live=live, n=len(done), wins=wins, pnl=total,
                         hold_days=(sum(spans) / len(spans) if spans else None)))
    # WORST LAST, and unscoreable setups after the scored ones: a setup with no finished
    # instance is not a zero-expectancy setup, it is one you cannot judge yet.
    return sorted(out, key=lambda x: (x.expectancy is None, -(x.expectancy or 0.0)))


def _span_days(opened: str | None, closed: str | None) -> float | None:
    if not opened or not closed:
        return None
    try:
        return (dt.date.fromisoformat(closed) - dt.date.fromisoformat(opened)).days
    except ValueError:
        return None


@dataclass(frozen=True)
class Falsifier:
    """One recorded way a bet could be wrong, and whether that has been settled.

    INVISIBLE UNTIL v18. Fourteen of these were written, carefully -- `source` names a data
    release, `notes` links into the vault with a line number -- and no screen in the desk
    ever showed one, so none was ever resolved. A thing you cannot see is a thing you cannot
    close.
    """
    id: int
    bet_id: str
    role: str                    # 'main-support' | 'side-leg'
    leg_label: str | None
    wrong_if: str
    threshold: str | None
    action: str | None
    known_by: str | None
    known_by_prose: str | None
    cadence: str | None
    source: str | None
    written_on: str | None
    provenance: str
    resolved_on: str | None
    outcome: str | None          # 'held' | 'broken' | 'ambiguous'
    notes: str | None

    @property
    def settled(self) -> bool:
        return self.outcome is not None

    @property
    def when(self) -> str | None:
        """The best answer to "when would you know" -- a date, else the prose, else cadence."""
        return self.known_by or self.known_by_prose or self.cadence


def falsifiers_of(con, bet_id: str) -> list[Falsifier]:
    """A bet's falsifiers, main support first, then by id so the order is stable.

    STABLE ORDER MATTERS MORE THAN IT LOOKS: the resolve prompt names one by its position on
    screen, so a list that reordered between the render and the keystroke would resolve the
    wrong falsifier. `id` breaks every tie.
    """
    rows = con.execute(
        "SELECT id, bet_id, role, leg_label, wrong_if, threshold, action, known_by, "
        "       known_by_prose, cadence, source, written_on, provenance, "
        "       resolved_on, outcome, notes "
        "  FROM falsifiers WHERE bet_id=? "
        " ORDER BY role <> 'main-support', id", (bet_id,)).fetchall()
    return [Falsifier(**dict(r)) for r in rows]


def resolve_falsifier(con, fid: int, *, outcome: str, on_date: str) -> dict:
    """Settle one falsifier: held, broken, or ambiguous, on a date. Pre-image back for undo.

    BOTH FIELDS IN ONE UPDATE, because the schema's CHECK insists: `(resolved_on IS NULL) =
    (outcome IS NULL)`. A date without a verdict, or a verdict with no date, is half a
    resolution and reads as a finished one.

    'ambiguous' IS A REAL ANSWER and not a cop-out. It is what `revoked_on`/`revoked_why`
    were reaching for before they went in v17: sometimes the thing you said you would watch
    stopped being the thing that mattered, and recording that is more honest than forcing
    held or broken. What it must not be is left blank, which is what fourteen of these were.

    NOT BEFORE IT WAS WRITTEN -- the schema checks that too. A falsifier resolved earlier
    than the day it was written is a note reconstructed after the fact and read as foresight,
    which is the exact failure `provenance` exists to catch.
    """
    row = con.execute("SELECT id, resolved_on, outcome FROM falsifiers WHERE id=?",
                      (fid,)).fetchone()
    if row is None:
        raise DeskError(f"no falsifier {fid}")
    if outcome not in ("held", "broken", "ambiguous"):
        raise DeskError(f"outcome is held, broken or ambiguous -- not {outcome!r}")
    pre = dict(row)
    con.execute("UPDATE falsifiers SET resolved_on=?, outcome=? WHERE id=?",
                (on_date, outcome, fid))
    return pre


def unresolve_falsifier(con, fid: int) -> dict:
    """Clear a resolution, for the undo path and for a verdict entered by mistake."""
    row = con.execute("SELECT id, resolved_on, outcome FROM falsifiers WHERE id=?",
                      (fid,)).fetchone()
    if row is None:
        raise DeskError(f"no falsifier {fid}")
    pre = dict(row)
    con.execute("UPDATE falsifiers SET resolved_on=NULL, outcome=NULL WHERE id=?", (fid,))
    return pre


def parked(con) -> float | None:
    """Market value of open MONEY-MARKET legs, in the home currency.

    NOT A SECOND DENOMINATOR, and deliberately not one. It is printed beside the weights so
    that "1.4% of the book" is readable: on a book with most of its value in T-bill ETFs, the
    parked share can be ~90% of net, which is why every real bet looks tiny. The author, on
    buying them: "im basically parking my principal without letting them eat dust. im waiting
    on the opportunities."

    Dividing by net-less-parking instead was the alternative and it is NOT taken here. It
    would revive the risk-free carve-out the author removed from the dashboard -- "Drop the
    concept entirely" -- and it would mean the exposure figure quietly depends on how each
    instrument was classified when it was registered. One denominator, stated, plus the
    context to read it.

    `kind` comes from the price source's own instrumentType, not from a rule about
    trading, which is the difference.
    """
    rows = [p for p in trades(con)
            if con.execute("SELECT kind FROM instruments WHERE ticker=?",
                           (p.ticker,)).fetchone()[0] == "money-market"]
    if not rows:
        return 0.0
    return None if any(p.value_base is None for p in rows) else \
        sum(p.value_base for p in rows)


def add_account(con, *, account_id: str, name: str, on_date: str,
                make_default: bool = False) -> str:
    """Create an account. The one bootstrap a fresh book cannot do without.

    An account is a NAME. It held a `kind` from a Canadian tax taxonomy and a single
    `currency` until v11; both went, because nothing read the kind and one currency per
    sleeve is false -- one registered account can hold CAD and USD at once. What is
    left is the id they type after `/` on every fill.

    This is a WRITER rather than a documented SQL snippet because of the partial unique
    index on is_default: a hand-typed INSERT gets it wrong and the error is a raw
    IntegrityError.

    Not seeded by migrate.py on purpose: which sleeves someone trades is theirs to
    say, and a baked-in default would be the unchosen number this design refuses.
    """
    if con.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
        raise DeskError(f"account {account_id} already exists")
    if make_default:
        # At most one default; the schema enforces it with a partial unique index, so
        # clear the old one rather than handing back an IntegrityError.
        con.execute("UPDATE accounts SET is_default=0 WHERE is_default=1")
    con.execute(
        "INSERT INTO accounts(id,name,opened,is_default) VALUES (?,?,?,?)",
        (account_id, name, on_date, 1 if make_default else 0))
    return account_id


def _quote_lookup(ticker: str) -> list[dict]:
    """The default identifier: desk/prices.py talking to Yahoo.

    Imported lazily and INSIDE the function, for the same reason app.py does it: `book/` is a
    script directory rather than a package, so importing it at module scope would make
    desk/book.py unimportable wherever that path is not set up -- including in a test that
    only wants arithmetic.
    """
    # A PLAIN SIBLING IMPORT. This was `importlib.import_module("book.prices")` with a
    # sys.path fallback -- two ways to find one file, and NEITHER worked from an installed
    # wheel, because book/ was not a package. prices moved into desk/ on 2026-09-30.
    #
    # Still deferred rather than imported at module scope: prices reaches the network, and
    # desk/book.py is imported by every test.
    from . import prices
    return prices.identify(ticker, timeout=6)


def register_from_quote(con, ticker: str, *, lookup=None) -> dict:
    """Verify a ticker at the price source and register it. Raises DeskError otherwise.

    The author: "why not automatically check it up and add it to the registry as i enter? i mean
    seems weird that we dont do this." They are right, and the reason it was not done is only
    that the wall predates having a price source to ask.

    THE WALL BECOMES A CHECK. It exists so a typo cannot become a second instrument with its
    own cost base -- but it was enforced by asking whether the book had SEEN the ticker,
    which refuses a genuinely new holding exactly as firmly as a typo. Asking Yahoo separates
    them: a made-up symbol 404s and a real one comes back with its currency, type and name.

    AMBIGUITY IS REFUSED, NEVER GUESSED, and that is not theoretical:

        CBIL      Corgi 3-12 Month T-Bill ETF     USD, Cboe US
        CBIL.TO   Global X 0-3 Month T-Bill ETF   CAD, Toronto

    The author holds the Canadian one. Taking the first hit would have priced their holding off an
    American fund forever. INTC and TQQQ are the same shape. So several candidates raises
    AmbiguousSymbol carrying all of them, and the pane asks which one -- a tab and an enter,
    against the old wall's four words that were the same for every ticker.

    NEVER 'leveraged-etf': the schema requires a leverage_factor with it and Yahoo reports
    SOXL as a plain 'ETF', so there is nothing to supply. A leveraged fund registers as a
    plain etf, and that is the right trade -- the author does not want to be asked, and no figure
    the
    desk computes reads leverage_factor.
    """
    lookup = lookup or _quote_lookup
    ticker = ticker.upper()
    found = lookup(ticker)

    if not found:
        raise DeskError(
            f"{ticker} is not a symbol the price source knows — check the spelling")
    if len(found) > 1:
        raise AmbiguousSymbol(ticker, found)

    c = found[0]
    add_instrument(con, ticker=c["ticker"], name=c["name"], currency=c["currency"],
                   kind=c["kind"], quote_symbol=c["quote_symbol"])
    return c


def add_instrument(con, *, ticker: str, name: str | None, currency: str, kind: str,
                   quote_symbol: str | None = None,
                   leverage_factor: float | None = None) -> str:
    """Register an instrument. A fill is refused until its ticker is known.

    quote_symbol defaults to the ticker and is NOT the same thing: ZNQ does not
    resolve at Yahoo and ZNQ.TO does, which is why the column exists.

    LEVERAGE_FACTOR WAS MISSING AND THAT MADE A WHOLE KIND UNREGISTERABLE. The schema has
    `CHECK (kind <> 'leveraged-etf' OR leverage_factor IS NOT NULL)`, so registering one
    died on a raw IntegrityError from SQLite. Nothing sets that kind now that `i` is gone,
    and the parameter stays because the CHECK is still the wall.
    `is_daily_reset` is deliberately still left at its
    default: whether a fund resets daily is a fact about the fund that nothing here reads,
    and inventing a value for it would be worse than the zero.
    """
    if con.execute("SELECT 1 FROM instruments WHERE ticker=?", (ticker,)).fetchone():
        raise DeskError(f"instrument {ticker} already exists")
    if kind == "leveraged-etf" and leverage_factor is None:
        raise DeskError(f"{ticker} is a leveraged ETF and needs its factor — the schema "
                        f"refuses one without it")
    con.execute(
        "INSERT INTO instruments(ticker,name,currency,kind,quote_symbol,leverage_factor) "
        "VALUES (?,?,?,?,?,?)",
        (ticker, name, currency, kind, quote_symbol or ticker, leverage_factor))
    return ticker


def add_bet(con, *, bet_id: str, name: str | None = None,
            note: str | None = None, allocation: float | None = None,
            on_date: str) -> str:
    """Register a bet in the book. Touches nothing outside it.

    NO FOLDER, NO FILE, NO TEMPLATE. This used to create a thesis in the vault, and
    The author removed that: they write theses in Obsidian by shortcut or by chatting with
    an AI, so the tool creating a scaffold was the tool having an opinion about
    someone else's workflow.

    What a bet is here: a name, an allocation, and the legs that hang off it. The
    claim lives wherever they write it, under the same name.

    Starts as 'forming', which is now a DEFAULT rather than a gate: the triggers that
    refused to promote a bet without a main-support falsifier were dropped in v13 at the author's
    request, so `s` moves it to live whenever they say so.

    `name` is the free text they typed; `bet_id` is its slug. Defaulting name to the id keeps
    every existing caller and test honest -- a bet created without one reads as its slug,
    which is exactly what it did before the column existed.
    """
    if con.execute("SELECT 1 FROM bets WHERE id=?", (bet_id,)).fetchone():
        raise DeskError(f"bet {bet_id} already exists")
    con.execute(
        "INSERT INTO bets(id,name,status,opened,notes,"
        "allocation_base,allocation_ccy,allocation_set_on) "
        "VALUES (?,?,'forming',?,?,?,?,?)",
        (bet_id, name or bet_id, on_date, note,
         allocation, base_currency(con) if allocation else None,
         on_date if allocation else None))
    return bet_id


def flows(con) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT id, on_date, amount, kind, currency, notes, account_id FROM flows "
        "ORDER BY on_date, id").fetchall()


def capital_ledger(con) -> list[sqlite3.Row]:
    """Every cash movement the author types by hand: flows AND offsets, one list, oldest first.

    TWO TABLES, ONE PAGE, because they are the same act from where they sit -- a number
    and a date that moves the account. They are separate tables because they mean
    different things to the return: a flow moves contributed capital, the denominator, and
    an offset moves cash, the numerator. Recording a dividend as a deposit would hide
    exactly that much P&L, which is why the distinction survives in the schema even though
    it does not survive in the UI.

    `src` is what the pane keys its rows on, so a delete or an edit knows which table it
    is holding. The row key is `src:id`, because both tables number from 1 and `4` alone
    named two different rows.
    """
    return con.execute("""
        SELECT 'flow' AS src, id, on_date, amount, kind, currency, notes, account_id
          FROM flows
        UNION ALL
        SELECT 'offset' AS src, id, on_date, amount, 'offset' AS kind, currency, notes,
               account_id
          FROM adjustments
        ORDER BY on_date, src, id""").fetchall()


def capital_in(con, as_of: str | None = None) -> float | None:
    """Net capital contributed, optionally only up to `as_of`. None if nothing.

    A drawdown measured against a zero denominator is not conservative, it is
    meaningless, so the caller has to handle the absence.

    THE as_of ARGUMENT IS THE FIX FOR A REAL AND DANGEROUS BUG. The loss limit is
    `contributed - balance`, and balance comes from a snapshot dated some day D. If
    contributed sums EVERY flow while the balance is D's, then a deposit made after
    D manufactures a loss of exactly the deposit: paying in D showed a "loss" of D
    on a portfolio where no price had moved, eating that much of the headroom against
    the loss-limit wall. Enough deposits and the desk would have said "close the
    entire portfolio and step away" because the author added money.

    "Flow-immune by construction" was true only if the balance is re-read in the same
    breath as the deposit -- which is what the test did and real life does not. The
    two sides have to be as of the SAME DATE for the subtraction to mean anything.
    """
    if as_of is None:
        row = con.execute("SELECT SUM(amount) FROM flows").fetchone()
    else:
        row = con.execute("SELECT SUM(amount) FROM flows WHERE on_date <= ?",
                          (as_of,)).fetchone()
    return None if row[0] is None else float(row[0])


def accounts(con) -> list[sqlite3.Row]:
    """Every sleeve. `kind` and `currency` went in v11, and so did this SELECT's copy.

    IT WOULD HAVE RAISED. Nothing calls this -- it is here for a future account view --
    so no test caught that it still named two dropped columns. A dead function that
    raises when revived is worse than no function, because the failure arrives later and
    somewhere else.
    """
    return con.execute("SELECT id, name, opened, is_default FROM accounts "
                       "ORDER BY is_default DESC, id").fetchall()


def known_accounts(con) -> frozenset[str]:
    return frozenset(r[0] for r in con.execute("SELECT id FROM accounts"))


def default_account(con) -> str | None:
    row = con.execute("SELECT id FROM accounts WHERE is_default=1").fetchone()
    if row:
        return row[0]
    only = con.execute("SELECT id FROM accounts LIMIT 2").fetchall()
    return only[0][0] if len(only) == 1 else None


def resolve_account(con, given: str | None) -> str:
    """Which account a write lands in. Never guesses when it cannot know.

    A book with one account needs no ceremony. A book with several needs either a
    recorded default or an explicit `/acct`, because silently picking one is how a
    position ends up in the wrong sleeve with nothing to catch it.
    """
    if given is not None:
        if given not in known_accounts(con):
            raise ValueError(f"{given!r} is not an account")
        return given
    acct = default_account(con)
    if acct is None:
        known = sorted(known_accounts(con))
        # ZERO AND SEVERAL ARE DIFFERENT PROBLEMS WITH DIFFERENT ANSWERS. This said
        # "several" and printed an empty list on a fresh book, which is the first
        # thing a new user sees and it is false.
        if not known:
            raise DeskError(
                "this book has no accounts yet, and a flow, a balance and a fill all "
                "have to land in one. Open the sleeve you trade in with `a` on the "
                "dashboard — e.g. tfsa tfsa CAD =default")
        raise DeskError(
            "this book has several accounts and no default — say which with /acct: "
            + ", ".join(known))
    return acct


class Pooled:
    """Every account's latest balance, and the portfolio total ONLY if it is whole.

    THE BUG THIS TYPE EXISTS TO PREVENT: the old code took the single most recent
    snapshot row and called it the balance. The moment a second account existed
    that silently reported one sleeve as the entire portfolio -- and because the
    loss limit is measured against total contributions across BOTH accounts, it
    understated the balance by a whole account and manufactured a loss that was
    not there.

    So `total` is None unless every account has a snapshot. An incomplete pooled
    figure is not a conservative estimate, it is a wrong number.
    """
    total: float | None
    risk_free: float | None
    as_of: str | None                    # the OLDEST of the per-account dates
    per_account: tuple[sqlite3.Row, ...]
    missing: tuple[str, ...]             # accounts with no snapshot at all
    mixed_dates: bool                    # the sleeves were read on different days


class Balance:
    """Cumulative loss against capital contributed, against a dollar limit.

    FLOW-IMMUNE BY CONSTRUCTION, which is the whole reason it is shaped this way:
    loss = contributed - balance, and a deposit raises both by the same amount. A
    percentage limit would need a high-water mark, and under monthly contributions a
    resetting baseline never fires while a flow-adjusted one needs a unit index.
    Neither is required here.

    A GAIN CUSHIONS. After a good run the fall from the top can exceed the limit
    while cumulative loss does not. The author chose that reading deliberately on 2026-09-07
    over the stricter drawdown-of-peak-P&L: "that's more accurate and easier."
    """
    capital: float | None
    balance: float | None
    balance_date: str | None
    # NEWEST MARK MINUS BALANCE DATE, and the sign is the whole content: negative means
    # the marks are older than the statement (ordinary over a weekend), positive means
    # the statement predates the prices and every total here is behind. It was called
    # balance_is_stale_by, which asserted the negative case was staleness when usually
    # it is a Monday.
    mark_gap_days: int | None
    # BOTH ARE OPTIONAL, and for the same reason. `positions_value` always was; `cost`
    # summed `p.cost_base or 0.0`, so a leg with no recorded entry price contributed
    # NOTHING to cost and its whole value to mark -- printing a phantom gain of exactly
    # that position's size. The same hazard was fixed in three panes and left here.
    positions_cost: float | None
    positions_value: float | None
    limit: float | None                  # in account currency
    pooled: Pooled | None = None       # the per-account detail behind `balance`
    # Contributions dated AFTER the snapshot, and therefore not in it. Excluded from
    # `capital` on purpose -- including them turns a deposit into a loss of its own
    # size -- but surfaced so the headroom is not read as current when it is not.
    flows_after_balance: float = 0.0

    @property
    def incomplete_because(self) -> str | None:
        """Why there is no balance to show, in words a pane can print verbatim."""
        if self.pooled is None or self.balance is not None:
            return None
        if not self.pooled.per_account:
            return "no balance recorded yet"
        return ("no balance recorded for " + ", ".join(self.pooled.missing)
                + " — a partial total would understate the portfolio")

    @property
    def single_moment(self) -> bool:
        """Are both sides of the comparison as of ONE date?

        THE SECOND ROUTE TO THE BUG Pooled's docstring describes. `total` sums each
        sleeve's LATEST snapshot even when those were read on different days, and `as_of`
        is the OLDEST of them, while capital is summed only up to that oldest date. Any
        flow dated in between is therefore inside `balance` and outside `capital`, so it
        lands in P&L as a gain or loss of exactly its own size.

        Proven: rrsp last read 2026-06-01, tfsa read 2026-09-07, a 6,500 withdrawal on
        2026-08-01 already reflected in the September statement -> loss 5,041 against a
        5,000 limit, headroom -41, and the dashboard printing "LOSS LIMIT BREACHED --
        close the portfolio and step away" on a book whose true P&L is +1,459. A deposit
        in between inverts it and hides a real loss instead.

        So every derived figure below is withheld rather than computed wrong. The pooled
        total itself is still reported: it is a real sum of real statements, just not a
        single moment, and blanking the whole screen would be its own kind of lie.
        """
        return self.pooled is None or not self.pooled.mixed_dates

    @property
    def pnl(self) -> float | None:
        if self.capital is None or self.balance is None or not self.single_moment:
            return None
        return self.balance - self.capital

    @property
    def pnl_pct(self) -> float | None:
        if not self.capital or self.pnl is None:
            return None
        return 100.0 * self.pnl / self.capital

    # THE LOSS LIMIT IS GONE, and with it loss / headroom / floor / used_fraction /
    # breached. The author, 2026-09-08: "i believe you can remove the $x loss limit as well. i
    # have that number in my heart. a number that's not enforced has [no] use anyway."
    #
    # They are right about the mechanics. Nothing in this tool could ever stop a trade: the
    # broker is elsewhere and the desk is a record read afterwards. So "BREACHED --
    # close the portfolio and step away" was a sentence a text file said to a person who
    # had already decided. A wall that cannot hold is a wall in name, and naming it a wall
    # made every figure around it read as permission.
    #
    # `pnl` and `pnl_pct` STAY. Those are facts about what happened, which is the whole
    # remaining job. What went is the comparison of a fact against a number the tool had
    # no standing to hold.


# THE STATEMENT MACHINERY WAS HERE: Pooled, latest_balance(), Balance, balance(),
# loss_limit() and add_balance(). All of it read or wrote a total the author typed in by
# hand off a broker screen.
#
# The author, 2026-09-09: "why do you bookkeeping a number i did. theoretically, from now
# on, i only enter deposits, fees, or something minor adjustments, and trades and you
# should get roughly the same number as the broker" -- and "from now on, we dont
# need statements in the tool."
#
# net() replaces all of it, and the replacement is strictly better in one specific
# way worth recording: every date-alignment bug that lived here is gone rather than
# guarded. `balance` came from a statement dated some day D while `capital` summed
# flows up to D, so a deposit in between landed in P&L as a gain of its own size --
# hence Pooled.mixed_dates, Balance.single_moment and the as_of argument to
# capital_in(). Derived net has no D. It already contains every deposit, so the two
# sides are the same moment by construction.
#
# account_snapshots ITSELF SURVIVES, with its rows -- it is the author's real statement history,
# and desk/check.py still reconciles against it. Nothing in the desk reads or writes it.
#
# THE DEFECT THAT KEPT IT LOAD-BEARING IS CLOSED. v_bet_exposure divided into this table to
# get each bet's weight, so those weights were measured against a statement dated
# 2026-09-07 and drifted further every day. v12 dropped the view; bets() computes the
# weight from net() instead.


def known_tickers(con) -> frozenset[str]:
    return frozenset(r[0] for r in con.execute("SELECT ticker FROM instruments"))


def known_bets(con) -> frozenset[str]:
    return frozenset(r[0] for r in con.execute(
        "SELECT id FROM bets WHERE status IN ('forming','live','weakened')"))


# due() WAS HERE and had no caller: `obligations()` -- the dashboard block it fed -- was
# deleted with the rules, and nothing replaced it. It read v_due, whose date comparisons ran
# against a clock nothing could move, so even its one reader had been answering with a frozen
# number. Deleted with the rest of the enforcement on 2026-09-27.


# --------------------------------------------------------------------------- #
# what is owed
# --------------------------------------------------------------------------- #
def _n(count: int, noun: str, verb: str = "") -> str:
    """"1 open leg has" / "3 open legs have". A dashboard read daily should read."""
    s = f"{count} {noun}" + ("" if count == 1 else "s")
    if verb:
        s += " " + ({"have": "has", "are": "is", "cannot": "cannot"}[verb]
                    if count == 1 else verb)
    return s


class Obligation:
    """One thing owed, worded so a pane can print it verbatim.

    The wording lives HERE, beside the query that justifies it, because otherwise the
    claim and its evidence drift apart -- the pane would keep saying "no stop recorded"
    long after the view that decides it changed its mind.
    """
    severity: str          # 'stop' or 'warn', and nothing else
    what: str              # the claim, one line
    detail: str | None = None   # which rows, so it is actionable rather than a mood
    # HOW TO CLEAR IT, AS KEYS. Every obligation must name one.
    #
    # Six of the seven obligations this book raised had no remedy at all: the dashboard
    # was a list of demands the tool could not satisfy. A rule you cannot comply with
    # through the tool trains you to ignore the tool, which is worse than not checking.
    #
    # A key sequence, e.g. "s" or "3 then c": every token must be bound, and the FIRST
    # must resolve in the scope the obligation is read in. There was briefly a second
    # form -- a `desk` subcommand -- and it died with the CLI: the author wants one entry point,
    # so a remedy that is not reachable by keystroke is not a remedy. A test resolves
    # every value here rather than trusting the prose.
    fix: str | None = None

    @property
    def stops_you(self) -> bool:
        return self.severity == "stop"


# obligations() WAS HERE, and the dashboard block it fed with it.
#
# The author: "yes i think in a good product, gaps shouldnt be there at all." They are right
# about what it had become. It listed every record the book was missing -- an unset
# stop, a leg with no mark -- each with the keystroke that
# would clear it, and greeting someone with a list of chores is a tool deciding its
# own convenience is your obligation.
#
# What replaced it is narrower and earns its place: Net.unknown says why a figure ON
# SCREEN is blank, and nothing else. An unexplained em dash is worse than a nag; a
# nag about a stop the tool cannot place is just a nag.


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def atomic(con):
    """One unit of work, all of it or none of it.

    connect() sets isolation_level=None, so each statement commits on its own. That is
    right for a single INSERT and WRONG for anything that must hold together. An edit is
    delete-then-add, and when the add hit one of the book's own walls the delete had
    ALREADY committed -- so a REJECTED line destroyed a row while the prompt said only
    that the line was refused, and the pane went on claiming a rejection changes nothing.

    Proven before the fix: editing a 40-share position to sell 80 was refused with
    "USDCO holds 60; selling 80 would go short" -- a quantity that existed only because
    the -20 trim had just been deleted -- and the book was left holding a position that
    was never held.
    """
    # THE COMMIT USED TO SIT OUTSIDE THIS try, and that made the guarantee conditional on
    # the commit succeeding -- which is the one step most likely to fail, because it is where
    # SQLite actually takes the write lock and touches the disk. On SQLITE_BUSY or an I/O
    # error the exception escaped the `with`, no ROLLBACK ran, and the connection stayed
    # INSIDE an open transaction for the rest of the session: every later write reported
    # success, every SELECT on that connection saw it, a second reader saw none of it, and
    # the implicit rollback at process exit discarded the lot.
    #
    # `finally` rather than a second `except`, so the rollback covers a failed COMMIT and a
    # failed body by the same three lines. Asking `in_transaction` rather than tracking it:
    # after a successful COMMIT there is nothing to roll back, and after a failed one SQLite
    # is the only thing that knows.
    con.execute("BEGIN")
    try:
        yield
        con.execute("COMMIT")
    finally:
        if con.in_transaction:
            con.execute("ROLLBACK")


def _open_trade_for(con, bet_id: str, ticker: str, account: str) -> str | None:
    """The position is the (bet, instrument, ACCOUNT) triple.

    Account is part of the key, not a label on it. The same instrument in two
    sleeves is two positions with two cost bases -- CASH.TO is held in both here.
    Leaving account out of this lookup would append an RRSP fill to the TFSA trade
    and blend the averages, which no later report could untangle.
    """
    # `IS`, not `=`. bet_id is nullable since v10 and `bet_id = NULL` is never true, so
    # `=` would fail to find the open bet-less position and add_fill would open a second
    # trade for every top-up -- a monthly parking purchase becoming a pile of one-fill
    # trades with no shared average cost. `account_id IS ?` was already doing this for the
    # same reason one column over.
    row = con.execute(
        "SELECT id FROM trades WHERE bet_id IS ? AND ticker=? AND status='open' "
        "AND account_id IS ?", (bet_id, ticker, account)).fetchone()
    return row[0] if row else None


def add_fill(con, *, ticker: str, shares: float, price: float,
             bet_id: str | None = None,
             on_date: str, fee: float | None = None,
             note: str | None = None,
             account: str | None = None) -> tuple[str, int, bool]:
    """Record a fill. Returns (trade_id, fill_id, created_a_new_trade).

    A fill in an instrument this bet already holds IN THIS ACCOUNT appends to that
    trade. Otherwise a trade is created. That is what makes registration one line:
    the position is the (bet, instrument, account) triple, and how many rows
    express it is bookkeeping.
    """
    # NO BET IS A LEGITIMATE ANSWER since v10. This used to refuse with "a trade with no
    # bet has no thesis", which is true and beside the point: the author, buying SGOV/CBIL --
    # "im basically parking my principal without letting them eat dust ... this has nothing
    # to do with any bet." The tool records what happened; whether a position expresses a
    # claim is their business, not the schema's.
    acct = resolve_account(con, account)
    kinds = {True: "add", False: "trim"}
    trade_id = _open_trade_for(con, bet_id, ticker, acct)
    created = False

    if trade_id is None:
        if shares < 0:
            where = f"in {bet_id}" if bet_id else "outside any bet"
            raise DeskError(f"no open {ticker} position {where} to reduce")
        trade_id = f"{on_date}-{ticker.lower()}"
        n = 1
        while con.execute("SELECT 1 FROM trades WHERE id=?", (trade_id,)).fetchone():
            n += 1
            trade_id = f"{on_date}-{ticker.lower()}-{n}"
        cur = con.execute("SELECT currency FROM instruments WHERE ticker=?",
                          (ticker,)).fetchone()
        if cur is None:
            raise DeskError(f"{ticker} is not in the instrument registry")
        con.execute(
            # `ruleset_as_of` WAS SET HERE to the trade's own open date, so a trade could
            # only ever be judged by the rules in force when it was taken. Dropped in v14
            # with the rules themselves -- there is no version to pin.
            "INSERT INTO trades(id,bet_id,ticker,currency,side,status,opened,"
            "account_id) VALUES (?,?,?,?,'long','open',?,?)",
            (trade_id, bet_id, ticker, cur[0], on_date, acct))
        created = True
        kind = "open"
    else:
        held = con.execute("SELECT shares_held FROM v_position WHERE trade_id=?",
                           (trade_id,)).fetchone()[0] or 0.0
        if shares < 0 and abs(shares) > held + 1e-9:
            raise DeskError(
                f"{ticker} holds {held:g}; selling {abs(shares):g} would go short "
                f"(rule 3)")
        kind = kinds[shares > 0] if not (shares < 0 and abs(shares) >= held - 1e-9) \
            else "close"

    cur = con.execute(
        "INSERT INTO fills(trade_id,on_date,kind,shares,price,fee,fee_source,"
        "notes) VALUES (?,?,?,?,?,?,?,?)",
        (trade_id, on_date, kind, shares, price, fee,
         "statement" if fee is not None else None, note))
    fill_id = int(cur.lastrowid)

    if kind == "close":
        # IT ALSO WROTE exit_trigger='none-written' HERE, on every close. Seven of the author's 11
        # closed trades carried that placeholder, which is a field saying "this field is
        # empty" -- and the column was readable from nowhere but sqlite3. Dropped with the
        # field; a close that needs a reason gets one in `notes`, which is read.
        con.execute("UPDATE trades SET status='closed', closed=? WHERE id=?",
                    (on_date, trade_id))
    return trade_id, fill_id, created


def add_flow(con, *, amount: float, on_date: str, note: str | None = None,
             account: str | None = None, currency: str | None = None) -> int:
    """Money in or out. `currency` defaults to the profile's home currency.

    It has to be recorded because net is DERIVED from cash now, and cash is a pile per
    currency -- a deposit with no currency cannot be put in either pile.
    """
    acct = resolve_account(con, account)
    kind = "deposit" if amount > 0 else "withdrawal"
    ccy = (currency or config.profile().home_currency).upper()
    if ccy not in config.CURRENCIES:
        raise DeskError(f"{ccy} is not a currency this book handles — "
                        f"{', '.join(config.CURRENCIES)}")
    cur = con.execute(
        "INSERT INTO flows(on_date,amount,kind,currency,notes,account_id) "
        "VALUES (?,?,?,?,?,?)", (on_date, amount, kind, ccy, note, acct))
    return int(cur.lastrowid)


def add_adjustment(con, *, amount: float, on_date: str, currency: str | None = None,
                   note: str | None = None, account: str | None = None) -> int:
    """A signed cash correction: the OFFSET.

    The author, 2026-09-09, on dividends and fees: "they are just way too little to track, we will
    just add a special offset entry into the account balance ... if the diff is small, it's
    good enough."

    So one row stands in for a dividend, an interest credit, a fee, or half of a currency
    conversion. It moves CASH and never contributed capital -- see the table's own comment
    for why mixing those would move the denominator of every return figure.
    """
    acct = resolve_account(con, account)
    if amount == 0:
        raise DeskError("an offset of zero changes nothing")
    ccy = (currency or config.profile().home_currency).upper()
    if ccy not in config.CURRENCIES:
        raise DeskError(f"{ccy} is not a currency this book handles — "
                        f"{', '.join(config.CURRENCIES)}")
    cur = con.execute(
        "INSERT INTO adjustments(on_date,account_id,currency,amount,notes) "
        "VALUES (?,?,?,?,?)", (on_date, acct, ccy, amount, note))
    return int(cur.lastrowid)


STOP_BASES = ("gross-deployed", "current-cost")


def set_stop(con, trade_id: str, *, pct: float, basis: str = "gross-deployed",
             price: float | None = None, on_date: str) -> dict:
    """Record a leg's stop. Returns the pre-image so the write is undoable.

    THE OBLIGATION THE DESK COULD NOT CLEAR. v_trade_stop_check reads
    trades.loss_limit_pct, the dashboard reports every open leg that has none as a
    BLOCKING obligation, and until this function existed nothing in the tool could write
    it -- so the desk's only STOP was permanently unclearable and the user's choice was
    to ignore the dashboard or edit the database by hand. A rule you cannot comply with
    through the tool trains you to ignore the tool.

    `source` is 'hand-set' and not negotiable here: the alternatives in the schema mean
    "the curve set it at the bet's max" and "rule 1 forced it for a leveraged vehicle",
    and claiming either for a number a person typed would misattribute the decision.
    """
    row = con.execute(
        "SELECT id, status, loss_limit_pct, loss_limit_basis, loss_limit_source, stop, "
        "stop_set_on FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if row is None:
        raise DeskError(f"no trade {trade_id}")
    if row["status"] not in ("open", "planned"):
        raise DeskError(f"{trade_id} is {row['status']} — a stop on it would be a "
                        f"record of a decision that can no longer be acted on")
    if not 0 < pct <= 100:
        raise DeskError("a stop is a percentage above 0 and at most 100")
    if basis not in STOP_BASES:
        raise DeskError(f"{basis!r} is not a basis — {', '.join(STOP_BASES)}")
    if price is not None and price <= 0:
        raise DeskError("a stop price is positive")

    pre = dict(row)
    con.execute(
        "UPDATE trades SET loss_limit_pct=?, loss_limit_basis=?, "
        "loss_limit_source='hand-set', stop=COALESCE(?, stop), stop_set_on=? "
        "WHERE id=?", (pct, basis, price, on_date, trade_id))
    return pre


def set_allocation(con, bet_id: str, *, amount: float, on_date: str,
                   why: str | None = None) -> dict:
    """The bet's frozen capital. Returns the pre-image.

    RE-BASELINING IS A RECORDED ACT, not an edit. The schema says so and enforces half of
    it (`allocation_rebased_from IS NULL OR allocation_rebase_why IS NOT NULL`); this
    enforces the other half, that changing an existing figure fills rebased_from in the
    first place. Every leg's share of the bet is measured against this number, so a silent
    change would move every required stop under the bet at once and leave no trace of why.
    """
    row = con.execute(
        "SELECT id, allocation_base, allocation_ccy, allocation_set_on, "
        "allocation_rebased_from, allocation_rebase_why FROM bets WHERE id=?",
        (bet_id,)).fetchone()
    if row is None:
        raise DeskError(f"no bet {bet_id}")
    if amount <= 0:
        raise DeskError("an allocation is positive")
    pre = dict(row)
    old = row["allocation_base"]
    base = base_currency(con)

    if old is None:
        con.execute("UPDATE bets SET allocation_base=?, allocation_ccy=?, "
                    "allocation_set_on=? WHERE id=?", (amount, base, on_date, bet_id))
        return pre
    if abs(old - amount) < 1e-9:
        raise DeskError(f"{bet_id} is already allocated {old:,.2f} {row['allocation_ccy']}")
    if not why:
        raise DeskError(
            f"{bet_id} is already allocated {old:,.2f} {row['allocation_ccy']}. "
            f"Re-baselining moves every leg's share of the bet at once, so it needs a "
            f"reason: add ~why")
    con.execute(
        "UPDATE bets SET allocation_base=?, allocation_ccy=?, allocation_set_on=?, "
        "allocation_rebased_from=?, allocation_rebase_why=? WHERE id=?",
        (amount, row["allocation_ccy"] or base, on_date, old, why, bet_id))
    return pre


BET_STATUSES = ("forming", "live", "weakened", "falsified", "closed", "retired",
                "abandoned")

# The statuses that mean the bet is over. Holding an open leg under one of them is a book
# that says "this is finished" while money is still at risk.
_BET_OVER = ("falsified", "closed", "retired", "abandoned")


def set_bet_name(con, bet_id: str, *, name: str) -> dict:
    """Rename a bet as a person reads it. One UPDATE, undoable, pre-image back.

    THIS REPLACED A NINE-TABLE REWRITE, and the replacement is the point. When the slug was
    also the display name, renaming meant re-pointing every foreign key that referenced it
    -- nine columns, `PRAGMA defer_foreign_keys`, and an operation `u` could not undo. Now
    the id is derived once and never changes, so a change of wording is the cheapest write
    in the book.

    The author asked for the typing to get shorter ("with rename, i dont need to type out the
    entire stuff all at once"); separating the name from the key is what made that possible,
    and deleting rename_bet is what it bought.
    """
    row = con.execute("SELECT id, name FROM bets WHERE id=?", (bet_id,)).fetchone()
    if row is None:
        raise DeskError(f"no bet {bet_id}")
    if not name.strip():
        raise DeskError("a name is not empty")
    pre = dict(row)
    con.execute("UPDATE bets SET name=? WHERE id=?", (name.strip(), bet_id))
    return pre


def set_bet_setup(con, bet_id: str, *, setup: str | None) -> dict:
    """Name the playbook this bet is an instance of, or clear it. Pre-image back.

    SLUGIFIED, for the reason in schema.sql: a setup groups by exact match, so 'Oversold
    Bounce' and 'oversold bounce' must not become two playbooks. Same transform that turns a
    bet's name into its id, so the vocabulary is one thing typed freely and stored exactly.

    AN EMPTY STRING CLEARS IT rather than storing one. '' and NULL would both mean "no
    setup" and only one of them groups correctly -- the bets tab would grow a nameless
    heading beside the one-offs it is already showing.
    """
    row = con.execute("SELECT id, setup FROM bets WHERE id=?", (bet_id,)).fetchone()
    if row is None:
        raise DeskError(f"no bet {bet_id}")
    pre = dict(row)
    slug = parse.slugify(setup) if setup and setup.strip() else None
    con.execute("UPDATE bets SET setup=? WHERE id=?", (slug, bet_id))
    return pre


def set_bet_note(con, bet_id: str, *, note: str | None) -> dict:
    """Replace a bet's note. Returns the pre-image.

    NOT EDITABLE FROM THE ONE-LINE PROMPT, which is why the pane hands this to $EDITOR. The
    2026-q4-reverse note is three paragraphs and about 1,400 characters of transcribed
    reasoning; a prompt that replaced it with one typed line would destroy the thesis to fix
    a typo in it, and nothing about a one-line grammar can express a paragraph break.
    """
    row = con.execute("SELECT id, notes FROM bets WHERE id=?", (bet_id,)).fetchone()
    if row is None:
        raise DeskError(f"no bet {bet_id}")
    pre = dict(row)
    con.execute("UPDATE bets SET notes=? WHERE id=?", (note or None, bet_id))
    return pre


# rename_bet() AND bets_referrers() WERE HERE AND WERE DELETED IN v13.
#
# They changed a bet's ID, carrying all nine referring columns with it under
# `PRAGMA defer_foreign_keys` -- correct, tested, and obsolete one day later. Once `name` is
# free text and the slug is derived once and never shown, there is nothing left that wants
# the id to change: renaming is `set_bet_name`, one UPDATE that `u` can undo.
#
# Deleted rather than kept "in case": an operation no key reaches is the same furniture as a
# column no writer fills, which this project has now removed from `tp`, the instrument key
# and the loss-limit curve. If a slug ever must change, the git history has it working.

@dataclass(frozen=True)
class PortfolioGroup:
    """One row of the portfolio: a bet, or the legs under no bet, with its legs."""
    bet_id: str | None
    name: str
    status: str | None
    value: float | None          # open legs AT MARKET, home currency; None if a leg is blind
    weight_pct: float | None     # market value as a share of derived net
    legs: tuple                  # Trade, largest first
    unvalued: int                # legs left out of `value` for want of a mark or a rate
    # TWO WEIGHTS, BECAUSE THE AUTHOR NAMED TWO. 2026-09-24: "when we say weight, i think there are
    # two numbers im interested about. 1. allocated capital vs. entire portfolio 2. deployed
    # capital vs. bet." They answer different questions and neither substitutes for the
    # other: `claim_pct` is how much of the portfolio this bet has RESERVED, filled or not,
    # and `weight_pct` is how much is actually at risk. On their book both bets claim 47.3%
    # while holding 7.9% and 23.4% -- a six-fold gap that one number cannot express.
    allocation: float | None = None
    claim_pct: float | None = None    # allocation as a share of derived NET
    # ALLOCATION AS A SHARE OF EVERY ALLOCATION, which is what the dashboard row shows. The
    # author, 2026-11-02, on seeing two bets both report 61.5%: "i think this should be the
    # share of sum (total reserved capital for each bet)."
    #
    # IT WAS A SHARE OF NET and that read as broken even though it was not. Two bets holding
    # 18,896 and 5,314 printed the SAME 61.5% with the same bar, because both had reserved
    # 22,500 and net was 36,592 -- a true figure that answered a question nobody was asking
    # while sitting in a column whose other rows answered a different one.
    #
    # Against the other budgets it sums to 100% and says something usable: half the committed
    # capital each. Same rule the bets tab already uses -- "essentially we are giving each bet
    # x amount of cash to play out. this has nothing to do with total net worth."
    #
    # `claim_pct` SURVIVES for the pane header's "N% claimed by bets", which is a different
    # and load-bearing figure: over 100% means more has been reserved than the book holds.
    # Making this one of-total would have turned that into a tautological 100%.
    of_reserved_pct: float | None = None
    # AT COST, matching Bet.filled_pct -- see its docstring for why the basis changed.
    cost: float | None = None         # open legs at entry cost, home currency
    filled_pct: float | None = None   # cost as a share of this bet's own allocation

    def of_bet(self, leg) -> float | None:
        """One leg as a share of the bet's ALLOCATION, which is the denominator the author chose.

        Of the allocation rather than of the spent total, so the shares sum to the bet's
        FILLED percentage and the gap is budget not yet spent. A leg under no bet has no
        allocation to be a share of, and gets None rather than a share of something else.

        AT COST, because `filled_pct` is: these shares are supposed to sum TO it. At market
        they summed to a different number than the header printed, which is the same
        two-answers-for-one-word defect one level down.
        """
        if leg.cost_base is None or not self.allocation:
            return None
        return 100.0 * leg.cost_base / self.allocation


def portfolio(con) -> list[PortfolioGroup]:
    """What the money is in, grouped by bet, largest first. The dashboard's main content.

    The author, 2026-09-24: "the dashboard is essentially the portfolio tab. we should see live
    bets, their share of the portfolio, each trades within each bet. i need this knowledge
    to know what more to add to each trade."

    NOT THE SAME VIEW AS TAB 2, which is the question this has to answer to deserve the
    space. Tab 2 groups the same legs under the same bets, but its subject is the POSITION:
    what it cost, what it is worth now, what it has made. Here the subject is the SHARE --
    every figure is a percentage of derived net, so the question is "how is my money
    allocated" rather than "how is this trade doing". That is why `no bet` is a first-class
    group here and reads as an answer (58% parked) rather than as a gap.

    WEIGHTS ARE OF NET, not of the bets' total, and that is deliberate on this book: 58%
    of the author's net sits in money-market funds, so a share-of-the-bets view would show the
    equity bets at four times their real weight and hide the single biggest fact about the
    portfolio.

    A GROUP WITH AN UNVALUABLE LEG REPORTS None, and says how many. Summing the ones that
    can be valued and printing that as the group's worth is the `or 0.0` mistake: the figure
    would be confidently short by the size of whatever it skipped.
    """
    n = net(con)
    names = {b.id: (b.name, b.status) for b in bets(con)}
    allocs = {b.id: b.allocation for b in bets(con)}
    grouped: dict[str | None, list] = {}
    for p in trades(con):
        grouped.setdefault(p.bet_id, []).append(p)

    # EVERY ALLOCATION THAT IS SHOWN HERE, so the shares below sum to 100% of what the
    # reader can see. Summed over the groups on this table rather than over every bet in the
    # book: a bet with no open legs has no row, and dividing by budgets that are not on
    # screen would give a column that silently fails to add up.
    reserved = sum(v for k, v in allocs.items() if k in grouped and v)

    out = []
    for bet_id, legs in grouped.items():
        valued = [x for x in legs if x.value_base is not None]
        blind = len(legs) - len(valued)
        value = sum(x.value_base for x in valued) if valued and not blind else None
        # WITHHELD ON ANY MISSING COST, like `value` -- a group whose spend is short by one
        # leg must not report a confident filled percentage.
        cost = (None if any(x.cost_base is None for x in legs)
                else sum(x.cost_base for x in legs))
        name, status = names.get(bet_id, (bet_id or "no bet", None))
        alloc = allocs.get(bet_id)
        out.append(PortfolioGroup(
            bet_id=bet_id, name=name if bet_id else "no bet", status=status,
            value=value,
            weight_pct=(None if value is None or not n.net else 100.0 * value / n.net),
            legs=tuple(sorted(legs, key=lambda x: x.value_base or 0.0, reverse=True)),
            unvalued=blind,
            allocation=alloc,
            claim_pct=(None if not alloc or not n.net else 100.0 * alloc / n.net),
            of_reserved_pct=(None if not alloc or not reserved
                             else 100.0 * alloc / reserved),
            cost=cost,
            filled_pct=(None if not alloc or cost is None else 100.0 * cost / alloc)))
    # LARGEST FIRST, with the unvaluable groups last rather than sorted as zero -- a group
    # whose worth is unknown is not a small group.
    return sorted(out, key=lambda g: (g.value is None, -(g.value or 0.0)))


@dataclass(frozen=True)
class BetComposition:
    """What a bet is made of, per ticker, against the capital it declared.

    The author: "i want to view ticker composition within a bet (like it takes up xx%)",
    and they chose the DECLARED ALLOCATION as the denominator over spent capital. That
    choice is
    what makes `dry` part of the answer rather than a rounding remainder: shares sum to the
    bet's FILLED percentage and the gap is budget they have not spent, which is a fact about
    the bet that a share-of-spend view cannot express at all.
    """
    bet_id: str
    allocation: float | None              # None means none declared, so there is no
    currency: str | None                  # denominator and the pane must say so
    legs: tuple[tuple[str, float], ...]   # (ticker, cost in the HOME currency), largest first
    dry: float | None                     # allocation less the legs; negative means over
    unpriced: tuple[str, ...]             # tickers left out for want of a cost

    @property
    def spent(self) -> float:
        """Entry cost of every leg. WAS `deployed`, renamed with Bet.value -- "deployed" was
        being used for market value one class up, so it named two different quantities."""
        return sum(v for _, v in self.legs)


def bet_composition(con, bet_id: str) -> BetComposition:
    """Per-TICKER cost under one bet, in the home currency, largest first.

    THE HOME CURRENCY, NOT NATIVE, and here that is forced rather than chosen: the figures
    are being compared to `allocation_base`, which is a single home-currency number. A bet
    can hold USD and CAD legs at once -- 2026-q4-reverse holds both -- so native costs would
    be a set of amounts in different units with no common denominator to take a share of.
    This is the one place on the desk where translating is the only honest option.

    AGGREGATED BY TICKER, because the question is about tickers. The same instrument can sit
    in two accounts under one bet -- AEM.TO is in both rrsp and tfsa -- and those are one
    holding as far as "what is this bet made of" is concerned.

    A LEG WITH NO COST IS NAMED, NOT ZEROED. `cost_base` is None when the entry price or the
    rate is missing, and counting it as nothing would shrink every other ticker's share
    while the bars still summed to a confident 100%.
    """
    row = con.execute("SELECT allocation_base, allocation_ccy FROM bets WHERE id=?",
                      (bet_id,)).fetchone()
    if row is None:
        raise DeskError(f"no bet {bet_id}")
    # SAME GUARD AS bets(): the legs below are translated to the home currency, so an
    # allocation stamped in a different one is not a denominator. See bets().
    home = base_currency(con)
    by_ticker: dict[str, float] = {}
    unpriced: list[str] = []
    for p in trades(con):
        if p.bet_id != bet_id:
            continue
        if p.cost_base is None:
            unpriced.append(p.ticker)
            continue
        by_ticker[p.ticker] = by_ticker.get(p.ticker, 0.0) + p.cost_base
    legs = tuple(sorted(by_ticker.items(), key=lambda kv: kv[1], reverse=True))
    alloc = row["allocation_base"] if row["allocation_ccy"] == home else None
    # WITHHELD WHEN A LEG HAS NO COST, not computed from the ones that do. `dry` used to
    # subtract only the legs it could price, so a leg with no recorded cost was silently
    # re-labelled as UNSPENT BUDGET -- a bet holding US$9,500 with no fx row reported
    # spent 0 and dry 20,000, and the HOLDINGS bar drew 100% of the allocation as cash.
    # That is this project's signature defect, in the one function whose docstring promises
    # the opposite ("A LEG WITH NO COST IS NAMED, NOT ZEROED").
    dry = None if alloc is None or unpriced else alloc - sum(v for _, v in legs)
    return BetComposition(bet_id=bet_id, allocation=alloc,
                          currency=row["allocation_ccy"], legs=legs, dry=dry,
                          unpriced=tuple(sorted(set(unpriced))))


def set_trade_bet(con, trade_id: str, *, bet_id: str | None) -> dict:
    """Move a whole leg under a bet, or out from under one. Returns the pre-image.

    AN UPDATE, NOT A FILL REWRITE, and that distinction is the bug this replaces. The only
    way to re-point a leg used to be `enter` on its newest fill with a different `!bet` --
    which deletes the fill and re-adds it, and add_fill matches on (bet, ticker, account),
    so the fill landed in a NEW trade and the old one was left empty. On a leg of ONE fill
    that produced a phantom open position; on a leg of several it would have moved the
    newest fill and stranded the rest under the old bet, splitting one position in two
    without saying so.

    A leg's bet is a property of the LEG. Nothing about the fills changes when the author decides
    that GOOG was part of the reverse trade after all, so nothing about the fills should be
    touched to record it.

    `bet_id=None` detaches: a position needs no bet ("this has nothing to do with any bet"
    -- The author on parking cash), so the absence of one is a legitimate destination, not a
    refusal.
    """
    row = con.execute("SELECT id, bet_id, status FROM trades WHERE id=?",
                      (trade_id,)).fetchone()
    if row is None:
        raise DeskError(f"no trade {trade_id}")
    if bet_id is not None and con.execute(
            "SELECT 1 FROM bets WHERE id=?", (bet_id,)).fetchone() is None:
        raise DeskError(f"no bet {bet_id} — press n on the bets tab to declare it first")
    if row["bet_id"] == bet_id:
        where = f"already under {bet_id}" if bet_id else "already under no bet"
        raise DeskError(f"{trade_id} is {where}")
    pre = dict(row)
    con.execute("UPDATE trades SET bet_id=? WHERE id=?", (bet_id, trade_id))
    return pre


def set_bet_status(con, bet_id: str, *, status: str, on_date: str,
                   note: str | None = None) -> dict:
    """Move a bet between forming / live / weakened / falsified / closed / retired /
    abandoned. Returns the pre-image so the move is undoable.

    THERE WAS NO WRITER FOR THIS AT ALL. ai-capex-semis was closed and 2026-q4-reverse was
    made live by hand-written SQL, which is the gap the author asked about directly: "how do i
    change the bet status?"

    Every refusal below is a wall the SCHEMA already enforces, restated so it arrives as a
    sentence instead of a raw IntegrityError -- except the open-legs one, which the schema
    does NOT enforce and should: a bet marked closed while a leg is still open says the
    thing is finished while money is at risk.

    `live` USED TO REQUIRE A THESIS, a main_support and a main-support falsifier, refusing
    with a sentence naming what was missing. That gate is GONE, in Python and in the schema
    (v13 drops `bet_live_needs_falsifier_ins` and `_upd`). The author, 2026-09-24: "i cant put
    semis-and-big-techs to live bc of this. i think we are still enforcing rules which
    shouldnt be at this stage. let me explore the best shape of those rules before
    committing too early."

    It was the last piece of enforcement left, and it was the same mistake as the loss-limit
    curve and the `i` key one layer up: the tool deciding when they are allowed to record what
    already happened. A status is an OBSERVATION about a bet, and a bet they are running is
    live whether or not the prose has caught up. Refusing to record it does not produce a
    thesis; it produces a book that disagrees with reality, and a user who stops trusting
    the book. `?` and the bets table still show an empty thesis as empty, which is the
    honest version of the same nudge.
    """
    row = con.execute(
        "SELECT id, status, opened, closed, thesis, main_support FROM bets WHERE id=?",
        (bet_id,)).fetchone()
    if row is None:
        raise DeskError(f"no bet {bet_id}")
    if status not in BET_STATUSES:
        raise DeskError(f"{status!r} is not a status — {', '.join(BET_STATUSES)}")
    if status == row["status"]:
        raise DeskError(f"{bet_id} is already {status}")

    open_legs = [r[0] for r in con.execute(
        "SELECT id FROM trades WHERE bet_id=? AND status='open' ORDER BY id", (bet_id,))]
    if status in _BET_OVER and open_legs:
        raise DeskError(
            f"{bet_id} still holds {len(open_legs)} open leg(s): "
            f"{', '.join(open_legs)}. Close the positions first — a bet marked "
            f"{status} while money is at risk is a book that contradicts itself.")

    if status == "falsified" and not note:
        raise DeskError(
            "falsified is the strongest thing this book can say about a claim, so it "
            "needs a sentence: add ~why. Which falsifier broke, and how.")

    pre = dict(row)
    opened = row["opened"] or (None if status == "forming" else on_date)
    closed = row["closed"]
    if status in ("closed", "falsified"):
        closed = on_date
        if opened and closed < opened:
            raise DeskError(
                f"{bet_id} opened on {opened}, so it cannot close on {closed}")
    con.execute(
        "UPDATE bets SET status=?, opened=?, closed=?, "
        "notes = CASE WHEN ? IS NULL THEN notes "
        "             ELSE COALESCE(notes || char(10) || char(10), '') || ? END "
        "WHERE id=?",
        (status, opened, closed, note,
         f"{on_date}: → {status}. {note}" if note else None, bet_id))
    return pre


# The key delete_fill smuggles a trades pre-image under, inside a fills pre-image. A
# name rather than a literal because restore() has to strip exactly this one back out,
# and a typo in either place is an undo that silently half-works.
_ORPHANED_TRADE = "__trade__"

# The same trick for the rows SQLite deletes on our behalf. `confirmations` declares
# `REFERENCES trades(id) ON DELETE CASCADE`, and foreign keys are ON for every connection
# this module opens -- so deleting an emptied trade silently removed them, and the pre-image
# carried only the trade and the fill. On the live book one OPEN trade holds three
# confirmations rows, so `x` on its last fill destroyed three records of a known hole in the
# record, with no message, and `u` reported "restored the fills row" while they stayed gone.
#
# `falsifiers` WAS THE SECOND MEMBER and left in v17 with falsifiers.trade_id. A falsifier
# hangs off a BET now, so deleting a trade cannot cascade one away and there is nothing for
# the pre-image to carry. Leaving it here was not harmless: the SELECT below names
# `trade_id` on every child, so it raised `no such column` and took six delete/undo paths
# with it.
_ORPHANED_CHILDREN = "__children__"
_CASCADE_OFF_TRADES = ("confirmations",)


def delete_fill(con, fill_id: int) -> dict | None:
    """Returns the pre-image so undo can restore it.

    A TRADE THAT LOST ITS LAST FILL GOES WITH IT. An open trade with no fills is a
    position made of nothing, and check.py has always called that a FAILURE twice over
    ("an open trade with no fills", "an open trade holding nothing") -- so the desk was
    writing state its own checker rejects.

    The author hit it by moving GOOG to a bet. `enter` edits a fill by delete-then-add, and
    add_fill matches on (bet, ticker, account), so changing `!bet` moved the fill to a NEW
    trade and left the old one empty and unreachable: `x` on it answers "has no fills to
    delete", so nothing in the TUI could clean it up. sqlite3 was the only way out.

    FIXED HERE, not in the pane, because both routes to the hole run through this function
    -- the edit path and `x` on a single-fill leg. One guard covers both, and it also makes
    `x` mean "delete a position" on the last fill without inventing a key for it.

    ONLY WHEN status='open'. A `planned` trade legitimately has no fills yet -- that is
    what planned means -- and a closed one is history that must not evaporate because its
    last fill was corrected.
    """
    row = con.execute("SELECT * FROM fills WHERE id=?", (fill_id,)).fetchone()
    if row is None:
        return None
    con.execute("DELETE FROM fills WHERE id=?", (fill_id,))
    pre = dict(row)
    left = con.execute("SELECT COUNT(*) FROM fills WHERE trade_id=?",
                       (row["trade_id"],)).fetchone()[0]
    if left == 0:
        empty = con.execute("SELECT * FROM trades WHERE id=? AND status='open'",
                            (row["trade_id"],)).fetchone()
        if empty is not None:
            # READ THE CASCADE CHILDREN BEFORE THE DELETE, because after it they are gone
            # and there is nothing left to read. Captured even when empty so restore() has
            # one shape to handle.
            kids = {}
            for child in _CASCADE_OFF_TRADES:
                got = con.execute(f"SELECT * FROM {child} WHERE trade_id=?",
                                  (row["trade_id"],)).fetchall()
                if got:
                    kids[child] = [dict(r) for r in got]
            con.execute("DELETE FROM trades WHERE id=?", (row["trade_id"],))
            # CARRIED IN THE PRE-IMAGE so one `u` puts back what one keystroke removed.
            # The fill cannot be re-inserted without its trade -- fills.trade_id is a
            # NOT NULL foreign key -- so restoring them separately is not an option, and
            # two undo entries for one action would mean pressing `u` twice.
            pre[_ORPHANED_TRADE] = dict(empty)
            if kids:
                pre[_ORPHANED_CHILDREN] = kids
    return pre


def delete_flow(con, flow_id: int) -> dict | None:
    row = con.execute("SELECT * FROM flows WHERE id=?", (flow_id,)).fetchone()
    if row is None:
        return None
    con.execute("DELETE FROM flows WHERE id=?", (flow_id,))
    return dict(row)


def delete_adjustment(con, adjustment_id: int) -> dict | None:
    row = con.execute("SELECT * FROM adjustments WHERE id=?",
                      (adjustment_id,)).fetchone()
    if row is None:
        return None
    con.execute("DELETE FROM adjustments WHERE id=?", (adjustment_id,))
    return dict(row)


# The only tables undo may touch. Both restore() and delete_row() interpolate the
# table NAME into SQL, which a bound parameter cannot carry, so the set of legal names
# is closed here rather than trusted from the caller.
UNDOABLE = {"fills": delete_fill, "flows": delete_flow,
            "adjustments": delete_adjustment}


def delete_row(con, table: str, row_id: int) -> dict | None:
    fn = UNDOABLE.get(table)
    if fn is None:
        raise DeskError(f"{table} is not an undoable table")
    return fn(con, row_id)


def restore(con, table: str, row: dict) -> None:
    if table not in UNDOABLE:
        raise DeskError(f"{table} is not an undoable table")
    # THE TRADE FIRST, AND IN THE SAME CALL. delete_fill removes a trade its last fill
    # emptied, and `fills.trade_id` is a NOT NULL foreign key -- so re-inserting the fill
    # against a trade that is gone fails, and undo that raises is undo that does nothing.
    # Order is the whole point: parent, then child.
    row = dict(row)
    parent = row.pop(_ORPHANED_TRADE, None)
    kids = row.pop(_ORPHANED_CHILDREN, None)
    # THE ROW'S OWN id IS DROPPED, so SQLite assigns a fresh one. Re-asserting it was a
    # latent UNIQUE failure with a window that opens the moment you keep working: `fills.id`
    # is an INTEGER PRIMARY KEY with no AUTOINCREMENT, so deleting the highest row FREES that
    # rowid and the next INSERT takes it. x on a leg, then n to record anything, then u --
    # and the trades INSERT above has already run when the fills INSERT hits the collision,
    # leaving an open trade holding zero fills: the state delete_fill exists to prevent, and
    # one `x` cannot clear because it answers "has no fills to delete".
    #
    # Safe because nothing REFERENCES fills, flows or adjustments by id -- checked against
    # every foreign key in schema.sql. What undo promises is that the ROW comes back, not
    # that it comes back with the same surrogate key, and the id was never shown anywhere.
    row.pop("id", None)
    if parent is not None:
        cols = ",".join(parent)
        marks = ",".join("?" for _ in parent)
        con.execute(f"INSERT INTO trades ({cols}) VALUES ({marks})",
                    tuple(parent.values()))
    cols = ",".join(row)
    marks = ",".join("?" for _ in row)
    con.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
    # THE CASCADE CHILDREN, after their parent. `confirmations` and `falsifiers` both
    # reference trades(id) ON DELETE CASCADE, so deleting an emptied trade took them with it
    # silently -- see delete_fill. They are restored by hand because the cascade that removed
    # them leaves nothing to undo.
    for child, rows in (kids or {}).items():
        for r in rows:
            r = {k: v for k, v in r.items() if k != "id"}
            cols = ",".join(r)
            marks = ",".join("?" for _ in r)
            con.execute(f"INSERT INTO {child} ({cols}) VALUES ({marks})", tuple(r.values()))


# Tables whose rows are UPDATED in place rather than created and deleted. Undoing one of
# those is an UPDATE back to the pre-image, not an INSERT -- putting a trades pre-image
# through restore() above would try to insert a row that still exists and fail on the
# primary key, which is undo silently not working, the exact defect this project already
# shipped once.
# `falsifiers` JOINED IN v18, when resolving one became possible. A resolution is two
# fields on an existing row, so it undoes the same way a rename does. It is NOT in UNDOABLE
# above and must not be: that map is table -> DELETE function, and a falsifier should never
# be removed by pressing `u` -- a trigger already refuses to delete the only main-support
# falsifier of a live bet, and undo has no business arguing with it.
UPDATABLE = {"trades": "id", "bets": "id", "falsifiers": "id"}


def restore_update(con, table: str, row: dict) -> None:
    key = UPDATABLE.get(table)
    if key is None:
        raise DeskError(f"{table} is not an in-place-updatable table")
    if key not in row:
        raise DeskError(f"the {table} pre-image has no {key} to update by")
    sets = ",".join(f"{c}=?" for c in row if c != key)
    if not sets:
        return
    con.execute(f"UPDATE {table} SET {sets} WHERE {key}=?",
                tuple(v for c, v in row.items() if c != key) + (row[key],))
