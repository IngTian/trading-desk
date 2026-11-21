---
name: Pullback in an uptrend
---

# Pullback in an uptrend

All figures in this file are invented. Copy it to start your own strategy.

## Idea

Buy a shallow pullback in a stock that is already rising, with the stop under the pullback low.

## Entry and exit

Enter when price turns up from the pullback. Take profit at the previous high.

## Rules

The desk reads this block when the strategy is saved and checks every trade against it when the
trade is entered, showing you the results. `min_reward_risk` is the reward divided
by the risk: 2 means the target is twice as far from the entry as the stop is.

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
