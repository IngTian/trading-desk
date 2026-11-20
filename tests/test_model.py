"""Tests for deriving current state from the event log."""
from __future__ import annotations

import datetime as dt

import invented
import pytest

from desk import model, store

STOPPED_AT = dt.datetime(2026, 5, 4, 14, 0, tzinfo=dt.UTC)


def _trade(con, trade_id, *, side="long", stop=95.0, ticker=None):
    store.add_trade(
        con,
        trade_id=trade_id,
        account="paper",
        run_id=None,
        ticker=ticker or invented.TICKERS[0],
        instrument="stock",
        point_value=1.0,
        currency="USD",
        side=side,
    )
    store.append(
        con,
        kind="entered",
        actor="owner",
        source="chat",
        trade_id=trade_id,
        payload={
            "entry": 100.0,
            "stop": stop,
            "target": 110.0 if side == "long" else 90.0,
            "size": 10,
            "horizon": "2w",
            "setup": "test-setup",
            "thesis": "test thesis",
        },
    )


def _exit(con, trade_id, price, at, *, size=10):
    store.append(con, kind="exited", actor="owner", source="chat", trade_id=trade_id,
                 payload={"price": price, "size": size}, at=at.isoformat())


def _close(con, trade_id, at):
    store.append(con, kind="closed", actor="owner", source="chat", trade_id=trade_id,
                 payload={}, at=at.isoformat())


@pytest.fixture
def con(tmp_path):
    """A fresh database for each test."""
    db = tmp_path / "test.db"
    store.create(db)
    return store.open(db)


def test_a_stop_move_is_the_latest_one_that_counts(con):
    """Multiple stop_moved events: the last one wins."""
    store.add_trade(
        con,
        trade_id=invented.TEST_TRADE_ID,
        account="paper",
        run_id=None,
        ticker=invented.TICKERS[0],
        instrument="stock",
        point_value=1.0,
        currency="USD",
        side="long",
    )
    # Enter with stop at 95
    store.append(
        con,
        kind="entered",
        actor="owner",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={
            "entry": invented.PRICES[invented.TICKERS[0]],
            "stop": 95.0,
            "target": 110.0,
            "size": 10,
            "horizon": "2w",
            "setup": "test-setup",
            "thesis": "test thesis",
        },
    )
    # Move stop to 98
    store.append(
        con,
        kind="stop_moved",
        actor="owner",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={"stop": 98.0},
    )
    # Move stop to 99
    store.append(
        con,
        kind="stop_moved",
        actor="owner",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={"stop": 99.0},
    )

    state = model.state_of(con, invented.TEST_TRADE_ID)
    assert state.stop == 99.0, "The latest stop move should win"


def test_partial_exits_reduce_the_open_size_and_realise_r_on_their_share(con):
    """Two exits of 2 and 3 on a size of 5 leave nothing open and realise R on each share."""
    store.add_trade(
        con,
        trade_id=invented.TEST_TRADE_ID,
        account="paper",
        run_id=None,
        ticker=invented.TICKERS[0],
        instrument="stock",
        point_value=1.0,
        currency="USD",
        side="long",
    )
    # Enter long at 100, stop at 95, target at 110, size 5
    store.append(
        con,
        kind="entered",
        actor="owner",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={
            "entry": 100.0,
            "stop": 95.0,
            "target": 110.0,
            "size": 5,
            "horizon": "2w",
            "setup": "test-setup",
            "thesis": "test thesis",
        },
    )
    # Exit 2 shares at 105
    store.append(
        con,
        kind="exited",
        actor="owner",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={"price": 105.0, "size": 2},
    )
    # Exit 3 shares at 108
    store.append(
        con,
        kind="exited",
        actor="owner",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={"price": 108.0, "size": 3},
    )

    state = model.state_of(con, invented.TEST_TRADE_ID)
    assert state.size_open == 0, "All shares should be closed"
    assert state.size_closed == 5, "All shares should be accounted for"
    assert len(state.fills) == 2, "There should be two fill records"
    # Each fill realises R on its share of the 5 entered: (exit - entry) / (entry - stop) x share.
    # First exit: (105 - 100) / (100 - 95) x 2/5 = 0.4 R
    # Second exit: (108 - 100) / (100 - 95) x 3/5 = 0.96 R
    # Total: 1.36 R. This test used to pin 6.8, which was R x shares: the same trade on 50
    # shares would have read 68R.
    assert state.realised_r == pytest.approx(1.36), "Realised R should sum across fills"


