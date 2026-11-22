# trading-desk

A record of your trades under the strategies that allowed them. You write each strategy down
as a short Markdown file with a rules block. Every trade belongs to one strategy, at one
version of it, in one run. The desk stores your decisions about each trade, with your
reasons and screenshots, in an append-only SQLite log, and works out how each trade ended
whenever you read it, checks each trade against its strategy's rules when you enter it, and
reports performance per strategy, per version and per run.

**Status: 0.9.0, the MCP server and the Claude Code plugin — on GitHub only.** PyPI keeps
0.5.0 until 1.0, so install from the repository to use this version (see [Install](#install)).
This release stores the record, reads each trade's outcome (a replay trade's is what you
report; a live trade's is worked out from price bars, with your reports overriding it),
checks each trade against its strategy's rules when it is entered, reports statistics, and
serves all of it to an agent over MCP (see [Use it from an agent](#use-it-from-an-agent)). The
Python API below is what each tool calls, one to one.

> **Since 0.5 this is a new tool, not an upgrade of 0.4.** 0.4 was a portfolio book with a
> terminal UI. Both are gone, along with its commands (`desk`, `desk-migrate`, `desk-check`,
> `desk-mcp`), its `[mcp]` extra and its `hub.db` format. The desk does not read 0.4's data,
> and a 0.4 book is refused rather than misread.

## The model

| Thing | What it is |
|---|---|
| **Strategy** | A Markdown file you write: the idea, how you enter, where the target and the stop go, one `rules` block that the desk checks each trade against, and a checklist that you answer at each entry, which the desk records and shows apart, taking no part in whether the trade kept its rules. |
| **Version** | A strategy's text at one moment. Any change to the text makes a new version, except for spacing within a line, trailing spaces, line endings and extra blank lines. A version never changes once saved, so it tells you exactly what each of its trades was taken under. |
| **Run** | One version deployed over a period. Its mode is `live` (trading now) or `replay` (bar-by-bar replay of the past). It also carries free-text tags such as `paper`, `real` or `GC 15m`, which the desk stores and never interprets. |
| **Trade** | One entry in one run: ticker, side, a market or limit entry, stop, target, size, currency (CAD or USD), your reasons in your own words, and screenshots. |
| **Decision** | Anything you do to a trade after entering it: move the stop or target, close it by hand, report what the order or the position did, correct a mistake, or void it. Each carries a reason. |

Two principles hold everything together:

1. **Only decisions and bars are stored.** Your reports of what an order or a position did
   are decisions too. Price bars are stored as they were fetched and never changed. Exits,
   fills, statistics and whether a trade kept its rules are worked out from those two
   whenever they are read. Nothing derived is stored, so nothing can go stale. (The one
   exception: the rule results the desk showed you when you entered a trade, and the market
   values and checklist answers you gave, are kept with it, as a record of what you saw.) A correction is a new
   entry that is read in place of the one it corrects. Nothing is ever edited or deleted.
2. **One timing rule.** An entry, a move, a close and a report each have an `at`, in UTC:
   the moment a decision takes effect, or the moment whose state a report gives. In a live
   run an entry's, a move's and a close's `at` defaults to now. A report always gives its
   own, live or replay, and none is dated after now. In a replay run you must give every
   `at`, as the close of the bar you acted on. A decision takes effect from the first bar
   that opens at or after its `at`. A time without a timezone is refused, never guessed.
   Corrections and voids are record-keeping, not market decisions: they are dated when you
   make them.

## A strategy

````markdown
---
name: Pullback in an uptrend
---

## Idea
Buy a shallow pullback in a stock that is already rising, with the stop under the pullback low.

## Rules

```rules
timeframes = ["1h", "15m"]
min_reward_risk = 2
max_risk_per_trade = { USD = 100, CAD = 130 }
stop_required = true
target_required = true
sides = ["long"]
max_open_trades = 2
cooldown_after_loss = { minutes = 60, same_ticker = true }
daily_loss_limit = { R = 2 }

[rsi]
timeframe = "15m"
long = { at_most = 40 }
```

## Checklist

- The pullback came on lighter volume than the move up.
````

A template ships with the package (`desk/strategy.example.md`). The block is TOML. Every
value is read when the strategy is saved: an unknown rule, an unreadable value, a value in the
wrong unit, or a duplicate is refused, naming the rule and the fix. No rule is ever silently
dropped, and a block in the old line format (`timeframes: 1h`) is refused with the TOML form.

When you enter a trade, the desk checks it against the rules of the run's version, as of the
trade's `at`, and shows you each result: kept, breached, or unknown, naming what is missing.
A breach never refuses a trade, and nothing is read as kept for want of data. The results
shown are kept with the trade. `get_trade` also works them out again now, as `checks_now`,
and lists the rules whose answer has changed in `checks_differ`.

| Rule | Meaning |
|---|---|
| `timeframes` | Required. The bar sizes the strategy trades on, e.g. `["15m", "1h"]`. A live run is judged on Yahoo's 1-minute bars, whatever these are; they are for the rules and `trade_bars`. Yahoo keeps 1-minute bars about 30 days. |
| `min_reward_risk` | The smallest reward-to-risk you will take: the distance from the entry to the target divided by the distance from the entry to the stop. |
| `max_risk_per_trade` | The most you will lose if the stop is hit, per currency, e.g. `{ USD = 100, CAD = 130 }`. Risk is size × the stop's distance × the point value. The result names the largest size that passes. |
| `stop_required`, `target_required` | `true` or `false`. |
| `sides`, `instruments` | The sides (`"long"`, `"short"`) and instruments (`"stock"`, `"etf"`, `"future"`) you will trade. |
| `max_open_trades` | How many trades this run may hold at once. A pending limit order counts. |
| `max_trades_per_day` | How many trades the run may enter in a day (the system timezone's midnight to midnight). |
| `max_consecutive_losses` | How many losses in a row the run may take within the day before the next trade. |
| `daily_loss_limit` | The most R the run may lose in a day, e.g. `{ R = 2 }`. A day at exactly that loss is kept. One R is the amount a trade risks between its entry and its stop. |
| `cooldown_after_loss` | How long to wait after a loss, `{ minutes = 60, same_ticker = true }`; `same_ticker` is optional. |
| `trading_hours` | `{ start = "09:45", end = "15:30", days = ["mon", "tue"] }`, in the system timezone. It may not cross midnight; `days` is optional. |
| `[stop_atr]` | The stop's distance in ATRs (`length`, default 14), e.g. `at_least = 1.5`. |
| `[rsi]`, `[macd]` | An indicator's value on a `timeframe`, with `at_least`, `at_most` or `between`, at the top or under `long` and `short`. `macd` takes `fast`, `slow`, `signal` and an `output` (`histogram`, `line` or `signal`). |
| `[ema]`, `[sma]` | The close above or below a moving average (`length`, `timeframe`, `long = "above"`). Write `[[ema]]` more than once for several. |
| `[facts]` | Named values you supply at entry, each with a comparison, e.g. `rvol = { at_least = 2 }`. |

The checklist is the top-level bullets under `## Checklist`. Each is answered at entry with
`pass` or `fail` and a reason. The answers are shown apart from the rule results and take no
part in whether a trade kept its rules.

The desk computes an indicator from price bars it fetches (a live run) or from the `market`
dict you give `trade`, which holds a number per key the rules read, such as `rsi_14_15m`,
`macd_hist_12_26_9_15m`, `close_1h`, `ema_200_1h` or a fact's own name. Your value is used in
place of the desk's, and both are kept, so the gap from your chart shows. In a replay run give
every key; in a live one, an indicator the bars cannot give reads unknown and names the key to
supply (a future's Yahoo feed is delayed, so supply its keys).

## Install

```bash
uv tool install 'trading-desk @ git+https://github.com/IngTian/trading-desk'
```

```bash
pip install 'trading-desk @ git+https://github.com/IngTian/trading-desk'
```

The `uv tool` line puts `trading-desk` on the PATH, which the Claude Code plugin needs.

That is this version. `pip install trading-desk` from PyPI gives 0.5.0 until 1.0 is released.

Install it into the Python environment you will import it from: the desk is used
from Python, and it has two commands, `trading-desk mcp` and `trading-desk integrity`. Python
3.11 or later, built with SQLite 3.38 or later, on macOS or Linux. The package requires the `mcp` SDK and
yfinance (with pandas and numpy), which live runs read their bars through.

## Use it from an agent

In Claude Code, install the plugin (it needs `trading-desk` on your PATH, see [Install](#install)):

```
/plugin marketplace add IngTian/trading-desk
/plugin install trading-desk@trading-desk
```

To register the server by hand, or for another MCP client, run
`claude mcp add trading-desk -- trading-desk mcp`.

The 16 tools are `save_strategy`, `get_strategy`, `list_strategies`, `start_run`, `stop_run`,
`list_runs`, `trade`, `move_order`, `close_trade`, `report`, `correct_trade`, `void_trade`,
`get_trade`, `list_trades`, `trade_bars` and `stats`. Each is the Python function of the same
name. Replies are JSON with UTC times. A refusal comes back as a tool error that says what to
change, and a refused call writes nothing. The first start creates `desk.db`.

Claude Code asks before each tool call unless `mcp__plugin_trading-desk_desk__*` is allowed in
your settings. A pasted image is passed as a file path or a link; the desk copies the file when
the trade is recorded.

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

A live run is the same without the reports: start it with `mode="live"`, leave out `at` to
mean now, and each read judges the trade from Yahoo's bars. Report only what the bars got
wrong, such as the broker's fill.

| Call | Does |
|---|---|
| `save_strategy`, `get_strategy`, `list_strategies` | Save a strategy (a new version whenever its text changes), read one, list them. Saving over an existing strategy needs `if_current`, the `current_hash` from your last read, so two sessions can never overwrite each other's edits. |
| `start_run`, `stop_run`, `list_runs` | Deploy a version, stop it, list runs. A run trades in one currency, `currency="CAD"` or `"USD"`, which every trade on it uses. `stop_run` returns `cancelled` (pending orders it cancelled), `awaiting_bars` (read again later) and `needs_report` (trade id to why its bars could not be read: report them). |
| `trade` | Enter a trade, with your reasons and screenshots. A screenshot is a local file, copied into the store under its content hash, or an `https` link. The desk checks it against the run's version's rules and keeps the results shown (`checks_shown`, `checklist`). `market` gives the values the rules read, `checklist` the answers (`[{"item": …, "verdict": "pass", "reason": …}]`). `check_only=True` returns a `CheckView` and writes nothing. |
| `move_order`, `close_trade` | Move the stop or target, or close by hand: the decision to get out, which the bars price on a live run. On a replay run a position's exit is reported instead, with its price (`report(state="exited", kind="hand", price=…)`). Each needs a reason. |
| `report` | Report the trade's state as of `at`, which every report gives: `filled`, `cancelled` (with `why`), `open` or `exited` (with `kind`, and with `price` for a hand exit). The only intake on a replay run; on a live run it overrides the bars. |
| `correct_trade`, `void_trade` | Correct a mistake (a new entry, read in place of the one it corrects): the entry, a move (`fields={"move": 0, …}`) or a report (`fields={"report": "fill", "price": …}`). Void a trade: it stays in the log, and statistics and the checks of other trades leave it out. Or withdraw one report (`void_trade(report="fill")`), which is then read as absent. |
| `get_trade`, `list_trades` | Read trades back, with their decisions folded together and their `outcome`, `judged` and `differs`. `get_trade` also gives `checks_now`, the rule results worked out again now, and `checks_differ`, the rules whose answer changed since entry. `list_trades(open=True)` gives the trades still pending or open, `open=False` the rest. |
| `trade_bars` | A live trade's bars at one of its run's timeframes (the finest by default): from `before` bars ahead of the entry through `after` bars past the exit, or through the latest bar while it is open, with the ranges no fetch covered and why. |
| `stats` | Counts, rates and averages for one scope: `strategy=…`, `version=…` with a strategy, or `run=…`. Narrow it with `mode`, `tags`, or `conforming_only=True` (the trades whose results now are all kept). It returns a `desk.stats.Stats` with the trade ids in each bucket, the win rate, average and total R, P&L per currency, and the limit orders placed, filled and cancelled. Never a verdict or a ranking. |

A refusal raises `api.Refused`, whose message says what was wrong and how to fix it. A
refused call writes nothing. On a live run, `move_order`, `close_trade` and a correction of a
move's `at` are refused once the bars put the exit before them, since there is nothing left
to act on. If the bars ended the trade wrongly, the refusal names the report that fixes it:
`open`, when the broker still held the position, or `exited` with the broker's price.

Tracking a trade (its largest move for you or against you while it was open, the drift after
the exit, anything per strategy) is your own code: it reads `get_trade` and `trade_bars` each
time it runs, so a correction or a void shows up on its next run.

## How a trade's outcome is read

Both modes share one state model. An order (how the trade enters) is `pending`, `filled` or
`cancelled`: a market order fills as you enter it, and a limit order (one to buy at its price
or lower, or to sell at its price or higher) is pending until it fills or is cancelled. A
position is `open` until it exits at its stop, at its target or by hand, at a price and a
time. Every read carries the trade's `outcome`: its status, the fill, the exit and its
reason, R, and cash (the price move × size × point value, in the trade's currency).

### Replay runs are reported

One call, `report`, is the only intake. A report states the trade's state as of its `at`,
which every report gives:

| `state` | Says |
|---|---|
| `filled` | The limit order filled, at the limit unless you give `price`. |
| `cancelled` | It never will, and `why`: `not filled` (the market never reached the limit, or the order expired), `pulled` (you withdrew it) or `run stopped` (the run's stop cancelled it, dated at the stop). |
| `open` | The position was still held at `at`. |
| `exited` | The position closed: `kind="stop"` or `kind="target"`, at the level in force unless you give `price`, or `kind="hand"` (the default), always with its `price`. |

No bars are read, and a position with no exit reported reads open.

### Live runs are judged

Live runs are judged from Yahoo's 1-minute price bars, whatever the strategy's timeframes. The bars are unadjusted (not rewritten for a later
split or dividend) and regular-session (the exchange's normal hours, without pre- or
after-market trading). A future is read as Yahoo's rolling front month, e.g. `GC=F` for `GC`:
the contract nearest its expiry, so the series switches to the next contract at a roll. A
stock or an ETF in CAD is read as its Toronto listing (`.TO`). Reading a trade fetches the
completed bars it lacks, only while the trade has not ended, and keeps them in `bars.db`.
Yahoo keeps 1m bars
about 30 days, so a live trade not read for longer than that can become "not judged".

Your reports override the bars. The judge continues from your latest report:

- A fill fixes the entry, for example at the broker's actual price.
- `open` says the position was still held then, which sets aside an exit the bars found
  before it. Date it at or after that exit: an exit in the same bar as the report stands,
  since a bar cannot say which came first, and the read says so.
- A cancellation or an exit ends the trade.

A live read shows `outcome` (your reports over the judge), `judged` (the bars' own reading,
which takes no fill, price or `open` state from your reports) and `differs` (the fields where
the two part). The
difference is slippage (the gap between the price the bars give and the one you got), and
worth keeping.

### Every report is checked against your record before it is kept

The checks need no market data:

- A fill only on a pending limit order, and never worse than the limit.
- A cancellation says why: `not filled` or `pulled` (a run stop's reads `run stopped`), so the
  limit orders that never filled can be counted apart from the ones you pulled.
- An exit or an `open` report only on a position, so a limit order's fill is reported first,
  and an `open` report comes before the close.
- A stop exit is never better than the stop, and a target exit never worse than the target.
- Every report is in order with the entry, the moves and the close, and none is dated after
  now.

A report that fails one is refused, saying which, and nothing is written.

### The judge's assumptions

- A fill or an exit is dated at the close of the bar it happened in.
- A stop the price gapped through (a bar that opened beyond it) fills at the bar's open, and a
  target fills at the target price.
- A bar that reaches both the stop and the target counts as the stop.
- A limit order never fills better than its price, and in the bar it fills only the stop
  counts.
- A position that opens with its stop already at or beyond its price (a stop moved past a
  pending limit, or a fill reported beyond the stop) is stopped out at once, at that price:
  no gain and no loss.
- A hand close exits at the close of the last bar opening before it, or at the entry price
  when no bar traded between the entry and the close, unless the trade exited first. That bar
  counts its stop and its target when it had finished by the close, and only its stop when
  it was still forming.
- A run stop cancels pending limit orders and closes nothing.
- An entry's own bar (market or limit), and a reported fill's, is not judged.

When bars are missing, the trade reads "not judged": the missing range is named, and nothing
after it is claimed.

### Limits

- A split or another corporate action (a merger, a special dividend) during an open live
  trade is not adjusted for, because Yahoo's bars are unadjusted.
- A future's 1d or 1w indicator is not computed: supply it in `market`.

## Where your data lives

Everything lives under one folder: `~/Documents/trading-desk/` by default, or
`$TRADING_DESK_HOME`.

```
desk.db        strategies, runs, trades and every decision, reports included (append-only)
bars.db        price bars for live runs, and the range each fetch covered (insert-only)
attachments/   screenshots, by content hash
```

0.9 does not read a 0.6 desk.db (or a 0.5 one): there is no migration.

`$TRADING_DESK_DB` moves only the database: it names one file in place of `desk.db`.
Screenshots and `bars.db` still go to the root, so to keep a session apart from your real
data, set `$TRADING_DESK_HOME` instead.
Both databases use a rollback journal, never WAL, because `~/Documents` is often synced by
iCloud, and a database synced apart from its `-wal` file can be corrupted. Write from one
machine at a time.

`trading-desk integrity` checks the log for shapes it should never hold: a trade with no
entry, a decision dated before its trade, a move at or after the close, a report the record
does not allow, and more. It prints each one, and exits 1 if it found any, or if there is no
desk.db to check; 0 if the log is clean. It runs SQLite's `quick_check` on the file first,
and reports a damaged file, or a check SQLite cannot run, as a violation, never a traceback.
`quick_check` skips some of what `integrity_check` does, so for the slower, fuller check, run
`sqlite3 -readonly desk.db 'PRAGMA integrity_check'`.

## Roadmap

| Release | Adds |
|---|---|
| 0.5 | The record. |
| 0.6 | Reported states, bars and the judge. |
| 0.7 | Rule checks when you enter a trade (the results you were shown are kept with it), and statistics per run, version and strategy. |
| 0.8 | The MCP server and a Claude Code plugin, so an agent records your trades and decisions for you |
| 0.9 | Live runs judged on 1-minute bars, a trade must name its instrument, wrong-side stops and targets refused, and the agent told more (this release). |
| 1.0 | The release, once all of the above has landed. |

## Development

```bash
uv sync
uv run pytest -q
uv run ruff check .
uv run python tools/no_real_figures.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md). MIT licensed.
