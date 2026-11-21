"""TradingView Pine v6's indicator formulas, and how many bars each needs to settle.

Pure: no connection, clock or network. Each function returns the value at the last element, or
None when there are too few values. ema and rma start from an SMA of the first `length` values,
as Pine's do.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

from desk.judge import Bar


def _seeded(values: Sequence[float], alpha: float, length: int) -> list[float]:
    """Pine's ema/rma with an SMA seed, aligned to values[length-1:]."""
    if len(values) < length:
        return []
    out = [sum(values[:length]) / length]
    for v in values[length:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def _last(series: list[float]) -> float | None:
    return series[-1] if series else None


def sma(closes: Sequence[float], length: int) -> float | None:
    return sum(closes[-length:]) / length if len(closes) >= length else None


def ema(closes: Sequence[float], length: int) -> float | None:
    return _last(_seeded(closes, 2 / (length + 1), length))


def rma(values: Sequence[float], length: int) -> float | None:
    return _last(_seeded(values, 1 / length, length))


def rsi(closes: Sequence[float], length: int) -> float | None:
    changes = [b - a for a, b in zip(closes, closes[1:], strict=False)]
    gain = rma([max(c, 0.0) for c in changes], length)
    loss = rma([max(-c, 0.0) for c in changes], length)
    if gain is None or loss is None or gain == loss == 0:
        return None   # too few closes, or none moved
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def atr(bars: Sequence[Bar], length: int) -> float | None:
    ranges = [b.high - b.low if i == 0 else
              max(b.high - b.low, abs(b.high - bars[i - 1].close), abs(b.low - bars[i - 1].close))
              for i, b in enumerate(bars)]
    return rma(ranges, length)


def macd(closes: Sequence[float], fast: int, slow: int, signal: int
         ) -> tuple[float, float, float] | None:
    f, s = _seeded(closes, 2 / (fast + 1), fast), _seeded(closes, 2 / (slow + 1), slow)
    n = min(len(f), len(s))   # the bars where both exist, aligned at the end
    line = [a - b for a, b in zip(f[len(f) - n:], s[len(s) - n:], strict=True)]
    sig = _last(_seeded(line, 2 / (signal + 1), signal))
    return None if sig is None else (line[-1], sig, line[-1] - sig)


def _term(alpha: float) -> int:
    """Bars until the seed's weight is under 0.1%."""
    return 0 if alpha >= 1 else math.ceil(math.log(0.001) / math.log(1 - alpha))


def warmup(rule: str, settings: dict) -> int:
    """Bars of history a rule's figure needs before it matches TradingView's."""
    if rule == "sma":
        return settings["length"]
    if rule == "macd":
        slow, sig = settings["slow"], settings["signal"]
        return max(250, slow + _term(2 / (slow + 1)) + sig + _term(2 / (sig + 1)))
    length = settings["length"]
    if rule == "ema":
        return max(250, length + _term(2 / (length + 1)))
    if rule == "stop_atr":
        return max(250, length + _term(1 / length))
    if rule == "rsi":   # its averages start at the first change: two closes
        return max(250, length + 1 + _term(1 / length))
    raise ValueError(f"no warm-up for rule {rule!r}")