def test_a_corrected_event_is_superseded_by_the_one_that_corrects_it(con):
    """The fold must read the correction, not the original -- and both stay in the log."""
    store.add_trade(
        con,
        trade_id=invented.TEST_TRADE_ID,
        account="paper",
        run_id=None,
        ticker=invented.TICKERS[0],
        instrument="stock",
        point_value=1.0,
        currency="USD",
        side="long",
    )
    # Enter with wrong stop at 90
    event_id = store.append(
        con,
        kind="entered",
        actor="owner",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={
            "entry": 100.0,
            "stop": 90.0,  # Wrong
            "target": 110.0,
            "size": 10,
            "horizon": "2w",
            "setup": "test-setup",
            "thesis": "test thesis",
        },
    )
    # Correct it with right stop at 95
    store.append(
        con,
        kind="entered",
        actor="owner",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        corrects=event_id,
        payload={
            "entry": 100.0,
            "stop": 95.0,  # Corrected
            "target": 110.0,
            "size": 10,
            "horizon": "2w",
            "setup": "test-setup",
            "thesis": "test thesis",
        },
    )

    state = model.state_of(con, invented.TEST_TRADE_ID)
    assert state.stop == 95.0, "The correction should supersede the original"
    # Verify both events are still in the log
    events = store.events(con, trade_id=invented.TEST_TRADE_ID)
    assert len(events) == 2, "Both events should remain in the log"


def test_an_exit_cannot_exceed_the_open_size(con):
    """Not a SQL CHECK -- it spans rows -- so the fold reports it and integrity.py fails on it."""
    store.add_trade(
        con,
        trade_id=invented.TEST_TRADE_ID,
        account="paper",
        run_id=None,
        ticker=invented.TICKERS[0],
        instrument="stock",
        point_value=1.0,
        currency="USD",
        side="long",
    )
    # Enter with size 5
    store.append(
        con,
        kind="entered",
        actor="owner",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={
            "entry": 100.0,
            "stop": 95.0,
            "target": 110.0,
            "size": 5,
            "horizon": "2w",
            "setup": "test-setup",
            "thesis": "test thesis",
        },
    )
    # Try to exit 7 shares (exceeds open size)
    store.append(
        con,
        kind="exited",
        actor="owner",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={"price": 105.0, "size": 7},
    )

    state = model.state_of(con, invented.TEST_TRADE_ID)
    # The state should track this as an invariant violation
    assert "exit_exceeds_size" in state.violations, \
        "Over-exit should be flagged as an invariant violation"


def test_a_passed_trade_never_opens(con):
    """A passed trade stays in planned status and never transitions to open."""
    store.add_trade(
        con,
        trade_id=invented.TEST_TRADE_ID,
        account="paper",
        run_id=None,
        ticker=invented.TICKERS[0],
        instrument="stock",
        point_value=1.0,
        currency="USD",
        side="long",
    )
    # Pass on the trade
    store.append(
        con,
        kind="passed",
        actor="owner",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={"setup": "test-setup", "thesis": "test thesis"},
    )

    state = model.state_of(con, invented.TEST_TRADE_ID)
    assert state.status == "passed", "A passed trade should have status 'passed'"
    assert state.size_open is None, "A passed trade should have no open size"


def test_the_setup_label_records_when_it_was_written_relative_to_the_exit(con):
    """The hindsight guard: a label written after the exit is marked as such."""
    store.add_trade(
        con,
        trade_id=invented.TEST_TRADE_ID,
        account="paper",
        run_id=None,
        ticker=invented.TICKERS[0],
        instrument="stock",
        point_value=1.0,
        currency="USD",
        side="long",
    )
    # Enter
    store.append(
        con,
        kind="entered",
        actor="owner",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={
            "entry": 100.0,
            "stop": 95.0,
            "target": 110.0,
            "size": 10,
            "horizon": "2w",
            "setup": "unlabelled",
            "thesis": "test thesis",
        },
    )
    # Exit
    exit_event_id = store.append(
        con,
        kind="exited",
        actor="owner",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={"price": 105.0, "size": 10},
    )
    # Label AFTER exit (hindsight)
    store.append(
        con,
        kind="labelled",
        actor="owner",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={"setup": "labelled-after-exit"},
    )

    state = model.state_of(con, invented.TEST_TRADE_ID)
    assert state.setup == "labelled-after-exit", "The label should be recorded"
    assert state.setup_labelled_at is not None, \
        "setup_labelled_at should record the event ID of the labelling"
    # The hindsight guard: labelled_at should be after the exit
    assert state.setup_labelled_at > exit_event_id, \
        "A label written after exit should be marked as such"


