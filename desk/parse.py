"""The entry grammar. Pure: no Textual, no database, no clock of its own.

Every write in the desk goes through one line of text rather than a form, which is
the friction argument: registering a fill is `n`, then one line, then enter.

    ACME 10 12.34 !example-bet @2026-01-15 @14:32 =4.95 ~first tranche
    ACME -4 13.00 !example-bet @today           (a trim: negative quantity)
    1000 @2026-01-15 ~initial funding           (capital in)

The ticker is required even on a trim. Dropping it would make the leading token
ambiguous between a ticker and a quantity, and the whole reason the leading tokens
are strictly positional is that they are unambiguous.

SHAPE COPIED FROM daylogs/sigil.py + parse.py, deliberately. Its rules are not
arbitrary -- each one closes a hole:

  * A sigil counts only at the START of a whitespace token, so `50% off` and `a!b`
    need no escaping. `\\!` escapes a leading one.
  * The leading tokens are positional. Strictly positional, not "the first number
    that survives field extraction" -- that is what makes a number inside a note safe.
  * Exactly one sigil (`~`) may span spaces, so every other field stays one token
    and the grammar stays decidable.
  * A sigil the entity does not consume is REJECTED, never silently dropped.
  * parse(render(row)) == row is a tested property, so an edit can prefill.

`now` is injected rather than read, because a default clock is how a timezone
mismatch hides.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass

# THE CONSTANT ONLY. config.py does no work at import time, so this costs nothing and
# keeps one list of currencies rather than two that can disagree. Nothing here calls
# profile() -- that reads a file, and this module stays pure.
from .config import CURRENCIES

# `/` is the account. It cannot begin a ticker, a number or a date, so adding it
# breaks nothing already typeable. Everything is recorded against the default
# account unless a `/` says otherwise -- see book.resolve_account.
SIGILS = "!@~=/"
ESCAPE = "\\"


class ParseError(ValueError):
    """Shown verbatim in the prompt, so it must read as guidance, not a stack trace."""


@dataclass(frozen=True)
class Token:
    sigil: str      # "" for a plain token
    value: str      # sigil stripped, escape resolved
    start: int
    end: int


def tokenise(raw: str) -> list[Token]:
    out: list[Token] = []
    for m in re.finditer(r"\S+", raw):
        text, start = m.group(0), m.start()
        if text[0] == ESCAPE and len(text) > 1 and text[1] in SIGILS:
            out.append(Token("", text[1:], start, m.end()))
        elif text[0] in SIGILS:
            out.append(Token(text[0], text[1:], start, m.end()))
        else:
            out.append(Token("", text, start, m.end()))
    return out


def _fold_tilde(toks: list[Token], *, greedy: bool = True) -> list[Token]:
    """`~` absorbs the words after it. The one multi-word field.

    THE ABSORBED WORDS KEEP THE SIGIL THEY WERE TYPED WITH. tokenise() has already split
    `=default` into sigil `=` and value `default`, so joining bare values turned
    `nonreg ~Non-registered =default` into the name "Non-registered default" -- a
    character the author typed, gone without a word. Notes are free text and have to survive
    intact: `~bought @ the dip` means what it says.

    greedy=False stops at the next sigil instead, for the grammars whose `~` field is a
    NAME rather than a note. That is what made the silent loss above possible: `a` takes
    `~name` and `=default`, its own hint lists them in that order, and in that order the
    flag did nothing.
    """
    for i, t in enumerate(toks):
        if t.sigil != "~":
            continue
        rest, tail = toks[i + 1:], []
        if not greedy:
            stop = next((j for j, x in enumerate(rest) if x.sigil), len(rest))
            rest, tail = rest[:stop], rest[stop:]
        joined = " ".join([t.value] + [x.sigil + x.value for x in rest]).strip()
        end = rest[-1].end if rest else t.end
        return toks[:i] + [Token("~", joined, t.start, end)] + tail
    return toks


@dataclass(frozen=True)
class Grouped:
    plain: list[str]
    by_sigil: dict[str, list[str]]


def group(toks: list[Token], *, tilde_is_a_name: bool = False) -> Grouped:
    plain: list[str] = []
    by: dict[str, list[str]] = {}
    for t in _fold_tilde(toks, greedy=not tilde_is_a_name):
        if t.sigil:
            by.setdefault(t.sigil, []).append(t.value)
        else:
            plain.append(t.value)
    return Grouped(plain, by)


def _reject_unsupported(g: Grouped, allowed: set[str], what: str) -> None:
    for s in g.by_sigil:
        if s not in allowed:
            raise ParseError(f"a {what} has no {s} field — drop {s}")


def _one(g: Grouped, sigil: str, name: str) -> str | None:
    vals = g.by_sigil.get(sigil, [])
    if len(vals) > 1:
        raise ParseError(f"only one {name} — you gave {len(vals)}")
    return vals[0] if vals else None


_THOUSANDS = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")


def _number(text: str, what: str) -> float:
    # A COMMA IS A THOUSANDS SEPARATOR OR IT IS AN ERROR. Stripping every comma turned a
    # DECIMAL comma into a hundredfold figure: '1,50' parsed as 150.0 and was written as
    # a price, which no later report could distinguish from a real one.
    if "," in text and not _THOUSANDS.match(text):
        raise ParseError(
            f"{what}: a comma is a thousands separator here, so {text!r} is ambiguous "
            f"— write 1,234.56 or 1234.56")
    try:
        value = float(text.replace(",", ""))
    except ValueError:
        raise ParseError(f"{what} must be a number, not {text!r}") from None
    # nan AND inf BOTH GOT THROUGH. float() accepts them, and nan then slips past every
    # `> 0` guard downstream because any comparison with nan is False -- so `nan` was
    # accepted as a price by a check whose whole job was to require a positive one. inf
    # poisons every total it reaches instead.
    if not math.isfinite(value):
        raise ParseError(f"{what} must be a finite number, not {text!r}")
    return value


def _account(g: Grouped, known: frozenset[str]) -> str | None:
    """`/tfsa`. None means the caller should fall back to the default account.

    Validated here rather than at the database, because `trades.account_id` is
    nullable: a typo would otherwise be stored as "no account" and the position
    would vanish from every per-account total without anything failing.
    """
    raw = _one(g, "/", "account")
    if raw is None:
        return None
    acct = raw.lower()
    if known and acct not in known:
        raise ParseError(f"/{raw} is not an account — one of: {', '.join(sorted(known))}")
    return acct


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A bet id is the ONE string shared with wherever the thesis is written, so it has
# to be something the author will retype identically: lowercase words, single hyphens.
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def resolve_when(values: list[str], *, now: dt.datetime) -> str:
    """`@` carries a date, or the word today/yesterday. Returns the date.

    Absent date means today -- a fill you are typing is nearly always today's, and
    making the common case silent is the point of the whole grammar.

    IT CARRIED A TIME TOO, and returned (date, time). Of 48 recorded fills, ZERO had one,
    and seven of this function's eight callers discarded it with `on_date, _ =`. A broker
    fill does happen at a time, but this book reconciles against statements dated to the
    day, so nothing here could check that time or use it. `@14:32` is now an error rather
    than a value that goes into the book and stays invisible.
    """
    date: str | None = None
    for v in values:
        low = v.lower()
        if low == "today":
            date = now.strftime("%Y-%m-%d")
        elif low == "yesterday":
            date = (now - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        elif _DATE.match(v):
            # THE SHAPE IS NOT THE DATE. This matched the pattern and stopped, so
            # @2026-09-31, @2026-02-30, @2026-13-45 and @2026-00-10 all passed -- and
            # the schema's own guard is a GLOB on the shape too, so the row was
            # committed. When such a date ends up as the earliest sleeve read,
            # balance() and obligations() both raise "day is out of range for month" on
            # every call, the dashboard calls both on mount, and the desk will not open:
            # there is no delete key on that tab and undo only restores what a delete
            # stacked, so the book cannot be repaired from inside the tool.
            try:
                dt.date.fromisoformat(v)
            except ValueError:
                raise ParseError(f"@{v} is not a real date") from None
            date = v
        else:
            raise ParseError(f"@{v} is not a date — YYYY-MM-DD, today, or yesterday")
    return date or now.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# a fill
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FillInput:
    ticker: str
    shares: float          # signed: positive adds, negative reduces
    price: float
    bet_id: str | None
    on_date: str
    fee: float | None
    note: str | None
    account: str | None = None      # None means "the default account"


def parse_fill(raw: str, *, now: dt.datetime,
               known_tickers: frozenset[str] = frozenset(),
               known_bets: frozenset[str] = frozenset(),
               known_accounts: frozenset[str] = frozenset()) -> FillInput:
    toks = tokenise(raw)
    if not toks:
        raise ParseError("nothing to record")
    g = group(toks)
    _reject_unsupported(g, {"!", "@", "~", "=", "/"}, "fill")

    if len(g.plain) < 3:
        raise ParseError("need ticker, quantity and price — e.g. ACME 10 12.34")
    if len(g.plain) > 3:
        # Name only the EXTRAS. Echoing the valid three back reads as if they were
        # the problem.
        extra = " ".join(g.plain[3:])
        raise ParseError(
            f"ticker, quantity, price — then sigils. Did not know what to do with "
            f"{extra!r}: a note needs ~, a bet needs !")

    ticker = g.plain[0].upper()
    # THE REGISTRY CHECK WAS HERE and it went with `i`. It refused an unknown ticker
    # with "press i to register it", and `i` no longer exists -- the author: "The user should
    # never worry about ticker registration." The pane resolves the ticker at the price
    # source instead, which is a stronger typo check than a local list.
    #
    # `known_tickers` is still accepted so tab-completion can offer what the book holds,
    # but it no longer REFUSES anything: a ticker absent from it is new, not wrong.

    shares = _number(g.plain[1], "quantity")
    if shares == 0:
        raise ParseError("quantity must be non-zero — negative reduces the position")
    price = _number(g.plain[2], "price")
    if price <= 0:
        raise ParseError("price must be positive")

    # `!bet` IS OPTIONAL. A named bet must exist, but naming none is a position that
    # expresses no claim -- cash parking, a bond ladder -- and that is a fact about the
    # trade rather than a gap in it.
    bet = _one(g, "!", "bet")
    if bet and known_bets and bet not in known_bets:
        raise ParseError(f"!{bet} is not a bet — one of: {', '.join(sorted(known_bets))}")

    fee_raw = _one(g, "=", "fee")
    fee = _number(fee_raw, "fee") if fee_raw is not None else None
    if fee is not None and fee < 0:
        raise ParseError("a fee is not negative")

    account = _account(g, known_accounts)
    on_date = resolve_when(g.by_sigil.get("@", []), now=now)
    return FillInput(ticker, shares, price, bet, on_date, fee,
                     _one(g, "~", "note"), account)


def _num(value: float) -> str:
    """The SHORTEST text that parses back to exactly this float.

    render_* used `:g`, which gives six significant digits -- so a prefilled edit was
    lossy above six figures and the loss was silent, because the whole point of the
    prefill is that you press enter without retyping it:

        1,234,567.89  ->  '1.23457e+06'  ->  1,234,570.00   (2.11 gone)
        12,345.67 as a price -> '12345.7'                    (a cent gone per share)

    repr() of a float is the shortest string that round-trips exactly, which is the
    property this needs; the trailing '.0' is dropped only because a whole number reads
    better in a prompt, and '45002' still parses to 45002.0.
    """
    s = repr(float(value))
    return s[:-2] if s.endswith(".0") else s


def render_fill(row: dict) -> str:
    """The inverse, for prefilling an edit. `~` renders last: it absorbs what follows."""
    parts = [row["ticker"], _num(row["shares"]), _num(row["price"])]
    if row.get("bet_id"):
        parts.append(f"!{row['bet_id']}")
    if row.get("on_date"):
        parts.append(f"@{row['on_date']}")
    if row.get("fee") is not None:
        parts.append(f"={_num(row['fee'])}")
    if row.get("account"):
        parts.append(f"/{row['account']}")
    if row.get("note"):
        parts.append(f"~{row['note']}")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# capital in or out
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FlowInput:
    amount: float          # signed: positive deposit, negative withdrawal
    on_date: str
    note: str | None
    account: str | None = None
    currency: str | None = None   # None = the home currency, from the profile


def _currency(g: Grouped, what: str) -> tuple[str | None, list[str]]:
    """Pull a bare CAD or USD out of the plain words. Returns (currency, what is left).

    A BARE WORD RATHER THAN A SIGIL, deliberately, and it is the only new vocabulary v11
    added to the grammar. `1000 USD` reads as what it is; `1000 =USD` would be a sixth
    meaning for `=`, and the author's standing complaint is that there are already too many:
    "your entry system is way too complicated. way too complicated for the user."

    It is unambiguous because neither CAD nor USD can be a number, an account or a date,
    and because the pair is closed -- config.CURRENCIES is two entries long.
    """
    rest, found = [], None
    for word in g.plain:
        up = word.upper()
        if up in CURRENCIES:
            if found is not None and found != up:
                raise ParseError(f"one currency per {what} — you gave {found} and {up}")
            found = up
        else:
            rest.append(word)
    return found, rest


def parse_flow(raw: str, *, now: dt.datetime,
               known_accounts: frozenset[str] = frozenset()) -> FlowInput:
    toks = tokenise(raw)
    if not toks:
        raise ParseError("nothing to record")
    g = group(toks)
    _reject_unsupported(g, {"@", "~", "/"}, "capital flow")
    currency, plain = _currency(g, "flow")
    if not plain:
        raise ParseError("need an amount — e.g. 45000 @2026-08-23 ~initial funding")
    if len(plain) > 1:
        raise ParseError(f"too many bare words ({' '.join(plain)}) — a note needs ~")
    amount = _number(plain[0], "amount")
    if amount == 0:
        raise ParseError("amount must be non-zero — negative is a withdrawal")
    account = _account(g, known_accounts)
    on_date = resolve_when(g.by_sigil.get("@", []), now=now)
    return FlowInput(amount, on_date, _one(g, "~", "note"), account, currency)


def render_flow(row: dict, *, home: str | None = None) -> str:
    parts = [_num(row["amount"])]
    # ONLY WHEN IT IS NOT THE HOME CURRENCY. render() feeds the edit prefill, and printing
    # a redundant CAD on every CAD deposit would teach a word nobody needs to type.
    #
    # `home` is PASSED IN rather than read from the profile, because this module is pure
    # and profile() opens a file. The caller has it already.
    if row.get("currency") and row["currency"] != home:
        parts.append(row["currency"])
    parts.append(f"@{row['on_date']}")
    if row.get("account"):
        parts.append(f"/{row['account']}")
    if row.get("note"):
        parts.append(f"~{row['note']}")
    return " ".join(parts)


@dataclass(frozen=True)
class AdjustmentInput:
    amount: float          # signed: what to add to the account's cash
    on_date: str
    note: str | None
    account: str | None = None
    currency: str | None = None


def parse_adjustment(raw: str, *, now: dt.datetime,
                     known_accounts: frozenset[str] = frozenset()) -> AdjustmentInput:
    """`-4.95 @today ~a fee` — one signed correction to cash.

    THE ONLY DOOR FOR DIVIDENDS, FEES AND INTEREST. The author: "we will have some offsets as we
    accumulate. probably from dividends and fees. but they are just way too little to
    track, we will just add a special offset entry into the account balance as i gave you
    the number to reconcile the difference. that's way easier. if the diff is small, it's
    good enough."

    Same shape as a flow on purpose -- one grammar learned twice rather than two -- and
    the difference is what it MEANS. A flow is capital in or out and moves the
    denominator the return is measured against. An offset is money the account earned or
    paid, and moves the numerator. Recording a dividend as a deposit would hide exactly
    that much P&L.
    """
    toks = tokenise(raw)
    if not toks:
        raise ParseError("nothing to record")
    g = group(toks)
    _reject_unsupported(g, {"@", "~", "/"}, "offset")
    currency, plain = _currency(g, "offset")
    if not plain:
        raise ParseError("need an amount — e.g. -4.95 @today ~a fee")
    if len(plain) > 1:
        raise ParseError(f"too many bare words ({' '.join(plain)}) — a note needs ~")
    amount = _number(plain[0], "amount")
    if amount == 0:
        raise ParseError("amount must be non-zero — negative takes cash out")
    account = _account(g, known_accounts)
    on_date = resolve_when(g.by_sigil.get("@", []), now=now)
    return AdjustmentInput(amount, on_date, _one(g, "~", "note"), account, currency)


def render_adjustment(row: dict, *, home: str | None = None) -> str:
    return render_flow(row, home=home)


# --------------------------------------------------------------------------- #
# an account, and the book's as-of date
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AccountInput:
    account_id: str
    name: str | None
    make_default: bool


def parse_account(raw: str, *, known_accounts: frozenset[str] = frozenset()
                  ) -> AccountInput:
    """`fhsa ~First Home Savings` — an id, and that is the only required word.

    IT USED TO NEED A KIND AND A CURRENCY: `fhsa fhsa CAD ~First Home Savings`. Both left
    the schema in v11 -- kind was a Canadian tax taxonomy read by nothing, and a single
    currency per account was simply false for a sleeve holding CAD and USD at once. Losing
    them takes two positional words and a closed vocabulary out of this grammar, which is
    the direction the author asked for: "your entry system is way too complicated."

    `default` MARKS THE SLEEVE UNQUALIFIED WRITES LAND IN, and it is a BARE WORD now.
    It was `=default`, which made this the only place `=` carried a flag rather than an
    amount -- one of the five different things that sigil meant, and the odd one out. `=`
    now means money in three grammars (a fee, a stop price, a bet's allocation) and a quote
    symbol in one, which the instrument docstring calls out as the single exception.

    The author: "your entry system is way too complicated. way too complicated for the user."
    """
    toks = tokenise(raw)
    if not toks:
        raise ParseError("nothing to record")

    # TAKEN OUT AT THE TOKEN LEVEL, BEFORE THE TILDE FOLDS. `~` is the one field that spans
    # spaces, and for a name it runs to the next SIGIL -- so `nonreg ~Non-registered default`
    # put the flag INSIDE the name and silently set no default. That is the identical failure
    # `=default` had for the identical reason, one layer down: the flag has to be removed
    # before anything can absorb it.
    #
    # The cost, stated because it is real: an account NAME cannot contain the standalone word
    # "default". Against that, the flag works wherever it is typed -- which is what the hint
    # promises, and promising an order that does not work is how this went wrong twice.
    make_default = any(not t.sigil and t.value.lower() == "default" for t in toks)
    toks = [t for t in toks if t.sigil or t.value.lower() != "default"]

    g = group(toks, tilde_is_a_name=True)
    _reject_unsupported(g, {"~"}, "account")
    plain = list(g.plain)
    if not plain:
        raise ParseError("need an id — e.g. fhsa ~First Home Savings")
    if len(plain) > 1:
        raise ParseError(
            f"one id, then sigils. Did not know what to do with "
            f"{' '.join(plain[1:])!r}: the name needs ~")

    account_id = plain[0].lower()
    if not _SLUG.match(account_id):
        raise ParseError(
            f"{account_id!r} is not an account id — lowercase words joined by single "
            f"hyphens, e.g. tfsa or non-reg. You will type it after / on every fill.")
    if account_id in known_accounts:
        raise ParseError(f"{account_id} already exists")

    return AccountInput(account_id=account_id, name=_one(g, "~", "name"),
                        make_default=make_default)


# parse_as_of() WAS HERE and had NO CALLERS. It read the date the book judged its own
# rules as of, behind `k` on the dashboard -- and `k` went in v11 with the rules it
# selected between. Dead code that raises when revived is worse than none, which is the
# same reason book.accounts() was repaired rather than left naming two dropped columns.
#
# book.set_clock()/clock_as_of() went with it; the `clock` table stays, unread, holding
# the date the book was created.


# --------------------------------------------------------------------------- #
# and a bet's frozen allocation
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AllocationInput:
    amount: float
    on_date: str
    why: str | None


def parse_allocation(raw: str, *, now: dt.datetime) -> AllocationInput:
    """`5000 @2026-09-08 ~why it moved`

    `~why` is optional HERE and required by book.set_allocation when the figure already
    exists. The schema calls re-baselining "a deliberate, recorded act" and enforces
    `allocation_rebased_from IS NULL OR allocation_rebase_why IS NOT NULL`, so the reason
    is demanded at the point where it is actually needed rather than always.
    """
    toks = tokenise(raw)
    if not toks:
        raise ParseError("nothing to record")
    g = group(toks)
    _reject_unsupported(g, {"~", "@"}, "allocation")
    if not g.plain:
        raise ParseError("need an amount — e.g. 5000 @today")
    if len(g.plain) > 1:
        raise ParseError(
            f"one amount, then sigils. Did not know what to do with "
            f"{' '.join(g.plain[1:])!r}: the reason needs ~")
    amount = _number(g.plain[0], "allocation")
    if amount <= 0:
        raise ParseError("an allocation is positive")
    on_date = resolve_when(g.by_sigil.get("@", []), now=now)
    return AllocationInput(amount=amount, on_date=on_date, why=_one(g, "~", "reason"))


BET_STATUSES = ("forming", "live", "weakened", "falsified", "closed", "retired",
                "abandoned")


@dataclass(frozen=True)
class BetStatusInput:
    status: str
    on_date: str
    note: str | None


def parse_bet_status(raw: str, *, now: dt.datetime) -> BetStatusInput:
    """`closed @2026-09-08 ~all three legs exited on a portfolio call`

    One bare word, the status. `@date` is the day the move happened and defaults to today
    -- it becomes `opened` when a bet goes live and `closed` when it ends, which is why it
    is a field and not the wall-clock: recording yesterday's exit today should date it
    yesterday.
    """
    toks = tokenise(raw)
    if not toks:
        raise ParseError("nothing to record")
    g = group(toks)
    _reject_unsupported(g, {"~", "@"}, "bet status")
    if not g.plain:
        raise ParseError(f"need a status — {', '.join(BET_STATUSES)}")
    if len(g.plain) > 1:
        raise ParseError(
            f"one status, then sigils. Did not know what to do with "
            f"{' '.join(g.plain[1:])!r}: the reason needs ~")
    status = g.plain[0].lower()
    if status not in BET_STATUSES:
        raise ParseError(f"{status!r} is not a status — {', '.join(BET_STATUSES)}")
    on_date = resolve_when(g.by_sigil.get("@", []), now=now)
    return BetStatusInput(status=status, on_date=on_date, note=_one(g, "~", "reason"))


def parse_bet_name(raw: str) -> str:
    """Free text, for renaming a bet. `Big Techs and Semis`.

    NO SLUG RULE, and no slug CHANGE either: renaming edits the name only. The id was
    derived once at creation and is a foreign key in nine tables, so re-deriving it from a
    new name would turn a change of wording into a nine-table rewrite. Two things that were
    one field are now two, and this is the payoff.
    """
    toks = tokenise(raw)
    if not toks:
        raise ParseError("need a name")
    g = group(toks)
    _reject_unsupported(g, set(), "name")
    name = " ".join(g.plain).strip()
    if not name:
        raise ParseError("need a name")
    return name


def parse_trade_bet(raw: str, *, known: dict[str, str] | None = None) -> str | None:
    """`Big Techs and Semis`, `!semis-and-big-techs`, or EMPTY to detach. Returns the id.

    EMPTY IS A REAL ANSWER, which is why this does not refuse a blank line the way every
    other parser does. "no bet" is a destination -- the author parks principal with no thesis
    attached -- so detaching has to be expressible, and the only honest way to say nothing
    is to type nothing.

    RESOLVES A NAME, not just a slug. The author, 2026-09-24: "moving trades between bets can be
    its own key where i can tab through the target bet." Once `tab` cycles the targets they never
    types either form, so the prompt should show the words a person reads -- which
    means accepting text with spaces in it and mapping it back to the key.

    `known` maps a lowercased name AND id to the id. Without it, or on a miss, a single bare
    token is returned as-is so an id typed by hand still works; a miss on several words is an
    error, because "Big Techs" is not an id and guessing which bet was meant is exactly what
    this grammar exists not to do.
    """
    toks = tokenise(raw)
    if not toks:
        return None
    g = group(toks)
    _reject_unsupported(g, {"!"}, "bet")
    named = g.by_sigil.get("!", []) + g.plain
    if not named:
        return None
    text = " ".join(named).strip()
    if known:
        hit = known.get(text.lower())
        if hit is not None:
            return hit
    if len(named) > 1:
        raise ParseError(f"no bet called {text!r} — tab through the ones there are")
    return named[0]


# parse_review_date WAS HERE. The date it produced was written by `r` and read by
# NOTHING -- 0 of 6 bets carried one, and the only thing that ever read the column was
# a check.py warning quoting a rule this tool no longer enforces. Dropped 2026-09-27.


# --------------------------------------------------------------------------- #
# a stop on a leg
# --------------------------------------------------------------------------- #
BASES = ("gross-deployed", "current-cost")


@dataclass(frozen=True)
class StopInput:
    pct: float
    basis: str
    price: float | None


def parse_stop(raw: str) -> StopInput:
    """`25` or `25% =58.40 current-cost`

    The PERCENTAGE is what the curve checks, so it is the one required word and the `%`
    is optional noise -- nobody typing a stop means 25 dollars. An optional `=price` is
    the level at the broker, recorded alongside because the percentage is the rule and
    the price is the order.

    The basis defaults to gross-deployed: it is what the curve's own denominator means
    (cost at entry, not marked-to-market), so defaulting to the other one would silently
    change what the number is measured against.
    """
    toks = tokenise(raw)
    if not toks:
        raise ParseError("nothing to record")
    g = group(toks)
    _reject_unsupported(g, {"="}, "stop")
    if not g.plain:
        raise ParseError("need a percentage — e.g. 25, or 25% =58.40")

    pct = _number(g.plain[0].rstrip("%"), "stop percentage")
    if not 0 < pct <= 100:
        raise ParseError(f"a stop is between 0 and 100 percent, not {pct:g}")

    basis = "gross-deployed"
    if len(g.plain) > 1:
        basis = g.plain[1].lower()
        if basis not in BASES:
            raise ParseError(f"{basis!r} is not a basis — {', '.join(BASES)}")
    if len(g.plain) > 2:
        raise ParseError(
            f"a percentage and at most a basis. Did not know what to do with "
            f"{' '.join(g.plain[2:])!r}: the broker's price needs =")

    raw_price = _one(g, "=", "stop price")
    price = _number(raw_price, "stop price") if raw_price is not None else None
    if price is not None and price <= 0:
        raise ParseError("a stop price is positive")
    return StopInput(pct=pct, basis=basis, price=price)


# --------------------------------------------------------------------------- #
# an instrument
# --------------------------------------------------------------------------- #
# KINDS WAS HERE, the six instrument kinds parse_instrument validated against. The schema
# still has the CHECK -- it is the wall -- but nothing in Python needs the list any more:
# book.register_from_quote maps the price source's own instrumentType, and CURRENCIES is
# imported from config so a closed vocabulary lives in one place.


# parse_instrument() AND InstrumentInput WERE HERE, behind `i`, and both went with the key.
# The author: "The user should never worry about ticker registration. it's completely meaningless
# and garbage."
#
# Everything that grammar asked for is now read from the quote instead -- currency, type,
# name and the quote symbol, which was the field it existed to let them type. What is left
# for a person to answer is which LISTING, when a ticker names two securities, and that is
# parse_listing below: one word, chosen by tabbing rather than typed.


def parse_listing(raw: str) -> str:
    """One symbol, chosen from the candidates the desk offered. `COIN` or `COIN.TO`.

    THE ONLY THING LEFT FOR A PERSON TO SAY about an instrument. Every other field comes from
    the quote; this one cannot, because the two candidates ARE different securities and only
    the holder knows which they own. `tab` cycles them, so it is a choice rather than an entry.

    Membership is checked by the caller, which is the only place that knows what was offered.
    """
    toks = tokenise(raw)
    if not toks:
        raise ParseError("nothing chosen — tab through the listings, then enter")
    if any(t.sigil for t in toks) or len(toks) > 1:
        raise ParseError("one symbol, nothing else — tab cycles through the choices")
    return toks[0].value.upper()

# --------------------------------------------------------------------------- #
# completion
#
# Which vocabulary applies to the word under the cursor is a fact about the GRAMMAR, so
# it lives here beside the grammar rather than in the widget that draws the prompt. The
# prompt supplies the text and the caret; the app supplies the vocabularies, because only
# it has the book open.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Completion:
    """What to do about the word under the caret."""
    start: int              # where the word being completed begins, sigil included
    end: int                # where it ends (the caret)
    sigil: str              # "!", "/", "@" or "" -- carried so the caller can rebuild
    prefix: str             # what has been typed, sigil STRIPPED
    field: str              # 'ticker' | 'bet' | 'account' | 'when' | ''
    matches: tuple[str, ...]

    def replaced(self, raw: str, insert: str) -> tuple[str, int]:
        """(new line, new caret) with the word under the caret replaced by `insert`."""
        word = self.sigil + insert
        return raw[:self.start] + word + raw[self.end:], self.start + len(word)

    @property
    def common(self) -> str:
        """The longest prefix every match shares -- what it is safe to insert."""
        if not self.matches:
            return self.prefix
        first = self.matches[0]
        for i, ch in enumerate(first):
            if any(len(m) <= i or m[i] != ch for m in self.matches[1:]):
                return first[:i]
        return first


def _word_at(raw: str, caret: int) -> tuple[int, str]:
    """(start, text) of the whitespace-delimited word ending at the caret."""
    start = caret
    while start > 0 and not raw[start - 1].isspace():
        start -= 1
    return start, raw[start:caret]


def complete(raw: str, caret: int, label: str, *, tickers=(), bets=(), accounts=(),
             listings=()) -> Completion:
    """What can the word under the caret be?

    The FIELD is decided by the sigil, and only falls back to position when there is no
    sigil -- `!` is always a bet and `/` is always an account whatever else is on the
    line, which is what makes the grammar order-free in the first place. A bare word is a
    ticker only in the first slot of a fill, because every other bare slot is a number.
    """
    start, word = _word_at(raw, caret)
    sigil = word[0] if word and word[0] in SIGILS else ""
    prefix = word[1:] if sigil else word

    before = tokenise(raw[:start])
    field, pool = "", ()

    # A `~` ANYWHERE EARLIER MEANS PROSE, and that outranks the sigil. _fold_tilde
    # absorbs every later token into the note, so a `/tf` typed inside a note is the
    # characters "/tf" and not an account -- completing it would rewrite the note.
    if any(t.sigil == "~" for t in before):
        return Completion(start=start, end=caret, sigil=sigil, prefix=prefix,
                          field="", matches=())

    if sigil == "!":
        field, pool = "bet", bets
    elif sigil == "/":
        field, pool = "account", accounts
    elif sigil == "@":
        field, pool = "when", ("today", "yesterday")
    elif sigil:
        # `~` opens prose and `=` is a number. Nothing to offer, and offering the wrong
        # vocabulary is worse than offering none.
        field, pool = "", ()
    else:
        # A bare word is a ticker in the FIRST BARE SLOT, counted over TOKENS rather than
        # characters. The grammar is order-free, so `/tfsa USDCO 10 65` is a valid fill --
        # and counting characters made the ticker uncompletable there, which a test caught.
        bare = sum(1 for t in before if not t.sigil)
        if label == "listing":
            # THE WHOLE POINT OF THIS PROMPT. The author: "raise a prompt or something and let me
            # tab
            # through the ticker name." The pool is what the lookup found, so tab cycles COIN
            # and COIN.TO and nothing else.
            field, pool = "listing", listings
        elif label == "fill" and bare == 0:
            field, pool = "ticker", tickers
        # THE CLOSED VOCABULARIES, which is why their hints no longer spell them out. The
        # `bet status` grammar was 155 characters because it listed all seven values, and
        # `instrument` was 157 because it listed six kinds -- both far past the width of the
        # subtitle they render in, so both were clipped exactly where the options were.
        #
        # Offering them here is what makes leaving them out honest. A hint that says
        # "tab lists them" and then does not is worse than the long version.
        elif label == "bet status" and bare == 0:
            field, pool = "status", BET_STATUSES
        elif label in ("capital in", "offset") and bare >= 1:
            # The amount comes first and is a number; a later bare word can only be the
            # currency, which is the one thing `_currency` looks for.
            field, pool = "currency", CURRENCIES

    low = prefix.lower()
    matches = tuple(sorted(c for c in pool if c.lower().startswith(low)))
    return Completion(start=start, end=caret, sigil=sigil, prefix=prefix, field=field,
                      matches=matches)


# --------------------------------------------------------------------------- #
# a balance reading from a statement
# --------------------------------------------------------------------------- #
class BalanceInput:
    total: float
    risk_free: float | None
    on_date: str
    note: str | None
    account: str | None = None


# parse_balance() AND BalanceInput WERE HERE, behind `b` on the dashboard. The desk
# does not take a statement total any more -- see the note in book.py where
# balance() was. Removing it also took `=cash sleeve` out of the grammar, which was
# one of the five different things `=` meant.


# --------------------------------------------------------------------------- #
# a bet
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BetInput:
    bet_id: str          # the derived slug, which `!bet` carries
    name: str            # what they typed, verbatim
    note: str | None
    allocation: float | None
    on_date: str


def slugify(text: str) -> str:
    """`Big Techs and Semis` -> `big-techs-and-semis`. The key, derived once.

    Only ever called when a bet is CREATED. Renaming edits the name and leaves the slug
    alone, so the id stays a stable foreign key and nine tables are never rewritten for a
    change of wording.
    """
    out, prev_dash = [], True
    for ch in text.strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-")


def parse_bet(raw: str, *, now: dt.datetime,
              known_bets: frozenset[str] = frozenset()) -> BetInput:
    """`Big Techs and Semis =5000 ~one line if you want one`

    THE NAME IS THE UNSIGILED TEXT, all of it, and that is daylogs' rule rather than a new
    one: "whatever is not sigiled remains as plain text, in order." the author, 2026-09-24: "i dont
    want to write bets big-techs-and-semis. i just want to write Big Techs and Semis."

    The slug is DERIVED from the name, not typed. It exists because `!bet` has to survive
    tokenising as one whitespace-free word, which is a constraint of the grammar and no
    business of the person naming a thing. Before this, one field did both jobs and prose
    lost.

    `~note` is optional. It used to be a required title, because the desk created the thesis
    file and needed an H1 for it; the desk does not create files any more, and now the NAME
    is the title.
    """
    toks = tokenise(raw)
    if not toks:
        raise ParseError("nothing to record")
    g = group(toks)
    _reject_unsupported(g, {"~", "=", "@"}, "bet")
    if not g.plain:
        raise ParseError("need a name — e.g. Big Techs and Semis =5000")
    name = " ".join(g.plain)
    bet_id = slugify(name)
    if not bet_id:
        raise ParseError(
            f"{name!r} has no letters or digits to make an id from — give it a word")
    if bet_id in known_bets:
        raise ParseError(f"!{bet_id} already exists — open it rather than recreating it")

    note = _one(g, "~", "note")

    alloc_raw = _one(g, "=", "allocation")
    alloc = _number(alloc_raw, "allocation") if alloc_raw is not None else None
    if alloc is not None and alloc <= 0:
        raise ParseError("an allocation is positive")

    on_date = resolve_when(g.by_sigil.get("@", []), now=now)
    return BetInput(bet_id, name, note, alloc, on_date)
