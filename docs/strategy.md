# A strategy

This is the template that ships, [`desk/strategy.example.md`](../desk/strategy.example.md), without
its extra prose:

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
