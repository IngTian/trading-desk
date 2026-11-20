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

The desk checks every planned trade against this block. `min_reward_risk` is the reward divided
by the risk: 2 means the target is twice as far from the entry as the stop is.

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