def test_realised_r_respects_side_for_short_positions(con):
    """A short with entry 100, stop 105, exit 90 realises positive R."""
    store.add_trade(
        con,
        trade_id=invented.TEST_TRADE_ID,
        account="paper",
        run_id=None,
        ticker=invented.TICKERS[0],
        instrument="stock",
        point_value=1.0,
        currency="USD",
        side="short",
    )
    # Enter short at 100, stop at 105, target at 90, size 10
    store.append(
        con,
        kind="entered",
        actor="owner",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={
            "entry": 100.0,
            "stop": 105.0,
            "target": 90.0,
            "size": 10,
            "horizon": "2w",
            "setup": "test-setup",
            "thesis": "test thesis",
        },
    )
    # Exit at 90 (profit for short)
    store.append(
        con,
        kind="exited",
        actor="owner",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={"price": 90.0, "size": 10},
    )

    state = model.state_of(con, invented.TEST_TRADE_ID)
    # For short: (entry - exit) / abs(entry - stop), on all 10 of the 10 shares entered
    # (100 - 90) / abs(100 - 105) = 2.0 R
    assert state.realised_r == pytest.approx(2.0), \
        "A profitable short should have positive R"


def test_a_stop_fill_dates_the_cooldown_from_when_the_trade_went_flat(con):
    """A long filling at or below its stop is a stop, and the cooldown runs from that fill.

    Not from the `closed` event ten minutes later: that is bookkeeping, and dating the cooldown
    from it meant a stop-out nobody had closed started no cooldown at all.
    """
    _trade(con, "trade-a")
    _exit(con, "trade-a", 95.0, STOPPED_AT)
    _close(con, "trade-a", STOPPED_AT + dt.timedelta(minutes=10))

    assert model.stopped_closes(con) == {invented.TICKERS[0]: STOPPED_AT}


def test_a_trade_still_open_through_its_stop_is_not_a_stopped_close(con):
    """The rule measures from a CLOSE, and a partial fill through the stop is not one."""
    _trade(con, "trade-a")
    _exit(con, "trade-a", 94.0, STOPPED_AT, size=4)  # 6 of 10 still open

    state = model.state_of(con, "trade-a")
    assert state.status == "open" and state.size_open == 6
    assert model.stopped_closes(con) == {}, "a trade in progress starts no cooldown"

    # It counts once it is closed.
    _close(con, "trade-a", STOPPED_AT)
    assert model.stopped_closes(con) == {invented.TICKERS[0]: STOPPED_AT}


def test_a_trade_that_hit_its_target_is_not_a_stop(con):
    """Nothing to cool down from, and an empty dict is an answer rather than a missing one."""
    _trade(con, "trade-a")
    _exit(con, "trade-a", 105.0, STOPPED_AT)
    store.append(con, kind="closed", actor="owner", source="chat", trade_id="trade-a", payload={})

    assert model.stopped_closes(con) == {}


def test_a_short_stops_out_above_its_stop_not_below(con):
    """The side decides which way a stop is breached, so _signed does it here too."""
    _trade(con, "trade-a", side="short", stop=105.0)
    _exit(con, "trade-a", 106.0, STOPPED_AT)
    _close(con, "trade-a", STOPPED_AT)
    assert model.stopped_closes(con) == {invented.TICKERS[0]: STOPPED_AT}

    # The same fill on a long would be a profit, not a stop.
    _trade(con, "trade-b", ticker=invented.TICKERS[1])
    _exit(con, "trade-b", 106.0, STOPPED_AT)
    _close(con, "trade-b", STOPPED_AT)
    assert invented.TICKERS[1] not in model.stopped_closes(con)


def test_the_most_recent_stop_on_a_ticker_is_the_one_that_counts(con):
    earlier = STOPPED_AT - dt.timedelta(days=2)
    _trade(con, "trade-a")
    _exit(con, "trade-a", 94.0, earlier)
    _close(con, "trade-a", earlier)
    _trade(con, "trade-b")
    _exit(con, "trade-b", 95.0, STOPPED_AT)
    _close(con, "trade-b", STOPPED_AT)

    assert model.stopped_closes(con) == {invented.TICKERS[0]: STOPPED_AT}


