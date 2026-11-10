"""Tests for the pure formatters moved from the TUI."""
from desk import fmt


def test_a_figure_that_rounds_to_zero_claims_no_sign():
    """Carried from the deleted widgets test: the feed returns single-precision closes, so a
    flat row's P&L is a few hundred-thousandths of a cent, and the sign it picks is arbitrary."""
    import struct
    mark = struct.unpack("f", struct.pack("f", 25.07))[0]
    tiny = 100 * (mark - 25.07)
    assert tiny < 0 and abs(tiny) < 0.005
    assert fmt.signed(tiny) == "+0.00"
    assert fmt.signed(-0.006) == "-0.01", "a real cent still reads as one"
