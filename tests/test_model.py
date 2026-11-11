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
        actor="ing",
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
    store.append(con, kind="exited", actor="ing", source="chat", trade_id=trade_id,
                 payload={"price": price, "size": size}, at=at.isoformat())


def _close(con, trade_id, at):
    store.append(con, kind="closed", actor="ing", source="chat", trade_id=trade_id,
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
        actor="ing",
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
        actor="ing",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={"stop": 98.0},
    )
    # Move stop to 99
    store.append(
        con,
        kind="stop_moved",
        actor="ing",
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
        actor="ing",
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
        actor="ing",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={"price": 105.0, "size": 2},
    )
    # Exit 3 shares at 108
    store.append(
        con,
        kind="exited",
        actor="ing",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={"price": 108.0, "size": 3},
    )

    state = model.state_of(con, invented.TEST_TRADE_ID)
    assert state.size_open == 0, "All shares should be closed"
    assert state.size_closed == 5, "All shares should be accounted for"
    assert len(state.fills) == 2, "There should be two fill records"
    # Realised R calculation: (exit - entry) / (entry - stop) * shares
    # First exit: (105 - 100) / (100 - 95) * 2 = 5/5 * 2 = 2.0 R
    # Second exit: (108 - 100) / (100 - 95) * 3 = 8/5 * 3 = 4.8 R
    # Total: 6.8 R
    assert state.realised_r == pytest.approx(6.8), "Realised R should sum across fills"


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
        actor="ing",
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
        actor="ing",
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
        actor="ing",
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
        actor="ing",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={"price": 105.0, "size": 7},
    )

    state = model.state_of(con, invented.TEST_TRADE_ID)
    # The state should track this as an invariant violation
    assert "exit_exceeds_size" in state.taken_against, \
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
        actor="ing",
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
        actor="ing",
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
        actor="ing",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={"price": 105.0, "size": 10},
    )
    # Label AFTER exit (hindsight)
    store.append(
        con,
        kind="labelled",
        actor="ing",
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
        actor="ing",
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
        actor="ing",
        source="chat",
        trade_id=invented.TEST_TRADE_ID,
        payload={"price": 90.0, "size": 10},
    )

    state = model.state_of(con, invented.TEST_TRADE_ID)
    # For short: (entry - exit) / abs(entry - stop) * shares
    # (100 - 90) / abs(100 - 105) * 10 = 10 / 5 * 10 = 20.0 R
    assert state.realised_r == pytest.approx(20.0), \
        "A profitable short should have positive R"


def test_a_stop_fill_dates_the_cooldown_from_the_trades_close(con):
    """A long filling at or below its stop is a stop, and the cooldown runs from the close."""
    _trade(con, "trade-a")
    _exit(con, "trade-a", 95.0, STOPPED_AT)
    closed_at = STOPPED_AT + dt.timedelta(minutes=10)
    _close(con, "trade-a", closed_at)

    assert model.stopped_closes(con) == {invented.TICKERS[0]: closed_at}


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
    store.append(con, kind="closed", actor="ing", source="chat", trade_id="trade-a", payload={})

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