def test_trade_state_carries_account_and_run_id(con):
    """TradeState includes account and run_id from the trades row."""
    store.add_trade(
        con,
        trade_id=invented.TEST_TRADE_ID,
        account="real",
        run_id=None,
        ticker=invented.TICKERS[0],
        instrument="stock",
        point_value=1.0,
        currency="USD",
        side="long",
    )
    store.append(
        con,
        kind="entered",
        actor="owner",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={
            "entry": 100.0,
            "stop": 95.0,
            "target": 110.0,
            "size": 10,
            "horizon": "2w",
            "setup": "test-setup",
            "thesis": "test thesis",
        },
    )

    state = model.state_of(con, invented.TEST_TRADE_ID)
    assert state.account == "real"
    assert state.run_id is None


def test_open_trades_filters_by_account(con):
    """A paper trade must not appear in open_trades(con, account='real')."""
    # Create a real trade
    store.add_trade(
        con,
        trade_id="real-1",
        account="real",
        run_id=None,
        ticker=invented.TICKERS[0],
        instrument="stock",
        point_value=1.0,
        currency="USD",
        side="long",
    )
    store.append(
        con,
        kind="entered",
        actor="owner",
        source="chat",
        trade_id="real-1",
        payload={
            "entry": 100.0,
            "stop": 95.0,
            "target": 110.0,
            "size": 10,
            "horizon": "2w",
            "setup": "test-setup",
            "thesis": "test thesis",
        },
    )

    # Create a run for paper trades
    store.ensure_run(
        con,
        run_id="run-1",
        kind="paper",
        started="2026-01-01T00:00:00Z",
        rules_section="## Paper · run-1",
    )

    # Create a paper trade
    store.add_trade(
        con,
        trade_id="paper-1",
        account="paper",
        run_id="run-1",
        ticker=invented.TICKERS[1],
        instrument="stock",
        point_value=1.0,
        currency="USD",
        side="long",
    )
    store.append(
        con,
        kind="entered",
        actor="owner",
        source="chat",
        trade_id="paper-1",
        payload={
            "entry": 50.0,
            "stop": 45.0,
            "target": 60.0,
            "size": 20,
            "horizon": "1w",
            "setup": "test-setup",
            "thesis": "test thesis",
        },
    )

    # Unscoped: both trades
    all_open = model.open_trades(con)
    assert len(all_open) == 2
    assert {t.trade_id for t in all_open} == {"real-1", "paper-1"}

    # Scoped to real: only real-1
    real_open = model.open_trades(con, account="real")
    assert len(real_open) == 1
    assert real_open[0].trade_id == "real-1"

    # Scoped to paper: only paper-1
    paper_open = model.open_trades(con, account="paper")
    assert len(paper_open) == 1
    assert paper_open[0].trade_id == "paper-1"


def test_open_trades_filters_by_run_id(con):
    """A run-2 trade must not appear under run_id='run-3'."""
    # Create runs
    store.ensure_run(
        con,
        run_id="run-1",
        kind="paper",
        started="2026-01-01T00:00:00Z",
        rules_section="## Paper · run-1",
    )
    store.ensure_run(
        con,
        run_id="run-2",
        kind="paper",
        started="2026-01-02T00:00:00Z",
        rules_section="## Paper · run-2",
    )

    # Create a trade in run-1
    store.add_trade(
        con,
        trade_id="paper-run1",
        account="paper",
        run_id="run-1",
        ticker=invented.TICKERS[0],
        instrument="stock",
        point_value=1.0,
        currency="USD",
        side="long",
    )
    store.append(
        con,
        kind="entered",
        actor="owner",
        source="chat",
        trade_id="paper-run1",
        payload={
            "entry": 100.0,
            "stop": 95.0,
            "target": 110.0,
            "size": 10,
            "horizon": "2w",
            "setup": "test-setup",
            "thesis": "test thesis",
        },
    )

    # Create a trade in run-2
    store.add_trade(
        con,
        trade_id="paper-run2",
        account="paper",
        run_id="run-2",
        ticker=invented.TICKERS[1],
        instrument="stock",
        point_value=1.0,
        currency="USD",
        side="long",
    )
    store.append(
        con,
        kind="entered",
        actor="owner",
        source="chat",
        trade_id="paper-run2",
        payload={
            "entry": 50.0,
            "stop": 45.0,
            "target": 60.0,
            "size": 20,
            "horizon": "1w",
            "setup": "test-setup",
            "thesis": "test thesis",
        },
    )

    # Scoped to run-1: only paper-run1
    run1_open = model.open_trades(con, account="paper", run_id="run-1")
    assert len(run1_open) == 1
    assert run1_open[0].trade_id == "paper-run1"

    # Scoped to run-2: only paper-run2
    run2_open = model.open_trades(con, account="paper", run_id="run-2")
    assert len(run2_open) == 1
    assert run2_open[0].trade_id == "paper-run2"

    # Scoped to run-3: none
    run3_open = model.open_trades(con, account="paper", run_id="run-3")
    assert len(run3_open) == 0


