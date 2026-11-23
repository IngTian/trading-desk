# How it works

## The model

| Thing | What it is |
|---|---|
| **Strategy** | A Markdown file you write: the idea, how you enter, where the target and the stop go, one `rules` block that each trade is checked against, and a checklist you answer at each entry, recorded apart from the rules. |
| **Version** | A strategy's text at one moment. Any change to the text makes a new version, except for spacing within a line, trailing spaces, line endings and extra blank lines. A saved version never changes. |
| **Run** | One version deployed over a period, in one currency. Its mode is `live` (trading now) or `replay` (re-running past history). The desk reads no market data in either mode. Its tags, such as `paper` or `GC 15m`, are stored and never interpreted. |
| **Trade** | One entry in one run: ticker, side, `instrument` and `point_value` (1 unless a future), a market or limit entry, stop, target, size, your reasons, and screenshots. |
| **Decision** | Anything done to a trade after entry: a move of its stop or target, a report, a correction, or a void. Each carries a reason. |

1. **Only decisions are stored.** Your reports are decisions too. Fills, exits, statistics and
   whether a trade kept its rules are worked out from them whenever they are read, so nothing
   derived goes stale. The one exception: the rule results shown at entry, and the `market` values
   and checklist answers you gave, are kept with the trade as a record of what you saw. A
   correction is a new entry read in place of the one it corrects. Nothing is edited or deleted.
2. **One timing rule.** Every decision has an `at`, in UTC: the moment it takes effect, or, for a
   report, the moment whose state it gives. On a live run an entry, a move, a start and a stop
   default to now, and on a replay run you give every one, as the close of the bar you acted on. A
   report gives its own `at` in both modes. Any `at` after now is refused, and so is a time without
   a timezone. Corrections and voids are dated when you make them.

## How a trade's outcome is read

An order is `pending`, `filled` or `cancelled`: a market order fills as you enter it, and a limit
order is pending until it fills or is cancelled. A position is `open` until it exits at its stop, at
its target or by hand. Every read carries the trade's `outcome`: its status (`pending`, `open`,
`exited` or `cancelled`), the fill, the exit and its kind, R (the move from the fill to the exit over
the fill's distance to the entry's stop, never a moved stop) and cash (the price move × size × point
value, in the trade's currency). A cancellation's why is `not filled` (the market never reached the
limit, or the order expired), `pulled` (you withdrew it) or `run stopped`; the last is dated only at
the run's stop, and the fill rate leaves it out.

**Reports are the only intake.** A trade's fill, exit and cancellation come from your reports and
from nothing else, on a live run as on a replay one. `report` states the trade's state as of its
`at`: `filled` (at the limit unless you give `price`), `cancelled` (with its why), `open` (still
held) or `exited` (`kind="stop"` or `"target"`, at the level in force unless you give `price`, or
`kind="hand"`, the default, always with its `price`). A position with no exit reported reads open,
and a limit with no fill reported reads cancelled at its run's stop once the run has one.

Four words name a report everywhere: `filled`, `cancelled`, `open` and `exited`. They are the states
`report` takes, the kinds of event stored, the fields of the folded record, and what `correct_trade`
and `void_trade` name, so `report(state="exited")` is withdrawn as `void_trade(report="exited")`.

**Every report is checked against your record**, with no market data, and one that fails is refused,
saying which, with nothing written: a fill only on a limit order, never worse than its limit and not
after the run's stop; `run stopped` only at the run's stop; an exit or an `open` report only on a
position, so a limit's fill is reported first; an `open` report at or after the position opened and
before the exit; a hand exit with its price; a stop or target exit only with that level in force,
never better than the stop or worse than the target; one fill and one exit per trade; and every
decision inside its run and in order with the entry and the moves.
