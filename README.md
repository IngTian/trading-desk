# trading-desk

An MCP server that keeps your trading record for an agent to drive. MCP, the Model Context Protocol,
is how an agent such as Claude Code calls a program's functions. Each trade is filed under the
strategy, version and run that allowed it, with your reasons and screenshots, and checked against
the version's rules before entry; a breach never refuses it. Its outcome comes from your reports
when you replay history, or from price bars when you trade live, the reports overriding the bars. It
is bookkeeping: stats per run, version and strategy, as counts, rates and averages, never a verdict.

## Status

**0.10.0, on PyPI, Beta:** the tools and the data format may change before 1.0, and each change is
listed under [Versions](https://github.com/IngTian/trading-desk#versions).

- It reads a desk.db from 0.7 or later. One from 0.5 or 0.6 is refused untouched: move it aside and
  restart the MCP server, which creates a new one, since there is no migration.
- The one trap in upgrading from 0.8: a config.toml that sets `db` is refused, naming the step to
  take.
- 0.4 was a different tool, a portfolio book with a terminal UI. Its book (hub.db) is never opened
  unless you point `TRADING_DESK_DB` at it, and then it is refused untouched.

## Install

```bash
uv tool install trading-desk
```

This puts the `trading-desk` command in uv's folder for tool commands (`uv tool dir --bin`), on your
PATH, as the plugin needs; if uv warns that the folder is not on your PATH, run
`uv tool update-shell`. For the Python API, install it into the environment you import it from:

```bash
pip install trading-desk
```

It needs Python 3.11 or later, built with SQLite 3.38 or later (for its STRICT tables and its JSON
functions), on macOS or Linux. It depends on the `mcp` SDK, and on yfinance (with pandas and numpy),
through which live runs read Yahoo Finance's price bars (each period's open, high, low, close and
volume). yfinance's own README says it is not affiliated with Yahoo, and that Yahoo's terms of use
govern the data, which is for personal use. To upgrade, run `uv tool upgrade trading-desk`. An
install older than 0.8 has no `mcp` command, so the plugin cannot start until you upgrade. An
install made from the repository, as 0.6 to 0.9 were, moves to PyPI with
`uv tool install --force trading-desk`.

## Use it from Claude Code

```
/plugin marketplace add IngTian/trading-desk
/plugin install trading-desk@trading-desk
```

- The plugin runs `trading-desk mcp` from your PATH. `/mcp` shows the server; a failed one usually
  means `trading-desk` is not on the PATH Claude Code sees, or predates 0.8 (`uv tool list` shows
  its version).
- Without this rule in your settings, Claude Code asks before each call:
  `{"permissions": {"allow": ["mcp__plugin_trading-desk_desk__*"]}}`
- To register it by hand instead, run `claude mcp add -s user trading-desk -- trading-desk mcp`
  (without `-s user`, it serves this project only). Its tools are then `mcp__trading-desk__…`, and
  their allow rule is `mcp__trading-desk__*`. Any other MCP client runs `trading-desk mcp` over
  stdio (its standard input and output).
- The first start creates desk.db under `~/Documents/trading-desk/` (or `$TRADING_DESK_HOME`, or the
  file `$TRADING_DESK_DB` names), unless it exists, or its `.desk.db.icloud` stub does: the stub
  that iCloud, Apple's file sync, leaves on macOS 13 and earlier for a file it has evicted (moved
  off the Mac, to the cloud only).

## What the agent does for you

You tell the agent what you did and why, and it calls the desk's tools. A session:

