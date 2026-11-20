"""Tests for entering a trade through the api. Every figure is invented."""
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from desk import api, config, model, store

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
T1 = NOW + timedelta(hours=1)
BASE = dict(side="long", entry_type="limit", entry=100.0, stop=98.5, target=103.0, size=10,
            currency="USD", reasons="Dip into support.")


def make():
    return ("---\nname: Dip buyer\n---\n\n# Dip buyer\n\nBuy dips.\n\n"
            "```rules\ntimeframes: 1h\nmin_reward_risk: 2\n```\n")


@pytest.fixture
def con(tmp_path):
    db = tmp_path / "desk.db"
    store.create(db)
    with store.open(db) as conn:
        api.save_strategy(conn, "dip-buyer", make(), now=NOW)
        api.start_run(conn, "dip-buyer", mode="live", now=NOW)
        api.start_run(conn, "dip-buyer", mode="replay", at=NOW, now=NOW)
        yield conn


def enter(con, run="dip-buyer-r1", **kw):
    args = {**BASE, "ticker": "ACME", "at": T1, "now": T1, **kw}
    return api.trade(con, run, **args)


def test_a_trade_is_one_row_and_one_entered_event(con):
    t = enter(con, reasons="  Dip into\nsupport.  ")
    assert (t.id, t.run, t.strategy, t.version) == ("acme-20260302", "dip-buyer-r1", "dip-buyer", 1)
    assert (t.ticker, t.side, t.instrument, t.point_value, t.currency) == (
        "ACME", "long", "stock", 1.0, "USD")
    d = t.decisions
    assert (d.entry.type, d.entry.price, d.entry.stop, d.entry.target, d.entry.size,
            d.entry.at) == ("limit", 100.0, 98.5, 103.0, 10, T1)
    assert d.reasons == "  Dip into\nsupport.  " and d.moves == () and not d.voided
    assert d.close_at is None and d.screenshots == ()
    kinds = [e["kind"] for e in store.events(con, trade_id=t.id)]
    assert kinds == ["entered"] and len(store.trades(con)) == 1
    assert model.decisions_of(con, t.id) == d


def test_a_live_trade_defaults_to_now(con):
    assert enter(con, at=None).decisions.entry.at == T1


def test_a_replay_trade_without_at_is_refused_and_writes_nothing(con):
    with pytest.raises(api.Refused, match="at"):
        enter(con, "dip-buyer-r2", at=None)
    assert store.trades(con) == []


def test_a_naive_at_is_refused(con):
    with pytest.raises(api.Refused, match="timezone"):
        enter(con, at=datetime(2026, 3, 2, 13, 0))
    assert store.trades(con) == []


def test_an_unknown_run_is_refused(con):
    with pytest.raises(api.Refused, match="no run"):
        enter(con, "nope-r1")


def test_at_before_the_run_started_is_refused(con):
    with pytest.raises(api.Refused, match="started"):
        enter(con, at=NOW - timedelta(minutes=1))


def test_out_of_order_replay_entries_are_accepted(con):
    later = enter(con, "dip-buyer-r2", at=NOW + timedelta(days=2))
    earlier = enter(con, "dip-buyer-r2", at=NOW + timedelta(days=1))
    assert later.decisions.entry.at > earlier.decisions.entry.at
    assert len(store.trades(con, "dip-buyer-r2")) == 2


def test_at_or_after_the_run_stop_is_refused(con):
    stop = NOW + timedelta(days=3)
    api.stop_run(con, "dip-buyer-r2", at=stop, now=NOW)
    enter(con, "dip-buyer-r2", at=stop - timedelta(minutes=1))
    for at in (stop, stop + timedelta(days=1)):
        with pytest.raises(api.Refused, match="stopped"):
            enter(con, "dip-buyer-r2", at=at)
    assert len(store.trades(con)) == 1


@pytest.mark.parametrize("typed", ["GC1!", "gc1!", "GC=F", "COMEX:GC1!", "GC2!"])
def test_a_futures_ticker_in_any_form_is_a_future_on_its_root(con, typed):
    t = enter(con, ticker=typed, point_value=100.0)
    assert (t.ticker, t.instrument, t.point_value) == ("GC", "future", 100.0)


@pytest.mark.parametrize(("typed", "root"), [
    ("6E1!", "6E"), ("MNQ1!", "MNQ"), ("ZN=F", "ZN"), ("acme", "ACME"), ("BRK.B", "BRK.B")])
def test_canonical_tickers(con, typed, root):
    pv = {} if root in ("ACME", "BRK.B") else {"point_value": 50.0}
    t = enter(con, ticker=typed, **pv)
    assert t.ticker == root
    assert t.instrument == ("stock" if root in ("ACME", "BRK.B") else "future")


def test_a_future_without_a_point_value_is_refused(con):
    with pytest.raises(api.Refused, match="point_value"):
        enter(con, ticker="GC1!")
    assert store.trades(con) == []


def test_a_continuous_future_cannot_be_a_stock(con):
    with pytest.raises(api.Refused, match="future"):
        enter(con, ticker="GC1!", instrument="stock", point_value=100.0)


def test_an_unknown_instrument_is_refused(con):
    with pytest.raises(api.Refused, match="instrument"):
        enter(con, instrument="bond")


