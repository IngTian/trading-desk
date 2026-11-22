# The tools

One MCP tool per function, served by `trading-desk mcp`.

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

The server sends standing [instructions](../desk/instructions.md) at the start of every session. In
short:

| When | The agent |
|---|---|
| Always | Passes your reasons exactly as you wrote them, never its own, and asks when you gave none. States no figure the desk did not return. Gives times in ISO 8601 with an offset: on a replay run, the close of the bar you acted on, in UTC; on a live run, whenever you say when you acted; and in every report. Defines each finance term at first use, and uses your pronouns, they/them until it knows them. After a refusal, fixes the form of the call itself, but asks you before it changes a time, a price, a size, a reason or a report, or voids. |
| Entering a trade | Supplies `market`: on a replay run every key, read off your chart; on a live run your facts, a future's indicator keys (Yahoo's futures bars lag), and any value you trust over the desk's. Answers every checklist item with a verdict and a one-line reason. Passes screenshots as full file paths or links, and asks you for a path when it has none. |
| After entry | On a replay run, reports each limit's fill or cancellation and each exit. Reports an expired day order (one that lapses at the session's close) as cancelled, why `not filled`. On a live run, reports a stop-out (an exit at the stop) at its real time, with the broker's price, and reports `open` a position the bars ended that your broker still holds. Before an entry on a live run, settles that run's futures trades, for the rules that read other trades, giving such an `open` report and the entry the same `at`. Reads each open live trade at least weekly, since Yahoo keeps 1-minute bars about 30 days. Stops a run, or voids a trade, only on your word. Fixes a mistake with `correct_trade` or `void_trade`, never with a new trade on top. |
