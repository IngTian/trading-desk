# trading-desk

A record of your trades under the strategies that allowed them. You write each strategy down
as a short Markdown file with a rules block. Every trade belongs to one strategy, at one
version of it, in one run. The desk stores your decisions about each trade, with your
reasons and screenshots, in an append-only SQLite log. From that log it will work out how
each trade ended and report performance per strategy, per version and per run.

**Status: 0.5.0, the record.** This release stores strategies, runs, trades and every
later decision. It does not yet judge exits from price bars, check a trade against its
rules, report statistics, or serve the agent interface. Those come in the releases listed
under [Roadmap](#roadmap). The Python API below is what the agent interface will wrap, one
call per tool.

> **0.5 is a new tool, not an upgrade of 0.4.** 0.4 was a portfolio book with a terminal
> UI. Both are gone, along with its commands (`desk`, `desk-migrate`, `desk-check`,
> `desk-mcp`), its `[mcp]` extra and its `hub.db` format. 0.5 does not
> read 0.4's data, and a 0.4 book is refused rather than misread.

## The model

| Thing | What it is |
|---|---|
| **Strategy** | A Markdown file you write: the idea, how you enter, where the target and the stop go, and one `rules` block the desk reads (a later release checks each trade against it). |
| **Version** | A strategy's text at one moment. Any change to the text makes a new version, except for spacing within a line, trailing spaces, line endings and extra blank lines. A version never changes once saved, so it tells you exactly what each of its trades was taken under. |
| **Run** | One version deployed over a period. Its mode is `live` (trading now) or `replay` (bar-by-bar replay of the past). It also carries free-text tags such as `paper`, `real` or `GC 15m`, which the desk stores and never interprets. |
| **Trade** | One entry in one run: ticker, side, a market or limit entry, stop, target, size, currency (CAD or USD), your reasons in your own words, and screenshots. |
| **Decision** | Anything you do to a trade after entering it: move the stop or target, close it by hand, correct a mistake, or void it. Each carries a reason. |

Two principles hold everything together:

1. **Only decisions are stored.** Exits, fills, statistics and whether a trade kept its
   rules are worked out from your decisions and the price bars whenever they are read.
   Nothing derived is stored, so nothing can go stale. (The one exception, from the release
   that adds checks: the rule results the desk showed you when you entered a trade are kept
   with it, as a record of what you saw.) A correction is a new entry that is read in place
   of the one it corrects. Nothing is ever edited or deleted.
2. **One timing rule.** An entry, a move and a hand close each have an `at`: the moment
   the decision takes effect, in UTC. In a live run it defaults to now. In a replay run you
   must give it, as the close of the bar you acted on. A decision takes effect from the
   first bar that opens at or after its `at`. A time without a timezone is refused, never
   guessed. Corrections and voids are record-keeping, not market decisions: they are dated
   when you make them.

## A strategy

````markdown
---
name: Pullback in an uptrend
---

## Idea
Buy a shallow pullback in a stock that is already rising, with the stop under the pullback low.

## Rules

```rules
timeframes: 1h, 15m
min_reward_risk: 2
max_risk_per_trade: 100 USD
size: computed
stop_required: yes
target_required: yes
max_open_trades: 2
cooldown_after_stop: 1 day, same ticker
daily_loss_limit: -2R
banned: shorts, options
```
````

A template ships with the package (`desk/strategy.example.md`). Every value is read when
the strategy is saved: an unknown rule, an unreadable value, a value in the wrong unit, or a
duplicate is refused, naming the rule and the fix. No rule is ever silently dropped.

| Rule | Meaning |
|---|---|
| `timeframes` | Required. The bar sizes the strategy trades on, e.g. `15m, 1h`. The finest one is used to judge exits. |
| `min_reward_risk` | The smallest reward-to-risk you will take. Reward-to-risk is the distance from the entry to the target divided by the distance from the entry to the stop, so `2` means the target is twice as far as the stop. |
| `max_risk_per_trade` | The most you will lose if the stop is hit, as an amount and a currency, e.g. `100 USD`. Risk is size × the stop's distance × the point value. |
| `size` | `computed`: the largest size `max_risk_per_trade` allows. |
| `stop_required`, `target_required` | `yes` or `no`. |
| `max_open_trades` | How many trades this run may hold at once. A pending limit order counts. |
| `cooldown_after_stop` | How long to wait after a stop-out before the next trade, e.g. `1h`, optionally `same ticker`. |
| `daily_loss_limit` | The most R the run may lose in a day, e.g. `-2R`. One R is the amount a trade risks between its entry and its stop. |
| `banned` | What you will not trade, e.g. `shorts, options, futures`. |
| `stop_headroom_candles` | The least distance between entry and stop, counted in median candle ranges. |

## Install

```bash
pip install trading-desk          # or, in a uv project: uv add trading-desk
```

Install it into the Python environment you will import it from: in 0.5 the desk is used
from Python, and its only command is `trading-desk integrity`. Python 3.11 or later, built
with SQLite 3.38 or later, on macOS or Linux. The package has no runtime dependencies.

## Use it from Python

```python
from desk import api, config, store

store.create()                  # once: makes desk.db under the data root
con = store.open()

text = config.example_strategy().read_text()      # or your own strategy's Markdown
api.save_strategy(con, "pullback", text)

run = api.start_run(con, "pullback", mode="live", tags=["paper"])
t = api.trade(con, run.id, ticker="ACME", side="long", entry_type="market",
              entry=100.0, stop=98.5, target=103.0, size=10, currency="USD",
              reasons="Held the rising trend line on the hourly; volume dried up on the dip.",
              screenshots=["https://www.tradingview.com/x/AbCd1234/"])

api.move_order(con, t.id, stop=100.0, reason="Price cleared the last high; stop to breakeven.")
api.get_trade(con, t.id).decisions
```

| Call | Does |
|---|---|
| `save_strategy`, `get_strategy`, `list_strategies` | Save a strategy (a new version whenever its text changes), read one, list them. Saving over an existing strategy needs `if_current`, the `current_hash` from your last read, so two sessions can never overwrite each other's edits. A strategy can also carry a `tracker_script` and `tracker_limit`; they are stored now and run in a later release. |
| `start_run`, `stop_run`, `list_runs` | Deploy a version, stop it, list runs. |
| `trade` | Enter a trade, with your reasons and screenshots. A screenshot is a local file, copied into the store under its content hash, or an `https` link. |
| `move_order`, `close_trade` | Move the stop or target, or close by hand. Each needs a reason. |
| `correct_trade`, `void_trade` | Correct a mistake (a new entry, read in place of the one it corrects), or void a trade: it stays in the log, and the releases that add statistics and checks leave it out of them. |
| `get_trade`, `list_trades` | Read trades back, with their decisions folded together. |

A refusal raises `api.Refused`, whose message says what was wrong and how to fix it. A
refused call writes nothing.

## Where your data lives

Everything lives under one folder: `~/Documents/trading-desk/` by default, or
`$TRADING_DESK_HOME`.

```
desk.db        strategies, runs, trades and every decision (append-only)
attachments/   screenshots, by content hash
config.toml    optional settings, e.g. db = "elsewhere.db"
```

`$TRADING_DESK_DB` moves only the database: it names one file in place of `desk.db`.
Screenshots still go to the root's `attachments/`, so to keep a session apart from your
real data, set `$TRADING_DESK_HOME` instead.
The database uses a rollback journal, never WAL, because `~/Documents` is often synced by
iCloud, and a database synced apart from its `-wal` file can be corrupted. Write from one
machine at a time.

`trading-desk integrity` checks the log for shapes it should never hold: a trade with no
entry, a decision dated before its trade, a move after a hand close, and more. It prints
each one, and exits 1 if it found any, or if there is no desk.db to check; 0 if the log is
clean. It checks the rows, not the file: a page SQLite cannot read stops it with an error,
and a damaged page it never reads can pass. To check the file itself, run
`sqlite3 -readonly desk.db 'PRAGMA integrity_check'`.

## Roadmap

| Release | Adds |
|---|---|
| 0.5 | The record (this release). |
| next | Bars and the judge: price bars from Yahoo for live runs and from TradingView chart exports for replay runs, and a pure function that works out every fill and exit from them. A stop hit fills at the stop, or at the open if the bar gapped through it. A target hit fills at the target. |
| then | Rule checks when you enter a trade (the results you were shown are kept with it), and statistics per run, version and strategy: win rate, average R, total R, P&L. Each can be narrowed by mode, by tags, or to the trades that kept their rules, worked out again on read. |
| then | The MCP server and a Claude Code plugin, so an agent records your trades and decisions for you. |
| then | Custom trackers: a script per strategy that records whatever you choose about each trade, stored separately. |
| 1.0 | The release, once all of the above has landed. |

## Development

```bash
uv sync
uv run pytest -q
uv run ruff check .
uv run python tools/no_real_figures.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md). MIT licensed.
