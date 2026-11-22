# How it works

## The model

| Thing | What it is |
|---|---|
| **Strategy** | A Markdown file you write: the idea, how you enter, where the target and the stop go, one `rules` block that each trade is checked against, and a checklist you answer at each entry, recorded apart from the rules. |
| **Version** | A strategy's text at one moment. Any change to the text makes a new version, except for spacing within a line, trailing spaces, line endings and extra blank lines. A saved version never changes. |
| **Run** | One version deployed over a period, in one currency. Its mode is `live` (trading now, judged from Yahoo's bars) or `replay` (re-running past history: the desk reads no bars, only your reports). Its tags, such as `paper` or `GC 15m`, are stored and never interpreted. |
| **Trade** | One entry in one run: ticker, side, `instrument` and `point_value` (1 unless a future), a market or limit entry, stop, target, size, currency, your reasons, and screenshots. |
| **Decision** | Anything done to a trade after entry: a move of its stop or target, a close by hand, a report, a correction, or a void. Each carries a reason. |

1. **Only decisions and bars are stored.** Your reports are decisions too, and bars are stored as
   fetched and never changed. Fills, exits, statistics and whether a trade kept its rules are worked
   out from those two whenever they are read, so nothing derived goes stale. The one exception: the
   rule results shown at entry, and the `market` values and checklist answers you gave, are kept
   with the trade as a record of what you saw. A correction is a new entry read in place of the one
   it corrects. Nothing is edited or deleted.
2. **One timing rule.** Every decision has an `at`, in UTC: the moment it takes effect, or, for a
   report, the moment whose state it gives. On a live run an entry, a move, a close, a start and a
   stop default to now, and a report gives its own. On a replay run you give every `at`, as the
   close of the bar you acted on. Any `at` after now is refused, and so is a time without a
   timezone. A decision takes effect from the first bar that opens at or after its `at`. Corrections
   and voids are dated when you make them.

## How a trade's outcome is read

An order is `pending`, `filled` or `cancelled`: a market order fills as you enter it, and a limit
order is pending until it fills or is cancelled. A position is `open` until it exits at its stop, at
its target or by hand. Every read carries the trade's `outcome`: its status (`pending`, `open`,
`closing`, `exited`, `cancelled` or `not judged`), the fill, the exit and its reason, R (the move
from the fill to the exit over the fill's distance to the entry's stop, never a moved stop) and
cash (the price move × size × point value, in the trade's currency). A cancellation's why is
`not filled` (the market never reached the limit, or the order expired), `pulled` (you withdrew it)
or `run stopped`; the last is dated only at the run's stop, and the fill rate leaves it out.

**Replay runs are reported.** `report` is the only intake, and every report gives its `at`:
`filled` (at the limit unless you give `price`), `cancelled` (with its why), `open` (still held) or
`exited` (`kind="stop"` or `"target"`, at the level in force unless you give `price`, or
`kind="hand"`, the default, always with its `price`). No bars are read, and a position with no exit
reported reads open.

**Live runs are judged** from Yahoo's 1-minute bars, unadjusted (not rewritten for a later split, a change in a company's share count that moves its price, or a
dividend) and regular-session (the exchange's normal hours only). A future such as `GC` reads as
`GC=F`, and a stock or an ETF in CAD as its Toronto listing (`.TO`). A read fetches the completed
bars a trade lacks, only while it has not ended, and keeps them in bars.db. Yahoo keeps 1-minute
bars about 30 days, so a live trade not read for longer can become "not judged". Your reports
override the bars, and the judge continues from your latest one: a fill fixes the entry, for
example at the broker's price; `open` sets aside an exit the bars found before it (date it at or
after that exit: an exit in the same bar as the report stands, since a bar cannot say which came
first, and the read says so); and a cancellation or an exit ends the trade.

A live read shows `outcome` (your reports over the judge), `judged` (the bars' own reading, which
takes no fill, price or `open` from your reports) and `differs` (the fields where the two part):
the slippage, the gap between the price the bars give and the one you got. `move_order`,
`close_trade` and a correction of a move's `at` are refused once the bars put the exit before them;
if the bars ended the trade wrongly, the refusal names the report that fixes it (`open`, when the
broker still held the position, or `exited` with the broker's price). `stop_run`'s `needs_report`
maps a trade id to why its bars could not be read: report each one's fill, or its cancellation with
why `run stopped` dated at the stop, or read it again with `get_trade` once the cause passes.

A report is named by its state when you make it, and by its record when you fix it: `filled` becomes
`"fill"`, `cancelled` `"cancel"`, `open` `"open"` and `exited` `"close"`, so
`report(state="exited")` is withdrawn as `void_trade(report="close")`.

**Every report is checked against your record**, with no market data, and one that fails is
refused, saying which, with nothing written: a fill only on a pending limit, never worse than it and
not after the run's stop; `run stopped` only at the run's stop; an exit or an `open` report only on
a position (so a limit's fill is reported first), and an `open` report before the close; a hand
exit with its price; a stop or target exit only with one in force, never better than the stop or
worse than the target; and every report in order with the entry, the moves and the close.

**The judge's assumptions.** A fill or an exit is dated at the close of the bar it happened in. A
stop the price gapped through (a bar that opened beyond it) fills at the bar's open, a target at
the target price, and a bar that reaches both counts as the stop. A limit order never fills better
than its price, and in the bar it fills only the stop counts. A position that opens with its stop
already at or beyond its price (a stop moved past a pending limit, or a fill reported beyond the
stop) is stopped out at once, at that price. A hand close, unless the trade exited first, exits at the close of the last bar
opening before it, or at the entry price when no bar traded from the entry to the close; that bar
counts its stop and its target when it had finished by the close, and only its stop while still
forming, when the trade reads `closing`. A run stop cancels pending limits and closes nothing; a
limit the bars show filled first stays filled. An entry's own bar, and a reported fill's, is not
judged. Where bars are missing, the trade reads "not judged", naming the range, and nothing after
it is claimed.
