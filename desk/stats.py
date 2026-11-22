"""Stats over one scope's non-void trades: counts, rates and averages, each beside the trade ids
it rests on. Never a verdict, a grade or a ranking. Pure: api.stats builds the rows."""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from desk import judge

_RESIDUE = 1e-9   # an R this close to 0 is a breakeven: float residue, not a win or a loss


@dataclass(frozen=True)
class Row:
    """One non-void trade in scope."""
    id: str
    run: str
    version: int
    mode: str
    tags: tuple[str, ...]          # its run's
    entry_type: str
    currency: str
    outcome: judge.Outcome
    planned_rr: float | None       # checks.reward_risk of the entry as corrected


@dataclass(frozen=True)
class Stats:
    strategy: str
    version: int | None
    run: str | None
    mode: str | None
    tags: tuple[str, ...]
    conforming_only: bool
    trades: tuple[str, ...]                   # every trade the figures rest on
    closed: tuple[str, ...]                   # exited, with R
    wins: tuple[str, ...]
    losses: tuple[str, ...]
    breakevens: tuple[str, ...]
    win_rate: float | None
    average_r: float | None
    total_r: float | None
    average_win: float | None
    average_loss: float | None                # negative
    average_planned_rr: float | None
    no_planned_rr: tuple[str, ...]            # closed, without a planned reward to risk
    closed_no_r: tuple[str, ...]              # exited, R withheld; its cash is in pnl
    pnl: dict[str, float]                     # per currency, gross of fees, never converted
    open: tuple[str, ...]
    pending: tuple[str, ...]
    cancelled: dict[str, tuple[str, ...]]     # by why
    # {"placed", "filled", "pending", "cancelled": {why: ids}, "rate"}
    limits: dict
    left_out: dict    # conforming_only: {"breached": ids, "unknown": {result name: ids}}; else {}
    # Trade ids, in `trades`' order: {"mode": {m: ids}, "tags": [{"tags": [...], "trades": ids}],
    #  "version": {v: ids}}
    rests_on: dict


def _ids(rows: Sequence[Row]) -> tuple[str, ...]:
    return tuple(r.id for r in rows)


def _grouped(rows: Sequence[Row], key) -> dict:
    """Trade ids by `key`, groups in first-seen order, ids in `rows`' order."""
    groups: dict = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r.id)
    return {k: tuple(ids) for k, ids in groups.items()}


def _mean(xs: list[float]) -> float | None:
    return math.fsum(xs) / len(xs) if xs else None


def _by_why(rows: Sequence[Row]) -> dict[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {}
    for r in rows:
        if r.outcome.status == "cancelled":
            out.setdefault(r.outcome.cancelled_why, []).append(r.id)
    return {why: tuple(ids) for why, ids in out.items()}


def summarise(rows: Sequence[Row], *, strategy: str, version: int | None, run: str | None,
              mode: str | None, tags: tuple[str, ...], conforming_only: bool,
              left_out: dict) -> Stats:
    """The buckets read the outcome only. A figure with nothing under it is None."""
    def having(*status: str) -> list[Row]:
        return [r for r in rows if r.outcome.status in status]
    exited = having("exited")
    closed = [r for r in exited if r.outcome.r is not None]
    rs = [r.outcome.r for r in closed]
    wins = [r for r in closed if r.outcome.r > _RESIDUE]
    losses = [r for r in closed if r.outcome.r < -_RESIDUE]
    planned = [r.planned_rr for r in closed if r.planned_rr is not None]
    cash: dict[str, list[float]] = {}
    for r in exited:            # closed and closed_no_r alike
        cash.setdefault(r.currency, []).append(r.outcome.cash)

    limit = [r for r in rows if r.entry_type == "limit"]
    by_why = _by_why(limit)
    l_pending = [r for r in limit if r.outcome.status == "pending"]
    filled = [r for r in limit if r.outcome.fill is not None and r.outcome.status != "cancelled"]
    # `run stopped` ends an order for a reason unrelated to its market, so it is left out
    base = len(limit) - len(l_pending) - len(by_why.get("run stopped", ()))

    return Stats(
        strategy=strategy, version=version, run=run, mode=mode, tags=tags,
        conforming_only=conforming_only, trades=_ids(rows), closed=_ids(closed),
        wins=_ids(wins), losses=_ids(losses),
        breakevens=_ids([r for r in closed if abs(r.outcome.r) <= _RESIDUE]),
        win_rate=len(wins) / len(closed) if closed else None,
        average_r=_mean(rs), total_r=math.fsum(rs) if rs else None,
        average_win=_mean([r.outcome.r for r in wins]),
        average_loss=_mean([r.outcome.r for r in losses]),
        average_planned_rr=_mean(planned),
        no_planned_rr=_ids([r for r in closed if r.planned_rr is None]),
        closed_no_r=_ids([r for r in exited if r.outcome.r is None]),
        pnl={c: math.fsum(xs) for c, xs in cash.items()},
        open=_ids(having("open")), pending=_ids(having("pending")),
        cancelled=_by_why(rows),
        limits={"placed": _ids(limit), "filled": _ids(filled), "pending": _ids(l_pending),
                "cancelled": by_why,
                "rate": len(filled) / base if base else None},
        left_out=left_out,
        rests_on={"mode": _grouped(rows, lambda r: r.mode),
                  "tags": [{"tags": list(t), "trades": ids}
                           for t, ids in _grouped(rows, lambda r: r.tags).items()],
                  "version": _grouped(rows, lambda r: r.version)})