def test_stopped_closes_filters_by_account(con):
    """Paper stop must not appear in stopped_closes(con, account='real')."""
    # Real account stopped trade
    _trade(con, "real-stopped", ticker=invented.TICKERS[0])
    # Override the account (the _trade helper defaults to paper)
    con.execute("UPDATE trades SET account = 'real' WHERE id = 'real-stopped'")
    _exit(con, "real-stopped", 94.0, STOPPED_AT)
    _close(con, "real-stopped", STOPPED_AT)

    # Paper account stopped trade
    _trade(con, "paper-stopped", ticker=invented.TICKERS[1])
    _exit(con, "paper-stopped", 44.0, STOPPED_AT)
    _close(con, "paper-stopped", STOPPED_AT)

    # Unscoped: both tickers
    all_stopped = model.stopped_closes(con)
    assert len(all_stopped) == 2
    assert set(all_stopped.keys()) == {invented.TICKERS[0], invented.TICKERS[1]}

    # Scoped to real: only TICKERS[0]
    real_stopped = model.stopped_closes(con, account="real")
    assert len(real_stopped) == 1
    assert invented.TICKERS[0] in real_stopped
    assert invented.TICKERS[1] not in real_stopped

    # Scoped to paper: only TICKERS[1]
    paper_stopped = model.stopped_closes(con, account="paper")
    assert len(paper_stopped) == 1
    assert invented.TICKERS[1] in paper_stopped
    assert invented.TICKERS[0] not in paper_stopped


# ---------------------------------------------------------------------------------------------
# R is measured against the INITIAL risk, the stop in force when the trade was entered.
# A stop moved later -- trailed, or to breakeven -- changes where the trade exits, never the size
# of the bet it started as. Measuring against the current stop crashed on a breakeven stop
# (a zero distance) and inflated R on every trailed one.


def _move_stop(con, trade_id, stop):
    store.append(con, kind="stop_moved", actor="owner", source="cli", trade_id=trade_id,
                 payload={"stop": stop})


def test_a_stop_moved_to_breakeven_does_not_break_the_fold(con):
    _trade(con, "trade-a")  # long 10 @ 100, stop 95, target 110
    _move_stop(con, "trade-a", 100.0)

    state = model.state_of(con, "trade-a")
    assert state.stop == 100.0, "the current stop is the moved one"
    assert state.initial_stop == 95.0, "the stop at entry is kept"
    assert state.planned_r == pytest.approx(2.0), "10 of reward over 5 of initial risk"

    _exit(con, "trade-a", 100.0, STOPPED_AT)
    state = model.state_of(con, "trade-a")
    assert state.realised_r == pytest.approx(0.0)
    # And every fold over the whole log still works, for this account and others.
    assert model.open_trades(con, account="real") == []
    model.open_trades(con)
    model.stopped_closes(con)


def test_a_trailed_stop_does_not_inflate_r(con):
    _trade(con, "trade-a")  # long 10 @ 100, stop 95
    _move_stop(con, "trade-a", 102.0)
    _exit(con, "trade-a", 102.0, STOPPED_AT)

    state = model.state_of(con, "trade-a")
    assert state.planned_r == pytest.approx(2.0), "against the 5-point risk at entry, not 2"
    assert state.realised_r == pytest.approx(0.4), "2 points won over 5 risked"


