"""Pine v6's indicator formulas and their warm-up counts. Every figure is invented."""
from datetime import UTC, datetime, timedelta

import pytest

from desk import indicators as ind
from desk.judge import Bar

T = datetime(2026, 3, 2, tzinfo=UTC)


def bar(h, lo, c, i=0):
    return Bar(T + timedelta(minutes=15 * i), c, h, lo, c, 1.0)


def test_moving_averages():
    assert ind.sma([1, 2, 3, 4], 4) == 2.5 and ind.sma([1, 2, 3], 4) is None
    assert ind.ema([1, 2, 3, 4, 5], 3) == 4.0
    assert ind.rma([1, 2, 3, 4, 5], 2) == 4.0625


def test_rsi_is_wilders_with_pines_edges():
    assert ind.rsi([10, 11, 10, 12], 2) == pytest.approx(83.33333333333333)
    assert ind.rsi([5, 5, 5, 5], 2) is None            # nothing moved: unknown
    assert ind.rsi([1, 2, 3, 4], 2) == 100.0 and ind.rsi([4, 3, 2, 1], 2) == 0.0
    assert ind.rsi([1, 2], 2) is None


def test_atr_counts_gaps():
    assert ind.atr([bar(11, 9, 10, 0), bar(12, 10, 11, 1), bar(13, 10, 12, 2)], 2) == 2.5
    assert ind.atr([bar(11, 9, 10, 0), bar(15, 14, 14.5, 1)], 2) == 3.5


def test_macd_line_signal_histogram():
    assert ind.macd([10, 11, 10.5, 12, 11.5, 13, 12.5, 14], 2, 3, 2) == pytest.approx(
        (0.328125, 0.28125, 0.046875))
    assert ind.macd([10, 11], 2, 3, 2) is None


@pytest.mark.parametrize("rule, settings, n", [
    ("rsi", {"length": 14}, 250), ("stop_atr", {"length": 14}, 250),
    ("macd", {"fast": 12, "slow": 26, "signal": 9}, 250), ("ema", {"length": 20}, 250),
    ("ema", {"length": 50}, 250), ("ema", {"length": 100}, 446), ("ema", {"length": 200}, 891),
    ("sma", {"length": 200}, 200), ("rsi", {"length": 1}, 250)])
def test_warmup(rule, settings, n):
    assert ind.warmup(rule, settings) == n