1. **Save a strategy**, a Markdown file (see
   [A strategy](https://github.com/IngTian/trading-desk#a-strategy)). Any change to its text beyond
   spacing makes a new version, and overwriting needs `if_current`, the `current_hash` from your
   last read, so two sessions never overwrite each other's edits.
2. **Start a run**: `live` or `replay`, one currency (CAD or USD) for all its trades, and free tags.
3. **Check a trade, then enter it**: `trade` with `check_only`, which records nothing, then without.
   A trade is long (profiting if the price rises) or short; it enters at market (filled at once) or
   with a limit order (filled only at its price or better); its stop is the price that exits a
   losing position, and its target the price that takes the profit.
   - It needs `instrument` (`stock`, `etf` or `future`) unless the ticker names a continuous future
     (`GC1!`), and a future's `point_value`, the cash a one-point move is worth on one contract. An
     ETF is an exchange-traded fund. A future is a contract to buy or sell at a set price on a later
     date; the desk reads Yahoo's rolling front month, the contract nearest expiry, which switches
     to the next at a roll.
   - Also give the `market` values its rules read, which `get_strategy`'s `market_keys` names. A key
     left out reads unknown, or on a live run is computed where the desk can.
   - Answer every checklist item; an unanswered one is recorded "not answered". Neither of these two
     is refused.
   - Over MCP, `reasons` is still required: pass an empty string with `check_only`.
   - Screenshots are local files, copied into the desk under a hash of their bytes, or https links.
4. **After entry**: `move_order`, `close_trade` and `report`.
5. **Fix a mistake**: `correct_trade` and `void_trade`. A trade's currency cannot be corrected.
6. **Read back**: `get_trade`, with `checks_now` and `checks_differ`; `list_trades`; and
   `trade_bars`, at one of its run's timeframes (the finest by default) or at `1m`.
7. **Stop the run.** `stop_run` sorts its unfilled limits into `cancelled` (by the stop),
   `awaiting_bars` (bars not yet at the stop: read again later) and `needs_report` (bars that could
   not be read: report each).
8. **Read the stats** of a run, a version or a strategy.

| Job | Tool | Does |
|---|---|---|
| Strategies | `save_strategy` | Saves a strategy's Markdown: version 1 for a new id, a new version whenever its text changes. |
| | `get_strategy` | One version, the latest by default: text, rules, timeframes (finest first), checklist, `versions`, `market_keys`, `current_hash`. |
| | `list_strategies` | Every strategy, at its latest version. |
| Runs | `start_run` | Deploys a version as run `<strategy>-r<N>`, live or replay, in one currency, with tags. |
| | `stop_run` | Stops a run: no entry at or after its `at`, pending limits cancelled, open trades left open. It cannot be undone. |
| | `list_runs` | Every run, or one strategy's, with `started` and `stopped`. |
| Trades | `trade` | Enters a trade, checked against its version's rules as of `at`; with `check_only`, a preview. |
| | `move_order` | Moves the stop, the target or both, in force from the first bar opening at or after `at`. |
| | `close_trade` | Closes by hand with no price: pulls a pending limit, or prices a live position from the bars. A replay position is refused: report its exit with its price instead, `report(state="exited", kind="hand", price=…)`. |
| | `report` | States the trade's state as of `at`: `filled`, `cancelled`, `open` or `exited`. |
| | `correct_trade` | Corrects the entry, one move (`fields={"move": i, …}`, `i` its place in `get_trade`'s moves) or one report (`fields={"report": "fill", "price": …}`, or `"cancel"`, `"open"`, `"close"`), read in place of the original, which stays in the log. |
| | `void_trade` | Takes a trade out of the stats and the other trades' checks, or withdraws one report. It cannot be undone. |
| Reading | `get_trade` | One trade, read now: its decisions, `outcome`, `judged`, `differs`, `checks_shown`, `checks_now`, `checks_differ`. |
| | `list_trades` | Every trade, voids included, narrowed by run, by strategy and to the open ones or the rest. |
| | `trade_bars` | A live trade's bars on a timeframe of its run or 1m: from `before` bars ahead of the entry's bar through `after` bars past its exit or cancellation (both 0 if left out), or through the latest bar while it has not ended, with the ranges missing. |
| | `stats` | Counts, rates and averages for one scope. |

Replies are JSON with UTC times. A refusal is a tool error that says what to change, and a refused
call writes nothing. Every write returns the updated strategy, run or trade.

## What the agent is told

The server sends standing
[instructions](https://github.com/IngTian/trading-desk/blob/main/desk/instructions.md) at the start
of every session. In short:

| When | The agent |
|---|---|
| Always | Passes your reasons exactly as you wrote them, never its own, and asks when you gave none. States no figure the desk did not return. Gives times in ISO 8601 with an offset: on a replay run, the close of the bar you acted on, in UTC; on a live run, whenever you say when you acted; and in every report. Defines each finance term at first use, and uses your pronouns, they/them until it knows them. After a refusal, fixes the form of the call itself, but asks you before it changes a time, a price, a size, a reason or a report, or voids. |
| Entering a trade | Supplies `market`: on a replay run every key, read off your chart; on a live run your facts, a future's indicator keys (Yahoo's futures bars lag), and any value you trust over the desk's. Answers every checklist item with a verdict and a one-line reason. Passes screenshots as full file paths or links, and asks you for a path when it has none. |
| After entry | On a replay run, reports each limit's fill or cancellation and each exit. Reports an expired day order (one that lapses at the session's close) as cancelled, why `not filled`. On a live run, reports a stop-out (an exit at the stop) at its real time, with the broker's price, and reports `open` a position the bars ended that your broker still holds. Before an entry on a live run, settles that run's futures trades, for the rules that read other trades, giving such an `open` report and the entry the same `at`. Reads each open live trade at least weekly, since Yahoo keeps 1-minute bars about 30 days. Stops a run, or voids a trade, only on your word. Fixes a mistake with `correct_trade` or `void_trade`, never with a new trade on top. |

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

## A strategy

This is the template that ships, `desk/strategy.example.md`, without its extra prose:

````markdown
---
name: Pullback in an uptrend
---

## Idea

Buy a shallow pullback in a stock that is already rising, with the stop under the pullback low.

```rules
timeframes = ["15m", "1h"]
min_reward_risk = 2
max_risk_per_trade = { USD = 100, CAD = 130 }
stop_required = true
target_required = true
sides = ["long"]
max_open_trades = 2
cooldown_after_loss = { minutes = 60, same_ticker = true }
daily_loss_limit = { R = 2 }

[ema]
length = 50
timeframe = "1h"
long = "above"
```

## Checklist

- The pullback came on lighter volume than the move up.
- No tier-1 news inside the next hour.
````

The block is TOML (a plain-text settings format), read when the strategy is saved: an unknown rule,
an unreadable value, a value in the wrong unit or a duplicate is refused, naming the rule and the
fix. No rule is silently dropped.

| Rule | Meaning |
|---|---|
| `timeframes` | Required: the bar sizes the rules read, e.g. `["15m", "1h"]`, and what `trade_bars` serves by default (the finest). A live run is judged on Yahoo's 1-minute bars, whatever these are. |
| `min_reward_risk` | The smallest reward-to-risk you will take: the distance from the entry to the target over the distance from the entry to the stop. That second distance, the risk, is 1R. |
| `max_risk_per_trade` | The most you will lose if the stop is hit, per currency, e.g. `{ USD = 100, CAD = 130 }`: size × the stop's distance × the point value. The result names the largest size that passes. |
| `stop_required`, `target_required` | `true` or `false`. |
| `sides`, `instruments` | The sides (`"long"`, `"short"`) and instruments (`"stock"`, `"etf"`, `"future"`) you will trade. |
| `max_open_trades` | How many trades the run may hold at once. A pending limit order counts. |
| `max_trades_per_day` | How many trades the run may enter in a day (the system timezone's midnight to midnight). |
| `max_consecutive_losses` | How many losing exits in a row, within the day, the run may take before the next trade. A breakeven, an exit with no gain or loss, is skipped. |
| `daily_loss_limit` | The most R the run may lose in a day, e.g. `{ R = 2 }`. A day at exactly that loss is kept. |
| `cooldown_after_loss` | How long to wait after a loss, `{ minutes = 60, same_ticker = true }`; `same_ticker` is optional. |
| `trading_hours` | `{ start = "09:45", end = "15:30", days = ["mon", "tue"] }`, in the system timezone, from `start` up to `end`. It may not cross midnight; `days` is optional. |
| `[stop_atr]` | The stop's distance in ATRs (average true range: a bar's typical high-to-low size), `length` 14 unless given, e.g. `at_least = 1.5`. |
| `[rsi]`, `[macd]` | RSI (relative strength index: a 0 to 100 reading of recent gains against recent losses; `length` 14) or MACD (moving average convergence divergence: the gap between a fast and a slow moving average; `fast` 12, `slow` 26, `signal` 9, and an `output` of `histogram`, `line` or `signal`) on a `timeframe` (the finest unless given), with `at_least`, `at_most` or `between`, at the top or under `long` and `short`. |
| `[ema]`, `[sma]` | The close above or below an EMA or an SMA (exponential or simple moving average): a `length`, a `timeframe` (the finest unless given), and `long` or `short` set to `"above"` or `"below"`. Write `[[ema]]` more than once for several. |
| `[facts]` | Named values you supply at entry, each with a comparison, e.g. `rvol = { at_least = 2 }` for a relative volume (the volume against its recent average) read off your chart. The desk never computes a fact. |

At entry the desk checks the trade against the run's version's rules, as of its `at`, and shows
each result: kept, breached, or unknown, naming what is missing. A breach never refuses a trade,
and nothing reads kept for want of data. The results shown are kept with the trade; `get_trade`
works them out again as `checks_now`, and lists in `checks_differ` the rules whose answer changed:
`[]` when none did, null where nothing was compared (`list_trades`, a write's reply, a void). The
checklist is the top-level bullets under `## Checklist`, each answered `pass` or `fail` with a
reason; the answers are shown apart and take no part in whether a trade kept its rules.

**`market_keys`:** `get_strategy` names every key the rules read, such as `rsi_14_15m`,
`macd_hist_12_26_9_15m`, `ema_50_1h`, `close_1h` or a fact's own name, and `trade`'s `market` gives
a number per key. In a replay run the desk computes nothing, so give every key. In a live run it
computes an indicator from Yahoo's completed bars before the trade's `at`; your value is used in its
place, and both are kept, so the gap from your chart shows. A key that neither you nor the desk
gives reads unknown for good, since `market` is not corrected.

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

## Stats

`stats` takes one scope (`strategy`, `version` with a strategy, or `run`), narrowed by `mode`,
`tags` (the runs holding every tag given) and `conforming_only` (the trades whose rule results now
are all kept). Voided trades never count. It returns the trade ids behind each figure: `closed`,
the exits with R, split into `wins`, `losses` and `breakevens`; `win_rate` (wins over closed), and
`average_r`, `total_r`, `average_win`, `average_loss` and `average_planned_rr` (the reward-to-risk
planned at entry), over closed; `closed_no_r`, the exits without R; `pnl`, the profit or loss per
currency, gross of fees and never converted; `open`, `pending`, `cancelled` (by why) and
`not_judged` (each with its missing range and the report that settles it); `limits`; `left_out`,
what `conforming_only` left out; and `rests_on`, the trades by mode, tag set and version, and the
live exits by reported or judged. A figure with nothing under it is null. Over MCP, every list of
trade ids comes as `{count, ids}`, and `not_judged` as `{count, trades}`.

The fill rate, `limits.rate`, is filled ÷ (placed − still pending − not judged without a fill −
cancelled by the run's stop). The stats are never a verdict or a ranking.

## Where your data lives

Everything lives under one folder, the data root: `~/Documents/trading-desk/` by default, or
`$TRADING_DESK_HOME`.

```
desk.db        strategies, runs, trades and every decision, reports included (append-only)
bars.db        price bars for live runs, and the range each fetch covered (insert-only)
attachments/   screenshots, named by the hash of their bytes
```

Append-only and insert-only mean rows are added, never changed or deleted. `$TRADING_DESK_DB` beats
`$TRADING_DESK_HOME`, for the database only: it names a file in place of desk.db, and the rest stays
under the root, so to keep a session apart from your real data, set `$TRADING_DESK_HOME`. No
config.toml is read, and every opener of desk.db refuses one that sets `db` (unless
`$TRADING_DESK_DB`, or from Python a path, names the database).

Both databases use a rollback journal, never WAL (a write-ahead log, a second file beside the
database), because `~/Documents` is often synced by iCloud, and a database synced apart from its
log can be corrupted. Write from one machine at a time. Where iCloud has evicted desk.db or
bars.db, the desk never creates an empty one over either, and names the folder to open in Finder.
Without desk.db every call is refused; without bars.db a live trade reads `not judged`, an entry is
still recorded with its indicator results unknown, and `trade_bars` fails. That guard catches the `.icloud` stub that macOS 13 and earlier leave for an evicted file;
on macOS 14 and later an evicted desk.db keeps its name, and is downloaded when it is opened.

`trading-desk integrity` checks the log for shapes it should never hold: a trade with no entry, a
decision dated before its trade, a move at or after the close, a report the record does not allow,
and more, after SQLite's `quick_check` of the file (for the fuller check, run
`sqlite3 -readonly desk.db 'PRAGMA integrity_check'`). It prints each violation, and exits 1 if it
found any, or when there is no desk.db, the file is not a desk.db this version reads, config.toml
sets `db`, or desk.db is in iCloud but not on this Mac; otherwise 0.

## Use it from Python

```python
from datetime import UTC, datetime

from desk import api, config, store

store.create()                  # once: makes desk.db under the data root
con = store.open()

text = config.example_strategy().read_text()      # or your own strategy's Markdown
api.save_strategy(con, "pullback", text)

def utc(day, hour, minute=0):   # a moment in the replayed history
    return datetime(2026, 3, day, hour, minute, tzinfo=UTC)

run = api.start_run(con, "pullback", mode="replay", currency="USD", tags=["paper"], at=utc(2, 14))
t = api.trade(con, run.id, ticker="ACME", side="long", entry_type="limit",
              entry=100.0, stop=98.5, target=103.0, size=10, currency="USD",
              instrument="stock",
              reasons="Held the rising trend line on the hourly; a limit at the dip's low.",
              screenshots=["https://www.tradingview.com/x/AbCd1234/"], at=utc(2, 14))
api.report(con, t.id, state="filled", at=utc(2, 15, 15),
           reason="The 15:00 bar traded down to it.")
t = api.report(con, t.id, state="exited", kind="target", at=utc(3, 16, 30),
               reason="The target filled.")
t.outcome   # status "exited", fill 100.0, exit 103.0 at its target, r 2.0, cash 30.0
```

The functions are the tools under
[What the agent does for you](https://github.com/IngTian/trading-desk#what-the-agent-does-for-you),
one for one, with the same arguments after the connection. Only Python has: `at` as an aware
datetime or an ISO 8601 string with its offset; `now`, which tests inject, a keyword of every
function but `get_strategy`, `list_strategies` and `list_runs`; and the frozen dataclasses
returned, `StrategyView`, `RunView`, `api.StoppedRun` (from `stop_run`), `TradeView`, `CheckView`
(from `trade` with `check_only=True`), `TradeBars` and `desk.stats.Stats`, whose id lists are plain
tuples, without MCP's counts. The API may change before 1.0. A refusal raises `api.Refused`, whose
message says what was wrong and how to fix it, and a refused call writes nothing.

Tracking a trade (its largest move for you or against you while it was open, the drift after the
exit, anything per strategy) is your own code: it reads `get_trade` and `trade_bars` each time it
runs, so a correction or a void shows up on its next run.

## Limits

- A split or another corporate action,
  such as a merger or a special dividend, during an open live trade is not adjusted for, because
  Yahoo's bars are unadjusted.
- A future's 1d or 1w indicator, and one on any timeframe Yahoo lacks (it has 1m, 2m, 5m, 15m, 30m,
  1h, 90m, 1d and 1w), is not computed: supply it in `market`.
- `trade_bars` at 1m returns about 45,000 characters per trading day of a stock, and about three
  times that for a future. Claude Code saves a reply over 50,000 characters to a file, so past about
  a day the agent reads it from there; a trade open for weeks can return a megabyte or more.

What 0.10 does not do: place orders, or send alerts; keep account balances or account-level P&L;
convert currencies; take more than one exit per trade; expire orders (report an expired order
cancelled, with why `not filled`); move a pending limit's price (cancel it and enter again); remove
a stop or a target by correction (void the trade and enter again); trade anything but stocks, ETFs
and futures, a future being Yahoo's rolling front month; look inside a bar at finer bars; or judge
replay runs from bars.

## Versions

| Version | Where it shipped, and what it holds |
|---|---|
| 0.4.0 | On PyPI: the old portfolio book, a different tool. |
| 0.5 | On PyPI: the record. |
| 0.6 to 0.9 | Development versions, installed from the repository and never uploaded to PyPI; they can no longer be installed. 0.6: reported states, bars and the judge. 0.7: rule checks at entry, and stats per run, version and strategy. 0.8: the MCP server and the Claude Code plugin. 0.9: live runs judged on 1-minute bars, `instrument` required, wrong-side stops and targets refused, and the agent told more. |
| 0.10 | On PyPI: this README, `checks_differ` null where it was not worked out, `trade_bars` serving 1m, and Python's openers guarded. |
| 1.0 | When the interface settles. |

## Development

You need [uv](https://docs.astral.sh/uv/). The tests never reach the network.

```bash
uv sync
uv run pytest -q
uv run ruff check .
uv run python tools/no_real_figures.py
```

See [CONTRIBUTING.md](https://github.com/IngTian/trading-desk/blob/main/CONTRIBUTING.md). MIT
licensed.
