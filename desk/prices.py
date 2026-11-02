#!/usr/bin/env python3
"""Pull closing prices and FX into the book.

    python -m desk.prices            # fetch and store the latest close for what we hold
    python -m desk.prices --dry      # fetch and print, write nothing
    python -m desk.prices --days 30  # backfill a window

WHERE THIS CAME FROM. The request shape is copied from
a separate data-ingestion project of the author's, deliberately copied rather than imported:
watchboard is not a package (no pyproject, and its modules need the repo root on
sys.path), and its fetch() is welded to a CFTC-derived symbol map that contains no
single-name equities by policy. Its cache holds none of our tickers either. So the
seam worth taking is the one function that has no repo-internal imports.

THREE LINES IN HERE ARE THE WHOLE POINT, each encoding a failure someone already hit:

1. The dataGranularity assert. Asking Yahoo for range=max makes it IGNORE `interval`
   and return MONTHLY bars while still answering 200 OK. Measured in watchboard:
   range=max&interval=1wk on ^VIX gave 440 points at granularity "1mo" against 1,915
   true weekly points. Storing those would put coarser data in a column labelled
   daily. Hence explicit period1/period2 and the check.
2. The browser User-Agent. The endpoint refuses some non-browser agents outright.
3. `if close is None: continue`. A holiday or a hole is not a zero price, and
   zero-filling a close would be a fabricated number in the one table everything
   else divides by.

PROVENANCE, stated because it matters: query1.finance.yahoo.com/v8/finance/chart is
NOT a documented public API. It can change or refuse without notice. watchboard uses
it because the keyless alternatives do not work from here -- FRED's fredgraph.csv
times out, Stooq returns a consent page instead of CSV. No key, no secret.

SYMBOLS ARE NOT TICKERS. `instruments.quote_symbol` holds the lookup symbol, because
they differ and the difference is silent: ZNQ does not resolve at all, ZNQ.TO does.
And FX direction is a trap -- CAD=X and USDCAD=X are CAD PER USD (~1.38) while
CADUSD=X is the inverse (~0.72). The book stores base=USD quote=CAD, i.e. CAD per
USD, so USDCAD=X is the correct one. Verified live before this was written.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import pathlib
import sqlite3
import sys
import time
import urllib.request

# NO sys.path HACK. This file lived in book/ and had to reach up a directory to import
# desk.config, so it carried its own two-line sys.path.insert. It is a module inside the
# package now -- moved there 2026-09-30 so that `pip install trading-desk` ships the schema
# and this script, without which an installed user could not create a book at all.
#
# Run it as `python -m desk.prices` (no console script: marks refresh on their own timer), never as
# a path: a
# relative import fails when a package module is executed as a file.
from . import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The book is DATA and is not in this repo. See desk/config.py.
DB = os.fspath(config.book_path())

ENDPOINT = "https://query1.finance.yahoo.com/v8/finance/chart/"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) trading-desk"}
WANT_GRANULARITY = "1d"
# 30s, NOT 90. The desk polls this every 60 seconds now, so a request allowed to hang
# for 90 outlives the interval that started it and two fetches overlap. The numbers are
# tokscale's, which has run this endpoint shape in anger: 30s to read, 3 attempts,
# 200ms doubling.
TIMEOUT = 30
MAX_RETRIES = 3
BACKOFF_S = 0.2
PAUSE_S = 0.4


def _require_book(path: str) -> None:
    """Refuse to touch a path that is not already a book.

    sqlite3.connect() CREATES the file, and an empty database left at a typo'd path is
    invisible (gitignored) while making `migrate.py --new` refuse with "already
    exists" -- which is false. Zero bytes is treated as absent for the same reason: a
    0-byte file is byte-for-byte indistinguishable from no file.
    """
    p = pathlib.Path(path)
    if not p.exists() or p.stat().st_size == 0:
        raise SystemExit(
            f"no book at {path}\n"
            f"  create one:   desk-migrate --new\n"
            f"  or point at one:   export TRADING_DESK_BOOK=/path/to/hub.db")


def fetch_one(symbol: str, since_epoch: int) -> list[dict]:
    """Daily bars for one Yahoo symbol. stdlib only, no key.

    Copied from watchboard/sources/prices.py. Keep the granularity assert.
    """
    url = (f"{ENDPOINT}{urllib.parse.quote(symbol)}"
           f"?period1={since_epoch}&period2={int(time.time())}"
           f"&interval={WANT_GRANULARITY}&events=div%2Csplit")
    req = urllib.request.Request(url, headers=UA)
    # RETRIED, because this is now on a 60s timer rather than a keystroke. An
    # undocumented endpoint refuses or times out occasionally, and one such refusal used
    # to mean the mark simply did not update -- the dashboard then showed a stale number
    # with nothing saying so. Three tries, 200ms doubling: tokscale's numbers.
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                payload = json.load(resp)
            break
        except Exception:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(BACKOFF_S * (2 ** attempt))

    err = (payload.get("chart") or {}).get("error")
    if err:
        raise RuntimeError(f"{symbol}: {err}")
    results = (payload.get("chart") or {}).get("result") or []
    if not results:
        raise RuntimeError(f"{symbol}: no result in payload")
    res = results[0]
    meta = res.get("meta") or {}

    got = meta.get("dataGranularity")
    if got != WANT_GRANULARITY:
        # range=max silently returns coarser bars while answering 200 OK.
        raise RuntimeError(
            f"{symbol}: asked for {WANT_GRANULARITY} bars and got {got!r}. Storing "
            "these would put coarser data in a column labelled daily.")

    currency = meta.get("currency")
    stamps = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []

    # A BAR'S DATE IS ITS EXCHANGE'S DATE, NOT UTC'S.
    #
    # Yahoo stamps a daily bar at the session's start in exchange-local time. For a
    # US equity that is 13:30 UTC, so formatting in UTC happens to give the right
    # day. For USDCAD=X the exchange is Europe/London and the stamp is 23:00 UTC of
    # the PREVIOUS calendar day -- so UTC formatting shifted every FX date back one.
    #
    # That put an FX row on Sunday 2026-08-30, left no FX row at all for Friday
    # 2026-09-04, and made the P&L attribution value an 18,500 USD sleeve at a rate
    # from a different day than the balance it was being compared against. At 1.86
    # CAD per pip that was worth more than the residual it was supposed to measure.
    #
    # gmtoffset is in seconds and is what Yahoo itself uses to lay out the session.
    offset = meta.get("gmtoffset")
    if offset is None:
        raise RuntimeError(
            f"{symbol}: no gmtoffset in meta, so a bar's exchange date cannot be "
            "derived. Refusing to guess -- guessing in UTC is the bug this replaced.")

    out = []
    # strict=True: `stamps` and `closes` come from two separate arrays in one JSON payload
    # and are only parallel because the API says so. If they ever disagree, zip() would
    # silently truncate to the shorter -- dropping the most recent closes, which is the
    # half that matters, and reporting success. A raise names the problem instead.
    for ts, close in zip(stamps, closes, strict=True):
        if close is None:
            continue          # a holiday or a hole; a null close is not a zero price
        # NOT FINITE, NOT A PRICE. json.load accepts the literals Infinity and NaN by
        # default, and `CHECK (amount > 0)` passes for Infinity -- so one odd payload could
        # put a non-finite REAL in `prices`, and from there into every derived figure and
        # into the MCP server's JSON, where `Infinity` is not even valid JSON. desk/parse.py
        # already guards the hand-typed path for exactly this reason.
        if not math.isfinite(close):
            continue
        out.append({
            "date": dt.datetime.fromtimestamp(
                ts + offset, dt.UTC).strftime("%Y-%m-%d"),
            "symbol": symbol,
            "close": float(close),
            "currency": currency,
        })
    return out


# WHAT YAHOO CALLS A THING -> WHAT THE SCHEMA CALLS IT. Measured against the live endpoint
# rather than guessed:
#
#   ZGLD.TO  instrumentType='ETF'     SGOV  'ETF'     SOXL  'ETF'     TQQQ  'ETF'
#
# SOXL IS A 3x FUND AND YAHOO SAYS ONLY 'ETF', so leverage cannot be inferred from this and
# nothing here ever returns 'leveraged-etf' -- the schema requires a leverage_factor with it
# and there is nothing to supply. A leveraged fund still needs `i` and its factor typed.
# Same for 'money-market': SGOV and CASH.TO are money-market funds by intent and Yahoo calls
# them ETFs, which is a judgement about how they are USED and not a fact about the security.
_KIND_OF = {"EQUITY": "common", "ETF": "etf", "MUTUALFUND": "etf",
            "FUTURE": "future", "OPTION": "option"}

# TRIED IN ORDER. A bare TSX ticker 404s and its `.TO` form resolves -- the author typed `ZGLD`,
# which is what the book calls it, and Yahoo only knows `ZGLD.TO`. That mismatch is the whole
# reason `instruments.quote_symbol` exists, and trying the suffix here fills it in by itself.
_SUFFIXES = ("", ".TO", ".V")


def identify(ticker: str, *, timeout: int = 10) -> list[dict]:
    """EVERY symbol Yahoo knows by this name, across the suffixes. Never picks one.

    THIS IS WHAT MAKES THE REGISTRY WALL A CHECK RATHER THAN A CHORE. The wall exists so a
    typo cannot become a second instrument with its own cost base -- but it was enforced by
    asking whether the book had SEEN the ticker before, which refuses a genuinely new holding
    exactly as firmly as a typo. The author: "why not automatically check it up and add it to the
    registry as i enter? i mean seems weird that we dont do this."

    Asking the price source distinguishes the two: `NOSUCHTICKERXYZ` 404s, and `ZGLD.TO` comes
    back with currency, type and name -- three fields nobody should have to type.

    IT RETURNS A LIST, AND THAT IS THE WHOLE POINT. The first version tried the suffixes in
    order and took the first hit, which measurement showed to be dangerous:

        CBIL      'Corgi 3-12 Month T-Bill ETF'      USD, Cboe US
        CBIL.TO   'Global X 0-3 Month T-Bill ETF'    CAD, Toronto

    The author holds the Canadian one. Taking the bare form first would have silently registered
    their
    holding against an American fund and then priced it from that fund forever -- a wrong
    number that looks entirely ordinary, which is the failure this codebase is built to
    refuse. Two candidates is an AMBIGUITY, and the caller must say so rather than guess.

    `VFV` bare is a third case: it resolves as an `ECNQUOTE` with no currency at all -- a
    phantom Nasdaq quote, not a listing. The currency filter drops it, and because that is a
    `continue` and not a `return`, `VFV.TO` is still found. The first version returned there
    and reported the real ETF as unknown.

    A SHORTER TIMEOUT THAN A PRICE FETCH, deliberately: this sits in the path of a fill being
    submitted, and thirty seconds of frozen prompt is worse than being told to use `i`.
    """
    found: list[dict] = []
    for suffix in _SUFFIXES:
        symbol = f"{ticker}{suffix}"
        url = (f"{ENDPOINT}{urllib.parse.quote(symbol)}"
               f"?period1={int(time.time()) - 86400 * 7}&period2={int(time.time())}"
               f"&interval={WANT_GRANULARITY}")
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=UA), timeout=timeout) as resp:
                payload = json.load(resp)
        except Exception:
            # A 404 is an ANSWER -- this suffix is not a listing -- so try the next. A timeout
            # or a DNS failure is not an answer but is indistinguishable here, which is why
            # the caller reads an empty list as "could not verify" and never as "does not
            # exist": it offers `i` rather than calling the ticker a typo.
            continue
        chart = payload.get("chart") or {}
        if chart.get("error"):
            continue
        meta = ((chart.get("result") or [{}])[0].get("meta") or {})
        currency = (meta.get("currency") or "").upper()
        if currency not in ("CAD", "USD"):
            # A third currency has no rate anybody fetches, so registering it would book every
            # figure at par. Also catches the ECNQUOTE phantoms, which carry no currency.
            continue
        found.append({
            "ticker": ticker.upper(),
            "quote_symbol": symbol,
            "currency": currency,
            "kind": _KIND_OF.get((meta.get("instrumentType") or "").upper(), "common"),
            "name": meta.get("longName") or meta.get("shortName") or None,
            "exchange": meta.get("fullExchangeName") or meta.get("exchangeName") or None,
        })
    return found


def wanted(con) -> list[tuple[str, str, str]]:
    """(ticker, quote_symbol, kind) for everything the book needs priced.

    Marks for instruments we actually hold, plus the FX pairs any holding needs to
    reach the account currency. Nothing speculative -- if it is not held, it is not
    fetched.
    """
    rows = con.execute("""
        SELECT DISTINCT i.ticker, COALESCE(i.quote_symbol, i.ticker), 'mark'
        FROM trades t JOIN instruments i ON i.ticker = t.ticker
        WHERE t.status = 'open'
    """).fetchall()
    # EVERY CURRENCY THE BOOK HOLDS CASH IN, not just the ones with an open position.
    # Cash is tracked per currency since v11, so a USD sleeve sitting entirely in cash
    # still has to be translated -- and with the old query it was not fetched at all,
    # which blanked the whole net the moment the last USD leg was closed.
    currencies = {r[0] for r in con.execute("""
        SELECT DISTINCT currency FROM trades WHERE status = 'open'
        UNION SELECT DISTINCT currency FROM flows
        UNION SELECT DISTINCT currency FROM adjustments
        UNION SELECT DISTINCT to_ccy FROM fx_conversions
        UNION SELECT DISTINCT from_ccy FROM fx_conversions
    """)}
    # FROM THE PROFILE. This was `SELECT allocation_ccy FROM bets ... LIMIT 1` with no
    # ORDER BY, so which pair got fetched depended on SQLite's row order -- the same
    # defect book.base_currency() had. The author asked for a profile: "inside the profile, you
    # need my name, and my home currency."
    base = config.profile().home_currency
    for c in sorted(currencies):
        if c != base:
            # USDCAD=X is CAD per USD, matching the book's base=USD quote=CAD row.
            rows.append((c, f"{c}{base}=X", "fx"))
    return rows


def main(dry: bool, days: int, emit=None) -> int:
    """Fetch and store the latest close for everything held. Returns 1 if any symbol failed.

    `emit` TAKES THE OUTPUT INSTEAD OF print(), and it exists because of a real leak. The desk
    runs this on a worker thread and used to capture its output with
    contextlib.redirect_stdout -- which replaces PROCESS-GLOBAL sys.stdout, not a thread's
    own. Two overlapping fetches therefore interleaved their save/restore, the first one to
    finish handed the real terminal back while the second was still printing, and six lines of
    fetch output appeared on the shell the moment the TUI stopped overdrawing them.

    A lock would not have been the fix. A thread should not be patching global stdout at all,
    so the output is handed to the caller: the desk passes `lines.append`, the CLI passes
    nothing and keeps printing.
    """
    say = emit if emit is not None else print
    _require_book(DB)
    # AUTOCOMMIT, AND THIS IS A BUG FIX, not a style choice. The author hit `OperationalError:
    # database is locked` recording a fill on 2026-09-25, and this function was why.
    #
    # Without isolation_level=None, Python opens an implicit transaction on the first write
    # and holds it until commit(). The first write was the `UPDATE session` below and the
    # commit was AFTER THE WHOLE LOOP -- so a write lock was held across every HTTP fetch,
    # every retry and every pause, for the entire run. Fourteen instruments at up to three
    # 30-second retries each is minutes of lock, and the desk runs this every 60 seconds on
    # a timer. Any fill submitted inside that window failed after the 5-second default
    # timeout, which is exactly what they saw.
    #
    # Autocommit means the session UPDATE lands immediately and each batch below takes the
    # lock for milliseconds. A background poll must never make the tool unwritable.
    con = sqlite3.connect(DB, isolation_level=None)
    con.execute("PRAGMA foreign_keys = ON")
    # AND WAIT RATHER THAN FAIL if someone else is mid-write. Ten seconds is far longer than
    # any write here now takes, so a timeout would mean something is genuinely stuck.
    con.execute("PRAGMA busy_timeout = 10000")
    con.execute("UPDATE session SET actor = 'prices.py'")

    cols = {r[1] for r in con.execute("PRAGMA table_info(instruments)")}
    if "quote_symbol" not in cols:
        say("FAIL: instruments.quote_symbol does not exist. Add it first — the "
              "lookup symbol is not the ticker (ZNQ does not resolve; ZNQ.TO does).")
        return 1

    since = int((dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).timestamp())
    targets = wanted(con)
    if not targets:
        say("nothing held, nothing to price")
        return 0

    stored = skipped = 0
    for i, (base, symbol, kind) in enumerate(targets):
        if i:
            time.sleep(PAUSE_S)
        try:
            bars = fetch_one(symbol, since)
        except Exception as exc:
            say(f"  ! {symbol:<12} {type(exc).__name__}: {exc}")
            skipped += 1
            continue
        if not bars:
            say(f"  ! {symbol:<12} no bars returned")
            skipped += 1
            continue
        last = bars[-1]
        # THE HOME CURRENCY, not the literal "CAD" this used to write. A USD-home book
        # fetching CADUSD=X would have stored the row as base=CAD quote=CAD -- a rate
        # from a currency to itself, which fx_rate() would then have applied.
        quote = config.profile().home_currency if kind == "fx" else last["currency"]
        say(f"  {base:<6} {symbol:<12} {last['date']}  {last['close']:>10.4f} "
              f"{quote}   ({len(bars)} bars)")
        if not dry:
            # ONE SHORT TRANSACTION PER INSTRUMENT, opened AFTER its fetch has returned and
            # closed before the next one starts, so the lock is never held across the
            # network. Batched rather than row-by-row because autocommit would otherwise
            # make each of ~1,500 rows its own fsync.
            con.execute("BEGIN IMMEDIATE")
            con.executemany(
                "INSERT OR REPLACE INTO prices"
                "(on_date,base,quote,amount,kind,source,notes) "
                "VALUES (?,?,?,?,?,?,?)",
                [(b["date"], base, quote, b["close"], kind, f"yahoo:{symbol}", None)
                 for b in bars])
            con.execute("COMMIT")
            stored += len(bars)
    say(f"\n{len(targets) - skipped}/{len(targets)} symbols"
          + (f", {stored} rows written" if not dry else ", DRY RUN — nothing written"))
    con.close()
    return 1 if skipped else 0


if __name__ == "__main__":
    d = 14
    if "--days" in sys.argv:
        d = int(sys.argv[sys.argv.index("--days") + 1])
    sys.exit(main(dry="--dry" in sys.argv, days=d))
