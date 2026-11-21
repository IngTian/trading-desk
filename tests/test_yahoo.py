"""Yahoo bars for live runs: only complete bars are stored, with the span each gap covered, and
the network is never reached in tests (conftest replaces `yahoo.fetch`). Every figure is
invented."""
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from desk import bars, judge, yahoo

B = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)   # bar n opens at B + n hours; 1h bars


def h(n):
    return B + timedelta(hours=n)


def bar(n, o, hi, lo, c):
    return judge.Bar(h(n), o, hi, lo, c, None)


AT = h(24)   # the write time


@pytest.fixture
def con(tmp_path):
    with closing(bars.open(tmp_path / "bars.db")) as c:
        yield c


def test_ensure_stores_complete_bars_and_the_span_they_cover(con, fake_yahoo):
    y = fake_yahoo([bar(n, 100, 101, 99, 100.5) for n in range(4)], market_time=h(3.5))
    assert yahoo.ensure(con, "ACME", 60, h(0), h(3.5)) is None
    assert y.calls == [("ACME", "60m", h(0), h(3.5))]
    assert [b.open_time for b in bars.read(con, "yahoo", "ACME", 60, h(0))] == [h(0), h(1), h(2)]
    assert bars.covered(con, "yahoo", "ACME", 60) == [(h(0), h(3))]


def test_a_forming_last_row_is_not_stored_and_a_later_read_fetches_only_the_tail(con, fake_yahoo):
    y = fake_yahoo([bar(n, 100, 101, 99, 100.5) for n in range(6)])   # market_time: each call's end
    yahoo.ensure(con, "ACME", 60, h(0), h(3.5))
    yahoo.ensure(con, "ACME", 60, h(0), h(3.75))      # under one bar uncovered: no request
    yahoo.ensure(con, "ACME", 60, h(0), h(5))
    assert y.calls == [("ACME", "60m", h(0), h(3.5)), ("ACME", "60m", h(3), h(5))]
    assert bars.covered(con, "yahoo", "ACME", 60) == [(h(0), h(5))]


def test_one_minute_bars_are_fetched_in_chunks_of_seven_days(con, fake_yahoo):
    y = fake_yahoo([])
    yahoo.ensure(con, "ACME", 1, h(0) - timedelta(days=20), h(0))
    spans = [(c[2], c[3]) for c in y.calls]
    assert len(spans) == 3 and all(b - a <= timedelta(days=7) for a, b in spans)
    assert spans[0][0] == h(0) - timedelta(days=20) and spans[-1][1] == h(0)


def test_a_start_older_than_yahoo_keeps_is_clipped_and_said(con, fake_yahoo):
    y = fake_yahoo([])
    why = yahoo.ensure(con, "ACME", 15, h(0) - timedelta(days=70), h(0))
    assert "15m" in why and y.calls[0][2] == h(0) - timedelta(days=59)


def test_a_clipped_start_already_kept_says_nothing(con, fake_yahoo):
    bars.add(con, "yahoo", "ACME", 15, [], h(0) - timedelta(days=70), h(0), AT)   # read before
    y = fake_yahoo([])
    assert yahoo.ensure(con, "ACME", 15, h(0) - timedelta(days=70), h(1)) is None
    assert y.calls == [("ACME", "15m", h(0), h(1))]


def test_a_window_kept_and_ending_before_the_clip_says_nothing(con, fake_yahoo):
    e = h(0) - timedelta(days=65)       # before the 59 days 15m bars are kept
    bars.add(con, "yahoo", "ACME", 15, [], e - timedelta(days=5), e, AT)   # read before
    y = fake_yahoo([])
    assert yahoo.ensure(con, "ACME", 15, e - timedelta(days=5), h(0), end=e) is None
    assert y.calls == []


def test_a_daily_fetch_is_not_clipped(con, fake_yahoo):
    y = fake_yahoo([])
    assert yahoo.ensure(con, "ACME", 1440, h(0) - timedelta(days=800), h(0)) is None
    assert y.calls[0][2] == h(0) - timedelta(days=800)       # F3: no retention for 1d or 1w


def test_a_gap_fetched_in_chunks_is_one_span_across_a_closed_market(con, fake_yahoo):
    fake_yahoo([judge.Bar(h(0) - timedelta(days=9), 100, 101, 99, 100.5, None),
                judge.Bar(h(0) - timedelta(minutes=2), 100, 101, 99, 100.5, None)])
    yahoo.ensure(con, "ACME", 1, h(0) - timedelta(days=9), h(0))     # two chunks
    assert bars.covered(con, "yahoo", "ACME", 1) == [(h(0) - timedelta(days=9),
                                                      h(0) - timedelta(minutes=1))]


def test_a_failing_fetch_returns_the_reason_and_stores_nothing(con):
    why = yahoo.ensure(con, "ACME", 60, h(0), h(3))      # the conftest stub: the network is off
    assert "the network is off in tests" in why
    assert bars.read(con, "yahoo", "ACME", 60, h(0)) == []
    assert bars.covered(con, "yahoo", "ACME", 60) == []


def test_a_row_whose_close_lies_outside_its_range_is_widened_to_hold_it(con, fake_yahoo):
    fake_yahoo([bar(0, 100, 101, 99, 101.5), bar(1, 101.5, 102, 101, 101.5)])
    yahoo.ensure(con, "ACME", 60, h(0), h(2))
    assert bars.read(con, "yahoo", "ACME", 60, h(0))[0].high == 101.5