def test_a_trade_id_is_safe_to_type_unquoted_and_collides_with_a_suffix(con):
    a = enter(con, ticker="GC1!", point_value=100.0)
    b = enter(con, ticker="GC=F", point_value=100.0)
    c = enter(con, "dip-buyer-r2", ticker="GC1!", point_value=100.0)
    assert (a.id, b.id, c.id) == ("gc-20260302", "gc-20260302-2", "gc-20260302-3")
    assert enter(con, ticker="BRK.B").id == "brkb-20260302"


@pytest.mark.parametrize("kw", [
    dict(entry_type="stop"), dict(entry=0), dict(entry=-1), dict(entry=float("nan")),
    dict(entry=float("inf")), dict(stop=0), dict(stop=-2), dict(target=float("nan")),
    dict(target=0), dict(stop=100.0), dict(size=0), dict(size=-1), dict(size=1.5),
    dict(size=True), dict(currency="EUR"), dict(reasons=""), dict(reasons="   "),
    dict(side="up"), dict(ticker=" ")])
def test_bad_figures_are_refused_and_write_nothing(con, kw):
    with pytest.raises(api.Refused):
        enter(con, **kw)
    assert store.trades(con) == [] and not config.attachments_dir().exists()


def test_a_stop_and_target_may_be_left_out(con):
    d = enter(con, stop=None, target=None).decisions.entry
    assert (d.stop, d.target) == (None, None)


def test_the_same_file_attached_twice_is_stored_once(con, tmp_path):
    f = tmp_path / "chart.png"
    f.write_bytes(b"pixels")
    digest = hashlib.sha256(b"pixels").hexdigest()
    a = enter(con, screenshots=[str(f), "https://example.com/c.png"])
    b = enter(con, screenshots=[str(f)])
    assert a.decisions.screenshots == (
        {"file": f"{digest}.png", "name": "chart.png"}, {"link": "https://example.com/c.png"})
    assert b.decisions.screenshots == ({"file": f"{digest}.png", "name": "chart.png"},)
    assert [p.name for p in config.attachments_dir().iterdir()] == [f"{digest}.png"]


@pytest.mark.parametrize("bad", ["missing.png", ".", "http://example.com/c.png", "ftp://x/y"])
def test_a_bad_screenshot_is_refused_before_anything_is_written(con, tmp_path, bad):
    good = tmp_path / "ok.png"
    good.write_bytes(b"x")
    bad = bad if "://" in bad else str(tmp_path / bad)
    with pytest.raises(api.Refused, match="screenshot"):
        enter(con, screenshots=[str(good), bad])
    assert store.trades(con) == []
    assert not config.attachments_dir().exists()


def test_a_trade_refused_for_the_run_stop_stores_no_screenshot(con, tmp_path):
    f = tmp_path / "chart.png"
    f.write_bytes(b"pixels")
    stop = NOW + timedelta(days=3)
    api.stop_run(con, "dip-buyer-r2", at=stop, now=NOW)
    with pytest.raises(api.Refused, match="stopped"):
        enter(con, "dip-buyer-r2", at=stop, screenshots=[str(f)])
    assert not config.attachments_dir().exists()
    enter(con, "dip-buyer-r2", at=stop - timedelta(minutes=1), screenshots=[str(f)])
    assert config.attachments_dir().exists()  # so the assertion above is not vacuous


@pytest.mark.parametrize("kw", [dict(ticker="GC"), dict(ticker="GC", instrument="stock"),
                                dict(ticker="SPY", instrument="etf"), dict(point_value=1.0)])
def test_a_point_value_on_a_trade_that_is_not_a_future_is_refused(con, kw):
    """Review item 1, Ruling R15: a bare root typed with a point value was recorded as a stock
    worth 1 a point, silently dropping the 100, and could not be corrected afterwards."""
    with pytest.raises(api.Refused, match="point_value is only for a future"):
        enter(con, **{"point_value": 100.0, **kw})
    assert store.trades(con) == []


def test_the_point_value_refusal_names_the_continuous_ticker_and_a_bare_root_may_be_a_future(con):
    with pytest.raises(api.Refused, match=r"'GC' reads as a stock.*'GC1!'"):
        enter(con, ticker="GC", point_value=100.0)
    t = enter(con, ticker="GC", instrument="future", point_value=100.0)
    assert (t.ticker, t.instrument, t.point_value) == ("GC", "future", 100.0)


def test_no_screenshots_may_be_given_as_none(con):
    assert enter(con, screenshots=None).decisions.screenshots == ()


@pytest.mark.parametrize("shots", ["x.png", [5], ["https://example.com/c.png", None], 7])
def test_screenshots_that_are_not_a_list_of_strings_are_refused(con, shots):
    """Review item 5: "x.png" was read letter by letter and refused as screenshot 'x'."""
    with pytest.raises(api.Refused, match="screenshots must be a list of strings"):
        enter(con, screenshots=shots)
    assert store.trades(con) == []


@pytest.mark.parametrize(("kw", "name"), [
    (dict(reasons=5), "reasons"), (dict(reasons=None), "reasons"), (dict(reasons=["x"]), "reasons"),
    (dict(ticker=5), "ticker")])
def test_text_arguments_that_are_not_text_are_refused(con, kw, name):
    with pytest.raises(api.Refused, match=f"{name} must be text"):
        enter(con, **kw)
    assert store.trades(con) == []
