"""Yahoo bars for live runs, read through the optional yfinance package.

`fetch` is the one network call, and the tests replace it (tests/conftest.py). `ensure` fetches
what bars.db lacks and stores only complete bars, with the span each gap covered. yfinance is
imported inside the call alone, so the desk runs without it until a live trade is read.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from desk import bars, judge

SOURCE = "yahoo"   # bars.db's source for every bar this module stores

# Minutes -> Yahoo's interval name. yfinance 1.8 has no 4h (ruling R10).
INTERVALS: dict[int, str] = {1: "1m", 2: "2m", 5: "5m", 15: "15m", 30: "30m", 60: "60m",
                             90: "90m", 1440: "1d", 10080: "1wk"}
# One day inside Yahoo's 30/60/730; 2m held only ~42 days in the probe (external.md §1.4);
# absent: no limit.
RETENTION_DAYS: dict[int, int] = {1: 29, 2: 40, 5: 59, 15: 59, 30: 59, 90: 59, 60: 729}
# The design's 7 days (Yahoo allows 8); a timeframe absent here is one request.
CHUNK: dict[int, timedelta] = {1: timedelta(days=7)}

_INSTALL = ("live runs read Yahoo through the optional yfinance package; install it with "
            "pip install 'trading-desk[yahoo]'")


@dataclass(frozen=True)
class Fetched:
    rows: tuple[judge.Bar, ...]
    market_time: datetime | None   # Yahoo's latest trade (regularMarketTime), delay included


def ticker_symbol(ticker: str, instrument: str, currency: str) -> str:
    """The desk's ticker as Yahoo spells it: GC -> GC=F, TSX:WXYZ.B -> WXYZ-B.TO."""
    symbol = ticker.rpartition(":")[2]   # Yahoo names no exchange prefix: COMEX:GC -> GC=F
    if instrument == "future":
        return f"{symbol}=F"
    if currency == "CAD":
        return symbol if symbol.endswith(".TO") else symbol.replace(".", "-") + ".TO"
    return symbol.replace(".", "-")


def _yfinance(symbol: str, interval: str, start: datetime, end: datetime) -> Fetched:
    try:
        import yfinance
    except ImportError as e:
        raise RuntimeError(_INSTALL) from e
    # A bad symbol raises, not an empty frame; put back after, as the author's own code uses it.
    debug = yfinance.config.debug
    hidden, debug.hide_exceptions = debug.hide_exceptions, False
    try:
        ticker = yfinance.Ticker(symbol)
        try:
            frame = ticker.history(interval=interval, start=start, end=end, auto_adjust=False,
                                   actions=False, rounding=True, prepost=False)
        except yfinance.exceptions.YFPricesMissingError:
            return Fetched((), None)
        market = ticker.get_history_metadata().get("regularMarketTime")
    finally:
        debug.hide_exceptions = hidden
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
    frame.index = frame.index.tz_convert("UTC")
    # float() throughout: sqlite3 cannot bind numpy's int64 volume.
    rows = tuple(judge.Bar(t.to_pydatetime(), float(r.Open), float(r.High),
                           float(r.Low), float(r.Close),
                           None if math.isnan(r.Volume) else float(r.Volume))
                 for t, r in frame.iterrows())
    # yfinance 1.8 gives a pandas.Timestamp when the meta names the exchange's zone, else seconds.
    if isinstance(market, datetime):
        market = market.timestamp()
    return Fetched(rows, None if market is None else datetime.fromtimestamp(market, UTC))


fetch = _yfinance   # the only network call; every test replaces it


def _gaps(spans: list[tuple[datetime, datetime]], start: datetime,
          end: datetime) -> list[tuple[datetime, datetime]]:
    """The parts of [start, end) outside the spans (oldest first, merged)."""
    gaps = []
    for a, b in spans:
        if a > start:
            gaps.append((start, min(a, end)))
        start = max(start, b)
    gaps.append((start, end))
    return [(a, b) for a, b in gaps if a < b]


def _widened(r: judge.Bar) -> judge.Bar:
    """Yahoo's prints disagree, and a traded price lies inside its bar's range."""
    return replace(r, high=max(r.high, r.open, r.close), low=min(r.low, r.open, r.close))


def ensure(con: sqlite3.Connection, symbol: str, minutes: int, start: datetime, now: datetime,
           *, end: datetime | None = None) -> str | None:
    """Fetch what bars.db lacks of [start, end) (end None: now; never past now) and store its
    complete bars, with one span per gap filled. Returns why part of the range is still missing,
    or None. `now` alone sets the retention clip."""
    name, bar = INTERVALS[minutes], timedelta(minutes=minutes)
    until = now if end is None else min(end, now)
    covered = bars.covered(con, SOURCE, symbol, minutes)
    why = []
    if (days := RETENTION_DAYS.get(minutes)) is not None and start < (
            limit := now - timedelta(days=days)):
        if _gaps(covered, start, min(limit, until)):   # a range kept from before is not missing
            why.append(f"Yahoo keeps {name} bars for about {days} days, so bars before "
                       f"{limit:%Y-%m-%d} cannot be fetched")
        start = limit
    for a, b in _gaps(covered, start, until):
        if b - a < bar:   # no complete bar fits in it
            continue
        rows: list[judge.Bar] = []
        market: datetime | None = None
        at, failed = a, None
        while at < b:
            to = min(at + CHUNK.get(minutes, b - a), b)
            try:
                got = fetch(symbol, name, at, to)   # looked up per call, so a monkeypatch holds
            except Exception as e:   # any failure is a reason to give, never a crash
                failed = f"Yahoo could not give {symbol} {name} bars from {at} to {b}: {e}"
                break
            rows += got.rows
            if got.market_time is not None:
                market = got.market_time if market is None else max(market, got.market_time)
            at = to
        if rows:
            last = max(r.open_time for r in rows)
            # Yahoo's post-close stub (volume 0, flat) is not a traded bar, but it still proves
            # the bar before it is complete, hence `last` above. Never captured: Notes 11.
            complete = [_widened(r) for r in rows
                        if (r.open_time < last or (market is not None
                                                   and r.open_time + bar <= market))
                        and not (r.volume == 0 and r.high == r.low)]
            if complete:
                bars.add(con, SOURCE, symbol, minutes, complete, a,
                         max(r.open_time for r in complete) + bar, now)
        if failed:
            return "; ".join([*why, failed])
    return "; ".join(why) or None