@pytest.mark.parametrize(("ticker", "instrument", "currency", "symbol"), [
    ("GC", "future", "USD", "GC=F"), ("COMEX:GC", "future", "USD", "GC=F"),
    ("ACME", "stock", "USD", "ACME"),
    ("ACME.B", "stock", "USD", "ACME-B"), ("NASDAQ:ACME", "stock", "USD", "ACME"),
    ("WXYZ", "stock", "CAD", "WXYZ.TO"), ("TSX:WXYZ", "etf", "CAD", "WXYZ.TO"),
    ("WXYZ.B", "stock", "CAD", "WXYZ-B.TO"), ("WXYZ.TO", "stock", "CAD", "WXYZ.TO")])
def test_ticker_symbols(ticker, instrument, currency, symbol):
    assert yahoo.ticker_symbol(ticker, instrument, currency) == symbol


# yfinance 1.8's history() runs format_history_metadata on the meta it keeps: with the exchange's
# time zone, regularMarketTime comes back a pandas.Timestamp; without it, still Unix seconds.
@pytest.mark.parametrize("meta", [{"exchangeTimezoneName": "America/New_York"}, {}])
def test_the_yfinance_fetch_asks_for_raw_regular_session_bars_and_returns_them_in_utc(monkeypatch,
                                                                                       meta):
    import pandas as pd
    import yfinance
    asked = {}
    ny = [pd.Timestamp("2026-03-02 09:30", tz="America/New_York"),
          pd.Timestamp("2026-03-02 10:30", tz="America/New_York")]
    frame = pd.DataFrame({"Open": [100.0, float("nan")], "High": [101.0, 1.0], "Low": [99.0, 1.0],
                          "Close": [100.5, 1.0], "Adj Close": [100.5, 1.0], "Volume": [1500, 0]},
                         index=pd.DatetimeIndex(ny))

    class Ticker:
        def __init__(self, symbol):
            asked["symbol"] = symbol

        def history(self, **kw):
            asked.update(kw)
            return frame

        def get_history_metadata(self):
            return yfinance.utils.format_history_metadata({**meta, "regularMarketTime": 1772466000})

    monkeypatch.setattr(yfinance, "Ticker", Ticker)
    f = yahoo._yfinance("ACME", "60m", h(0), h(3))
    assert {k: asked[k] for k in ("auto_adjust", "actions", "rounding", "prepost")} == dict(
        auto_adjust=False, actions=False, rounding=True, prepost=False)
    assert f.rows == (judge.Bar(datetime(2026, 3, 2, 14, 30, tzinfo=UTC), 100.0, 101.0, 99.0,
                                100.5, 1500.0),)
    assert all(type(v) is float for r in f.rows for v in (r.open, r.high, r.low, r.close, r.volume))
    assert f.market_time == datetime.fromtimestamp(1772466000, UTC)


def test_prices_missing_from_yahoo_read_as_no_rows(monkeypatch):
    import yfinance

    class Ticker:
        def __init__(self, symbol):
            pass

        def history(self, **kw):
            e = yfinance.exceptions.YFPricesMissingError
            raise e.__new__(e)   # built without its arguments, which vary between releases

    monkeypatch.setattr(yfinance, "Ticker", Ticker)
    assert yahoo._yfinance("ACME", "60m", h(0), h(3)).rows == ()


def test_a_fetch_puts_yfinances_hide_exceptions_back_as_it_found_it(monkeypatch):
    import yfinance
    seen = []

    class Ticker:
        def __init__(self, symbol):
            pass

        def history(self, **kw):
            seen.append(yfinance.config.debug.hide_exceptions)
            raise OSError("Yahoo refused")

    monkeypatch.setattr(yfinance, "Ticker", Ticker)
    monkeypatch.setattr(yfinance.config.debug, "hide_exceptions", True)   # yfinance 1.8's default
    with pytest.raises(OSError, match="Yahoo refused"):
        yahoo._yfinance("ACME", "60m", h(0), h(3))
    assert (seen, yfinance.config.debug.hide_exceptions) == ([False], True)


def test_without_yfinance_a_live_read_says_how_to_install_it(con, monkeypatch):
    monkeypatch.setattr(yahoo, "fetch", yahoo._yfinance)
    monkeypatch.setitem(sys.modules, "yfinance", None)
    assert "pip install 'trading-desk[yahoo]'" in yahoo.ensure(con, "ACME", 60, h(0), h(3))


def test_no_test_reaches_the_network():
    assert yahoo.fetch is not yahoo._yfinance
    with pytest.raises(OSError, match="network is off"):
        yahoo.fetch("ACME", "60m", h(0), h(1))


M15 = datetime(2026, 3, 2, 19, 45, tzinfo=UTC)


def test_the_post_close_stub_bar_is_not_stored(con, fake_yahoo):
    stub = judge.Bar(M15 + timedelta(minutes=15), 10.5, 10.5, 10.5, 10.5, 0.0)
    fake_yahoo({"15m": [judge.Bar(M15, 10, 11, 9, 10.5, 5.0), stub]})   # market = call end
    yahoo.ensure(con, "ACME", 15, M15, M15 + timedelta(minutes=30))
    assert [b.open_time for b in bars.read(con, "yahoo", "ACME", 15, M15)] == [M15]


def test_a_zero_volume_bar_with_a_range_or_a_flat_bar_without_volume_is_kept(con, fake_yahoo):
    nxt = M15 + timedelta(minutes=15)
    rows = [judge.Bar(M15, 10, 11, 9, 10.5, 0.0), judge.Bar(nxt, 10.5, 10.5, 10.5, 10.5, None),
            judge.Bar(nxt + timedelta(minutes=15), 10, 10, 10, 10, 3.0)]
    fake_yahoo({"15m": rows})
    yahoo.ensure(con, "ACME", 15, M15, M15 + timedelta(minutes=45))
    assert len(bars.read(con, "yahoo", "ACME", 15, M15)) == 3
