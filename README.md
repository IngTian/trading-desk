# trading-desk — a trading record your agent drives

[![PyPI version](https://img.shields.io/pypi/v/trading-desk)](https://pypi.org/project/trading-desk/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/IngTian/trading-desk/blob/main/LICENSE)

**trading-desk is an MCP server that keeps your trading record for an agent to drive.** MCP, the
Model Context Protocol, is how an agent such as Claude Code calls a program's functions. Each trade
is filed under the strategy, version and run that allowed it, with your reasons and screenshots,
and checked against the version's rules before entry; a breach never refuses it. Its outcome comes
from your reports when you replay history, or from price bars when you trade live, the reports
overriding the bars. It is bookkeeping: stats per run, version and strategy, as counts, rates and
averages, never a verdict.

## Is this you?

- **You trade with written rules** — a stop, a target, a risk per trade, a daily loss limit — and
  want every entry checked against them and kept with your reasons, so you can see which trades
  kept the rules and which did not.
- **You want an agent to keep the record.** You tell Claude Code what you did and why, and it
  files the trade, its moves, closes and reports through the desk's tools.
- **You replay your history by hand** to see how a strategy would have done. The desk reads no
  bars on a replay run: only your reports, each dated at the bar you acted on.
- **You trade live** and want outcomes judged from Yahoo's price bars, with your broker's fills
  and stop-outs reported over the bars.
- **You want the numbers** — win rate, average R, P&L per currency, fill rate — worked out per
  run, version and strategy, never as a verdict or a ranking.
- **You script it.** The same record is callable from Python, so your own analysis can read
  every trade and its bars.

It will not place orders, send alerts or keep account balances: it is a record, not a broker. The
full list of what it does not do is in
[Limits](https://github.com/IngTian/trading-desk/blob/main/docs/limits.md).

**Contents:** [What the agent does for you](#what-the-agent-does-for-you) · [Install](#install) ·
[Use it from Claude Code](#use-it-from-claude-code) · [Your data](#your-data) ·
[Where to look for things](#where-to-look-for-things) · [Status](#status)

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
     price on a later date; the desk reads Yahoo's rolling front month, the contract nearest
     expiry, which switches to the next at a roll.
   - Also give the `market` values its rules read, which `get_strategy`'s `market_keys` names. A
     key left out reads unknown, or on a live run is computed where the desk can.
   - Answer every checklist item; an unanswered one is recorded "not answered". Neither of these
     two is refused.
   - Over MCP, `reasons` is still required: pass an empty string with `check_only`.
   - Screenshots are local files, copied into the desk under a hash of their bytes, or https
     links.
4. **After entry**: `move_order`, `close_trade` and `report`.
5. **Fix a mistake**: `correct_trade` and `void_trade`. A trade's currency cannot be corrected.
6. **Read back**: `get_trade`, with `checks_now` and `checks_differ`; `list_trades`; and
   `trade_bars`, at one of its run's timeframes (the finest by default) or at `1m`.
7. **Stop the run.** `stop_run` sorts its unfilled limits into `cancelled` (by the stop),
   `awaiting_bars` (bars not yet at the stop: read again later) and `needs_report` (bars that
   could not be read: report each).
8. **Read the stats** of a run, a version or a strategy.

Every tool, argument by argument, and the standing instructions the agent gets, are in
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

## Your data

Everything lives under one folder, the data root: `~/Documents/trading-desk/` by default, or
`$TRADING_DESK_HOME`.

```
desk.db        strategies, runs, trades and every decision, reports included (append-only)
bars.db        price bars for live runs, and the range each fetch covered (insert-only)
attachments/   screenshots, named by the hash of their bytes
```

Append-only and insert-only mean rows are added, never changed or deleted. `$TRADING_DESK_DB` beats
`$TRADING_DESK_HOME`, for the database only. No config.toml is read. Both databases use a rollback
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
| see what 0.10 does not do | [Limits](https://github.com/IngTian/trading-desk/blob/main/docs/limits.md) |
| see the version history | [Versions](https://github.com/IngTian/trading-desk/blob/main/docs/versions.md) |
| contribute | [CONTRIBUTING.md](https://github.com/IngTian/trading-desk/blob/main/CONTRIBUTING.md) |

## Status

**0.10.0, on PyPI, Beta:** the tools and the data format may change before 1.0, and each change is
listed under [Versions](https://github.com/IngTian/trading-desk/blob/main/docs/versions.md).

- It reads a desk.db from 0.7 or later. One from 0.5 or 0.6 is refused untouched: move it aside and
  restart the MCP server, which creates a new one, since there is no migration.
- The one trap in upgrading from 0.8: a config.toml that sets `db` is refused, naming the step to
  take.
- 0.4 was a different tool, a portfolio book with a terminal UI. Its book (hub.db) is never opened
  unless you point `TRADING_DESK_DB` at it, and then it is refused untouched.

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
