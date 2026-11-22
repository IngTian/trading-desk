# trading-desk — a trading record your agent drives

[![PyPI version](https://img.shields.io/pypi/v/trading-desk)](https://pypi.org/project/trading-desk/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/IngTian/trading-desk/blob/main/LICENSE)

**trading-desk is an MCP server that keeps your trading record for an agent to drive.** MCP, the
Model Context Protocol, is how an agent such as Claude Code calls a program's functions. Each trade
is filed under the strategy, version and run that allowed it, with your reasons and screenshots,
and checked against the version's rules before entry; a breach never refuses it. The desk reads no
market data: a trade's fill, exit and cancellation come only from your reports, and every indicator
value the rules read comes only from what the agent supplies. It is bookkeeping: stats per run,
version and strategy, as counts, rates and averages, never a verdict.

## Is this you?

- **You trade with written rules** — a stop, a target, a risk per trade, a daily loss limit — and
  want every entry checked against them and kept with your reasons, so you can see which trades
  kept the rules and which did not.
- **You want an agent to keep the record.** You tell Claude Code what you did and why, and it
  files the trade, its moves and reports through the desk's tools.
- **You replay your history by hand** to see how a strategy would have done. Every figure comes
  from your reports, each dated at the bar you acted on.
- **You trade live** and want each fill, stop-out and cancellation recorded as you report it, with
  the same rules checked at entry.
- **You want the numbers** — win rate, average R, P&L per currency, fill rate — worked out per
  run, version and strategy, never as a verdict or a ranking.
- **You script it.** The same record is callable from Python, so your own analysis can read
  every trade and its whole record.

It will not place orders, send alerts or keep account balances: it is a record, not a broker. The
full list of what it does not do is in
[Limits](https://github.com/IngTian/trading-desk/blob/main/docs/limits.md).

**Contents:** [What the agent does for you](https://github.com/IngTian/trading-desk#what-the-agent-does-for-you) · [Install](https://github.com/IngTian/trading-desk#install) ·
[Use it from Claude Code](https://github.com/IngTian/trading-desk#use-it-from-claude-code) · [Your data](https://github.com/IngTian/trading-desk#your-data) ·
[Where to look for things](https://github.com/IngTian/trading-desk#where-to-look-for-things) · [Status](https://github.com/IngTian/trading-desk#status)

## What the agent does for you

You tell the agent what you did and why, and it calls the desk's tools. A session:

1. **Save a strategy**, a Markdown file (see
   [A strategy](https://github.com/IngTian/trading-desk/blob/main/docs/strategy.md)). Any change
   to its text beyond spacing makes a new version, and overwriting needs `if_current`, the
   `current_hash` from your last read, so two sessions never overwrite each other's edits.
2. **Start a run**: `live` or `replay`, one currency (CAD or USD) for all its trades, and free
   tags.
3. **Check a trade, then enter it**: `trade` with `check_only`, which records nothing, then
   without. A trade is long (profiting if the price rises) or short; it enters at market (filled
   at once) or with a limit order (filled only at its price or better); its stop is the price
   that exits a losing position, and its target the price that takes the profit.
   - It needs `instrument` (`stock`, `etf` or `future`) unless the ticker names a continuous
     future (`GC1!`), and a future's `point_value`, the cash a one-point move is worth on one
     contract. An ETF is an exchange-traded fund. A future is a contract to buy or sell at a set
     price on a later date.
   - Also give the `market` values its rules read, which `get_strategy`'s `market_keys` names,
     read off your chart at the bar you acted on. The desk computes none of them, and a key left
     out reads unknown for good, since `market` is not corrected.
   - Answer every checklist item; an unanswered one is recorded "not answered". Neither of these
     two is refused.
   - Over MCP, `reasons` is still required: pass an empty string with `check_only`.
   - Screenshots are local files, copied into the desk under a hash of their bytes, or https
     links.
4. **After entry**: `move_order` and `report`. The desk learns nothing on its own, so report each
   limit's fill or cancellation, and each exit with its kind and, for a hand exit, its price.
5. **Fix a mistake**: `correct_trade` and `void_trade`. A trade's currency cannot be corrected.
6. **Read back**: `get_trade`, with `checks_now` and `checks_differ`; and `list_trades`.
7. **Stop the run.** `stop_run` cancels its unfilled limit orders and lists their ids in
   `cancelled`; its open trades stay open until you report their exit.
8. **Read the stats** of a run, a version or a strategy.

Say you tell the agent about an entry on the pullback strategy:

> **you:** I'm long ACME at 100, stop 98.5, target 103, 10 shares. The pullback held the 1h EMA
> on lighter volume, and no tier-1 news inside the next hour.

The agent answers the checklist, fills in the `market` keys the rules read from your chart, and
checks the trade first with `check_only`, which records nothing:

```
min_reward_risk     kept  reward to risk 2.00 against a minimum of 2
max_risk_per_trade  kept  risk 15.00 USD against a limit of 100 USD; a size of up to 66 shares passes
stop_required       kept  the stop 98.5 is below the entry 100, which is the risk side for a long
target_required     kept  the target 103 is above the entry 100, which is the reward side for a long
sides               kept  the plan's side long is listed: long
max_open_trades     kept  0 trades open; this would make 1 against a limit of 2
daily_loss_limit    kept  the realised R of 2026-03-02 is +0.00 against a floor of -2 R
cooldown_after_loss kept  no loss on ACME before this trade
ema_50_1h           kept  close_1h 100.1 (supplied) is above ema_50_1h 99.4 (supplied)
```

A breach never refuses a trade: these results are kept with it, and `get_trade` works them out
again later as `checks_now`. The next day you report the exit, and the trade reads:

```
outcome  exited: fill 100.0, exit 103.0 at its target, r 2.0, cash 30.0
```

And the run's stats, worked out from nothing but the decisions you reported:

```
stats  wins 1 of 1 closed · win_rate 1.0 · average_r 2.0 · pnl USD 30.0
```

Every figure above is the desk's actual reply to that session, run end to end while writing this
README.

The 14 tools, grouped as they are in the reference: `save_strategy`, `get_strategy`,
`list_strategies` · `start_run`, `stop_run`, `list_runs` · `trade`, `move_order`,
`report`, `correct_trade`, `void_trade` · `get_trade`, `list_trades`, `stats`.
Every one, argument by argument, with the standing instructions the agent gets, is in
[The tools](https://github.com/IngTian/trading-desk/blob/main/docs/tools.md).

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
functions), on macOS or Linux. Its one dependency is the `mcp` SDK. To upgrade, run
`uv tool upgrade trading-desk`.

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

## Your data

Everything lives under one folder, the data root: `~/Documents/trading-desk/` by default, or
`$TRADING_DESK_HOME`.

```
desk.db        strategies, runs, trades and every decision, reports included (append-only)
attachments/   screenshots, named by the hash of their bytes
```

Append-only means rows are added, never changed or deleted. `$TRADING_DESK_DB` beats
`$TRADING_DESK_HOME`, for the database only. No config.toml is read. desk.db uses a rollback
journal, never WAL, because `~/Documents` is often synced by iCloud. The full story — the iCloud
guards, `trading-desk integrity`, and how to keep a session apart from your real data — is in
[Where your data lives](https://github.com/IngTian/trading-desk/blob/main/docs/data.md).

## Where to look for things

| I want to… | go here |
|---|---|
| see every tool and its arguments, and what the agent is told | [The tools](https://github.com/IngTian/trading-desk/blob/main/docs/tools.md) |
| write a strategy, or look up a rule | [A strategy](https://github.com/IngTian/trading-desk/blob/main/docs/strategy.md) |
| understand the model, and how a trade's outcome is read | [How it works](https://github.com/IngTian/trading-desk/blob/main/docs/how-it-works.md) |
| read a `stats` reply field by field | [Stats](https://github.com/IngTian/trading-desk/blob/main/docs/stats.md) |
| know where my data lives, and check its integrity | [Where your data lives](https://github.com/IngTian/trading-desk/blob/main/docs/data.md) |
| call the desk from Python | [Python](https://github.com/IngTian/trading-desk/blob/main/docs/python.md) |
| see what 0.11 does not do | [Limits](https://github.com/IngTian/trading-desk/blob/main/docs/limits.md) |
| see the version history | [Versions](https://github.com/IngTian/trading-desk/blob/main/docs/versions.md) |
| contribute | [CONTRIBUTING.md](https://github.com/IngTian/trading-desk/blob/main/CONTRIBUTING.md) |

## Status

**0.11.0, on PyPI, Beta:** the tools and the data format may change before 1.0, and each change is
listed under [Versions](https://github.com/IngTian/trading-desk/blob/main/docs/versions.md).

- It reads a desk.db at schema version 5. One an earlier version wrote is refused untouched: move
  it aside and restart the MCP server, which creates a new one, since there is no migration.
- A config.toml that sets `db` is refused, naming the step to take.

## Development

You need [uv](https://docs.astral.sh/uv/). The tests never reach the network.

```bash
uv sync
uv run pytest -q
uv run ruff check .
uv run python tools/no_real_figures.py
```

See [CONTRIBUTING.md](https://github.com/IngTian/trading-desk/blob/main/CONTRIBUTING.md). MIT
licensed — see [LICENSE](https://github.com/IngTian/trading-desk/blob/main/LICENSE).