def test_realised_r_is_in_r_not_r_times_size(con):
    _trade(con, "trade-a")  # long 10 @ 100, stop 95: 1R is 5 points on 10 shares
    _exit(con, "trade-a", 110.0, STOPPED_AT, size=4)
    _exit(con, "trade-a", 95.0, STOPPED_AT, size=6)

    state = model.state_of(con, "trade-a")
    # 4 of 10 shares realise +2R on their share (+0.8R), 6 of 10 realise -1R (-0.6R).
    assert [f.realised_r for f in state.fills] == [pytest.approx(0.8), pytest.approx(-0.6)]
    assert state.realised_r == pytest.approx(0.2)


def test_an_entry_with_no_risk_withholds_r_instead_of_dividing(con):
    """The CLI refuses stop == entry, but the fold must survive one written another way."""
    _trade(con, "trade-a", stop=100.0)
    _exit(con, "trade-a", 104.0, STOPPED_AT)

    state = model.state_of(con, "trade-a")
    assert state.planned_r is None
    assert state.realised_r is None, "no initial risk means no R, which is not 0R"
    assert state.realised_cash == pytest.approx(40.0), "the cash result does not need R"


def test_realised_cash_uses_the_point_value(con):
    store.add_trade(con, trade_id="fut-a", account="replay", run_id=None,
                    ticker="GC1!", instrument="future",
                    point_value=invented.FUTURE_POINT_VALUE, currency="USD", side="long")
    store.append(con, kind="entered", actor="owner", source="cli", trade_id="fut-a",
                 payload={"entry": 2000.0, "stop": 1998.5, "target": 2003.0, "size": 1})
    store.append(con, kind="exited", actor="owner", source="cli", trade_id="fut-a",
                 payload={"price": 2004.0, "size": 1})

    state = model.state_of(con, "fut-a")
    assert state.realised_cash == pytest.approx(400.0), "4 points x 100 per point x 1 contract"
    assert state.currency == "USD"
    assert state.realised_r == pytest.approx(4.0 / 1.5)


def test_a_short_realises_cash_and_r_on_the_right_side(con):
    _trade(con, "trade-a", side="short", stop=105.0)  # short 10 @ 100, target 90
    _exit(con, "trade-a", 90.0, STOPPED_AT)

    state = model.state_of(con, "trade-a")
    assert state.realised_r == pytest.approx(2.0)
    assert state.realised_cash == pytest.approx(100.0)


# ---------------------------------------------------------------------------------------------
# The cooldown starts when the trade went flat through its stop, without waiting for `closed`.


def test_a_stop_out_starts_the_cooldown_without_a_closed_event(con):
    _trade(con, "trade-a")
    _exit(con, "trade-a", 95.0, STOPPED_AT)

    assert model.state_of(con, "trade-a").status == "open", "flat, not yet closed"
    assert model.stopped_closes(con) == {invented.TICKERS[0]: STOPPED_AT}


def test_a_stop_moved_after_the_exit_does_not_erase_the_stop_out(con):
    """The fill is judged against the stop in force when it filled, not the last stop."""
    _trade(con, "trade-a")
    _exit(con, "trade-a", 95.0, STOPPED_AT)
    _move_stop(con, "trade-a", 90.0)

    assert model.stopped_closes(con) == {invented.TICKERS[0]: STOPPED_AT}


# ---------------------------------------------------------------------------------------------
# Today's realised R, for daily_loss_limit. Each exit fill realises R on its share on the day
# it filled; an unknown R anywhere makes the day's total unknown, never smaller.


def test_realised_r_on_a_day_sums_that_days_fills_in_scope(con):
    day = STOPPED_AT.astimezone().date()
    _trade(con, "trade-a")
    _exit(con, "trade-a", 95.0, STOPPED_AT)                          # -1R that day
    _trade(con, "trade-b", ticker=invented.TICKERS[1])
    _exit(con, "trade-b", 110.0, STOPPED_AT - dt.timedelta(days=3))  # +2R, another day

    assert model.realised_r_on(con, day) == pytest.approx(-1.0)
    assert model.realised_r_on(con, day, account="real") == 0.0, "the log was read: nothing"


def test_realised_r_on_a_day_is_withheld_when_one_fill_has_no_r(con):
    day = STOPPED_AT.astimezone().date()
    _trade(con, "trade-a")
    _exit(con, "trade-a", 95.0, STOPPED_AT)
    _trade(con, "trade-b", stop=100.0, ticker=invented.TICKERS[1])  # no initial risk
    _exit(con, "trade-b", 99.0, STOPPED_AT)

    assert model.realised_r_on(con, day) is None
